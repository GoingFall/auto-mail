"""抽取流水线：串起 ICS → 规则 → LLM，并合并去重。

来源优先级（规格 §2 措辞修正）：**ICS > 明确规则 > LLM**。
冲突时**保留候选并进入审核**，不单纯取数值最高者——因为不同来源给出不同
时间意味着抽取本身不可信，正是需要人看的信号。

分层职责：

* 预筛器决定「是否抽取」与「是否值得调 LLM」（省 token）
* ICS 直解（零 LLM）
* 规则抽取
* 仅当预筛器认为值得、**且**规则未得到可用结果时才调 LLM
* 合并去重、冲突检测、标记待审原因
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from zoneinfo import ZoneInfo

from ..mail.mime import ParsedMail, parse_message
from ..models import EventSource
from .ics import parse_ics
from .llm import LlmExtractor, normalize_llm_events
from .prefilter import PrefilterVerdict, classify
from .rules import evaluate_hit, extract_by_rules

logger = logging.getLogger("automail.extract.pipeline")

#: 冲突判定：同一天但时刻差异超过此阈值（分钟）视为冲突
CONFLICT_MINUTES = 60


@dataclass(slots=True)
class Candidate:
    """流水线产出的候选事件（尚未落库）。"""

    title: str
    start_ts: str | None
    end_ts: str | None
    all_day: bool
    source: EventSource
    confidence: float
    fingerprint: str
    evidence: str = ""
    location: str | None = None
    organizer: str | None = None
    tz: str | None = None

    ics_uid: str | None = None
    ics_sequence: int | None = None
    ics_recurrence_id: str | None = None
    ics_rrule: str | None = None

    requires_review: bool = True
    review_reason: str = ""
    conflicts_with: list[str] = field(default_factory=list)
    """与之冲突的其它候选指纹，便于审核界面成组展示。"""

    title_from_subject: bool = False
    """标题只是退回了邮件主题——规则识别出时间，但说不出这是什么事件。

    去重时用它与否决「来源优先级」：两者指向同一时刻时，若规则说不出事件名
    而 LLM 说得出，应采用 LLM 的标题（时间仍取优先级更高者）。
    """

    @property
    def auto_pushable(self) -> bool:
        return not self.requires_review


@dataclass(slots=True)
class ExtractionOutcome:
    """一封邮件的抽取结果。"""

    verdict: PrefilterVerdict
    candidates: list[Candidate]
    llm_called: bool = False
    llm_ok: bool | None = None
    llm_error: str | None = None
    llm_skipped_reason: str | None = None
    """值得调 LLM 但未调用时的原因（凭据缺失/超预算）。

    与 ``llm_error`` 区分：那是「调了但失败」，这是「根本没调」。
    """

    rules_hits: int = 0
    ics_events: int = 0
    used_sources: tuple[EventSource, ...] = ()

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)


def extract_from_parsed(
    parsed: ParsedMail,
    *,
    received_at: datetime,
    user_timezone: str = "Asia/Shanghai",
    default_duration_minutes: int = 30,
    confidence_auto_push_threshold: float = 0.85,
    ambiguous_date_policy: str = "pending",
    llm: LlmExtractor | None = None,
    is_known_contact: bool = True,
    ics_non_contact_auto_push: bool = False,
    now: datetime | None = None,
) -> ExtractionOutcome:
    """对一封已解析邮件做完整抽取。

    Args:
        is_known_contact: 发件人是否为联系人（有过用户主动回复）。非联系人的
            ICS **默认进待审**，以降低「伪造 ICS 投毒」的风险（规格 §9）。
        now: 当前时刻，用于「时间已过 → 待审」判定。默认取真实当前时间；
            测试应显式传入固定值以保证可复现。
    """
    tz = ZoneInfo(user_timezone)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=tz)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)

    is_list = bool(parsed.list_id or parsed.has_unsubscribe)
    verdict = classify(
        subject=parsed.subject,
        text=parsed.body.text,
        auto_submitted=parsed.auto_submitted,
        is_list_mail=is_list,
        # 传 ``ics_parts`` 而非 ``has_ics`` 标志。预筛的 ``has_ics`` 语义是
        # 「**有可解析的** ICS，因此不必调 LLM」，只有真的拿到部件时才成立。
        #
        # ``has_ics`` 标志为真、部件却为空，是**离线重跑**的常态：库里只存
        # 清洗后的正文片段（隐私设计），不存 ICS 原文。此时若仍按标志短路，
        # 会出现最坏组合——预筛宣称「可直接解析 ICS」而不调 LLM，实际却
        # 一个部件都解析不了，整封邮件静默产出零候选（实测 #27/#33）。
        # 退回正文路径后，规则与 LLM 至少还有机会。
        has_ics=bool(parsed.ics_parts),
    )

    if not verdict.extract:
        return ExtractionOutcome(verdict=verdict, candidates=[], used_sources=())

    candidates: list[Candidate] = []
    sources: list[EventSource] = []

    # ── 1. ICS 直解（最高置信、零成本）──────────────────────
    if parsed.ics_parts:
        for part in parsed.ics_parts:
            for event in parse_ics(
                part.raw,
                user_timezone=user_timezone,
                default_duration_minutes=default_duration_minutes,
            ):
                candidate = _ics_to_candidate(
                    event,
                    is_known_contact=is_known_contact,
                    non_contact_auto_push=ics_non_contact_auto_push,
                    current=current,
                )
                candidates.append(candidate)
        if candidates:
            sources.append(EventSource.ICS)

    ics_count = len(candidates)

    # ── 2. 规则抽取（确定性）────────────────────────────────
    rule_result = extract_by_rules(
        text=parsed.body.text,
        subject=parsed.subject,
        received_at=received_at,
        user_timezone=user_timezone,
        default_duration_minutes=default_duration_minutes,
    )
    rules_hits = len(rule_result.hits)

    # 规则与 ICS 指向同一事件时不重复添加（ICS 更权威）
    ics_slots = {
        (c.start_ts or "")[:16] for c in candidates if c.start_ts
    }
    for hit in rule_result.hits:
        start_ts = hit.start.astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S%z")
        if start_ts[:16] in ics_slots:
            continue  # ICS 已覆盖同一时刻

        confidence, review, reason = evaluate_hit(
            hit,
            received_at=received_at,
            user_timezone=user_timezone,
            confidence_auto_push_threshold=confidence_auto_push_threshold,
            ambiguous_date_policy=ambiguous_date_policy,
            multiple_candidates=rule_result.ambiguous,
            now=current,
        )
        candidates.append(_rule_hit_to_candidate(hit, confidence, review, reason, user_timezone))

    if rules_hits:
        sources.append(EventSource.RULES)

    # ── 3. LLM 兜底（仅当值得且规则未给出可用结果）──────────
    llm_called = False
    llm_ok: bool | None = None
    llm_error: str | None = None
    llm_skipped_reason: str | None = None

    rules_usable = any(c.source is EventSource.RULES and not c.requires_review for c in candidates)
    if verdict.call_llm and not rules_usable:
        # ``available`` 是约定属性：为 False 表示「不具备调用条件」。
        # 用 getattr 兼容未实现该属性的轻量假对象（测试替身），缺省视为可用。
        if llm is None or getattr(llm, "available", True) is False:
            # **不可用不算「调用失败」**：它根本没被调用。
            # 分开统计很重要——「失败」意味着值得排查的错误，「跳过」意味着
            # 配置缺失或超预算。混为一谈会让使用者误判问题性质。
            llm_skipped_reason = (
                "未配置 LLM 凭据" if llm is None else "LLM 不可用（凭据缺失或已超预算）"
            )
        else:
            llm_called = True
            result = llm.extract(
                excerpt=parsed.body.text,
                subject=parsed.subject,
                received_at=received_at,
                user_timezone=user_timezone,
            )
            llm_ok = result.ok
            llm_error = result.error

            if result.ok and result.events:
                for normalized in normalize_llm_events(
                    result.events,
                    user_timezone=user_timezone,
                    received_at=received_at,
                    default_duration_minutes=default_duration_minutes,
                ):
                    if normalized.start_ts and normalized.start_ts[:16] in ics_slots:
                        continue
                    candidates.append(_llm_to_candidate(normalized))
                sources.append(EventSource.LLM)

    # ── 4. 跨来源去重：同一时刻的候选只保留最高优先级来源 ────
    #
    # 这一步是必要的：规则与 LLM 常指向同一事件，但标题措辞不同
    # （例如规则用邮件主题「阿里云域名到期提醒」，LLM 给出「域名续费」），
    # 内容指纹因此不同。若不去重，用户会在审核队列里看到两条重复项。
    # 优先级按规格：ICS > RULES > LLM。
    candidates = _dedupe_by_slot(candidates, tz)

    # ── 5. 冲突检测：同日但时刻差异大 → 全部转待审 ──────────
    _mark_conflicts(candidates)

    return ExtractionOutcome(
        verdict=verdict,
        candidates=candidates,
        llm_called=llm_called,
        llm_ok=llm_ok,
        llm_error=llm_error,
        llm_skipped_reason=llm_skipped_reason,
        rules_hits=rules_hits,
        ics_events=ics_count,
        used_sources=tuple(dict.fromkeys(sources)),
    )


def extract_from_raw(
    raw: bytes,
    *,
    received_at: datetime,
    max_chars: int = 4000,
    **kwargs: object,
) -> ExtractionOutcome:
    """从原始邮件字节做抽取（测试与离线评测的便捷入口）。"""
    from ..mail.sync import parse_raw_message

    parsed = parse_message(parse_raw_message(raw), max_chars=max_chars)
    return extract_from_parsed(parsed, received_at=received_at, **kwargs)  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────
# 组装辅助
# ──────────────────────────────────────────────────────────────


def _ics_to_candidate(
    event: object,
    *,
    is_known_contact: bool,
    non_contact_auto_push: bool,
    current: datetime,
) -> Candidate:
    """ICS 事件 → 候选。

    自动入历白名单要求（规格 §2/§9）：``METHOD:REQUEST``、``organizer`` 非空、
    起止可解析、时间在将来、**且发件人是联系人**（除非显式 opt-in）。
    """
    from .ics import IcsEvent

    assert isinstance(event, IcsEvent)

    reasons: list[str] = []
    is_request = (event.method or "REQUEST").upper() == "REQUEST"

    if event.is_cancellation:
        reasons.append("取消通知：必须人工裁决")
    if not is_request:
        reasons.append(f"非 REQUEST（METHOD={event.method}）")
    if not event.organizer:
        reasons.append("缺少 organizer（防投毒要求）")
    if not event.start_ts:
        reasons.append("起止时间不可解析")
    if not is_known_contact and not non_contact_auto_push:
        reasons.append("发件人非联系人（防投毒：默认待审）")

    # 时间已过的旧邀请不该自动写进日历（与规则路径同一判据）
    if event.start_ts:
        try:
            start = datetime.strptime(event.start_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
            if start < current.astimezone(UTC):
                reasons.append("时间已过（早于当前时刻，不建议入历）")
        except ValueError:
            reasons.append("起止时间格式无法解析")

    if event.needs_review and event.review_reason:
        reasons.append(event.review_reason)

    requires_review = bool(reasons)
    confidence = 0.99 if not requires_review else 0.9

    return Candidate(
        title=event.summary,
        start_ts=event.start_ts,
        end_ts=event.end_ts,
        all_day=event.all_day,
        source=EventSource.ICS,
        confidence=confidence,
        fingerprint=event.fingerprint,
        evidence=event.evidence,
        location=event.location,
        organizer=event.organizer,
        ics_uid=event.uid,
        ics_sequence=event.sequence,
        ics_recurrence_id=event.recurrence_id,
        ics_rrule=event.rrule,
        requires_review=requires_review,
        review_reason="；".join(reasons),
    )


def _rule_hit_to_candidate(
    hit: object, confidence: float, review: bool, reason: str, user_timezone: str
) -> Candidate:
    from .rules import RuleHit, hit_to_candidate_fields

    assert isinstance(hit, RuleHit)
    fields = hit_to_candidate_fields(hit)
    return Candidate(
        title=str(fields["title"]),
        start_ts=fields["start_ts"],  # type: ignore[arg-type]
        end_ts=fields["end_ts"],  # type: ignore[arg-type]
        all_day=bool(fields["all_day"]),
        source=EventSource.RULES,
        confidence=confidence,
        fingerprint=str(fields["fingerprint"]),
        evidence=hit.evidence,
        tz=user_timezone,
        requires_review=review,
        review_reason=reason,
        title_from_subject=bool(getattr(hit, "title_from_subject", False)),
    )


def _llm_to_candidate(normalized: object) -> Candidate:
    from .llm import NormalizedLlmEvent

    assert isinstance(normalized, NormalizedLlmEvent)
    return Candidate(
        title=normalized.title,
        start_ts=normalized.start_ts,
        end_ts=normalized.end_ts,
        all_day=normalized.all_day,
        source=EventSource.LLM,
        confidence=normalized.confidence,
        fingerprint=normalized.fingerprint,
        evidence=normalized.evidence,
        location=normalized.location,
        requires_review=True,  # LLM 恒待审
        review_reason=normalized.review_reason,
    )


#: 来源优先级（数值越大越权威）。用于跨来源去重时决定保留哪一条。
_SOURCE_PRIORITY: dict[EventSource, int] = {
    EventSource.ICS: 3,
    EventSource.RULES: 2,
    EventSource.LLM: 1,
}


def _local_parts(candidate: Candidate, tz: ZoneInfo) -> tuple[str, str | None]:
    """候选的本地日期与时刻（``HH:MM``；全天为 ``None``）。

    必须用**本地**时间而非 UTC 日期/时刻：

    * UTC 日期可能与本地的自然日差一天（香港 10-01 00:00 = 09-30 16:00Z），
      按 UTC 日期分组会把「同一天的事」拆到两组。
    * 更严重的是按 UTC **小时**分组会把本地 10:00 与 10:30（都是 02 点 UTC）
      判为同一槽——实测中这导致 LLM 抽对的「升旗禮 10:30」被静默丢弃。
    """
    if not candidate.start_ts:
        return "", None
    parsed = datetime.strptime(candidate.start_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    local = parsed.astimezone(tz)
    day = local.strftime("%Y-%m-%d")
    return day, None if candidate.all_day else local.strftime("%H:%M")


def _slot_key(candidate: Candidate, tz: ZoneInfo) -> str:
    """候选的「时刻槽」键：**本地**日期 + 分钟级时刻。

    分钟精度是必要的：同一小时内的两个事件（10:00 与 10:30）是不同的事，
    不能因为落在同一 UTC 小时就被合并。
    """
    day, clock = _local_parts(candidate, tz)
    if not day:
        return f"none:{candidate.fingerprint}"
    return f"{day}:allday" if clock is None else f"{day}:{clock}"


def _dedupe_by_slot(
    candidates: list[Candidate], tz: ZoneInfo | None = None
) -> list[Candidate]:
    """按时刻槽去重，保留来源优先级最高的候选；并用精确时刻取代同日全天。

    同时把被丢弃者的标题并入保留者的 evidence，避免信息丢失
    （审核时能看到「规则与 LLM 都指向这个时间」）。
    """
    if not candidates:
        return candidates

    tz = tz or ZoneInfo("Asia/Shanghai")
    candidates = _drop_all_day_when_timed_exists(candidates, tz)

    best: dict[str, Candidate] = {}
    for candidate in candidates:
        key = _slot_key(candidate, tz)
        existing = best.get(key)
        if existing is None:
            best[key] = candidate
            continue
        if _SOURCE_PRIORITY[candidate.source] > _SOURCE_PRIORITY[existing.source]:
            winner, loser = candidate, existing
        else:
            winner, loser = existing, candidate

        # 胜者说不出事件名、败者说得出 → 借用更好的标题。
        #
        # 来源优先级管的是**时间与置信度**，不该顺带决定「谁更会命名」。
        # 实测：规则能解析英文日期后，它凭优先级占住了时刻槽，标题却是
        # 退回主题的泛称（「XX Programme Condition Fulfilment and Orientation…」），
        # 把 LLM 的「Registration Deadline: …」挤掉了。时间取胜者、标题取
        # 更具体者，两者不冲突。
        if (
            winner.title_from_subject
            and not loser.title_from_subject
            and loser.title
            and loser.title.casefold() != winner.title.casefold()
        ):
            winner.title = loser.title

        if loser.title and loser.title.casefold() != winner.title.casefold():
            note = f"另有 {loser.source.value} 给出：{loser.title}"
            winner.evidence = f"{winner.evidence}；{note}" if winner.evidence else note
        # 任一来源认为需审核，则最终仍需审核（保守）
        if loser.requires_review and not winner.requires_review:
            winner.requires_review = True
            winner.review_reason = (
                f"{winner.review_reason}；{loser.review_reason}"
                if winner.review_reason
                else loser.review_reason
            )
        best[key] = winner

    # 保持原有顺序（按时刻槽首次出现顺序）
    ordered: list[Candidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _slot_key(candidate, tz)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(best[key])
    return ordered


def _mark_conflicts(candidates: list[Candidate]) -> None:
    """标记同一时刻存在分歧的候选，并全部转为待审。

    判据：两个候选日期相同、但开始时刻相差超过 :data:`CONFLICT_MINUTES`
    （或一个有具体时刻、另一个是全天）。这说明抽取本身不确定，应交人判断。
    """
    usable = [c for c in candidates if c.start_ts]
    for i, left in enumerate(usable):
        for right in usable[i + 1 :]:
            if not _is_conflicting(left, right):
                continue
            left.conflicts_with.append(right.fingerprint)
            right.conflicts_with.append(left.fingerprint)

    for candidate in usable:
        if candidate.conflicts_with:
            candidate.requires_review = True
            if "来源冲突" not in candidate.review_reason:
                suffix = "来源之间时间不一致，需人工裁决"
                candidate.review_reason = (
                    f"{candidate.review_reason}；{suffix}"
                    if candidate.review_reason
                    else suffix
                )


def _drop_all_day_when_timed_exists(
    candidates: list[Candidate], tz: ZoneInfo
) -> list[Candidate]:
    """同一本地日期上，若有定时候选则丢弃该日的全天候选。

    场景：规则从「二零二六年十月一日」抽出全天候选（没有时刻），
    而 LLM 从同一封邮件抽出了「10:30」。二者指同一件事，LLM 的更精确。
    若都保留，用户的日历上会出现「同一天的全天事件 + 具体时刻事件」两条。

    被丢弃的全天候选标题并入保留者的 evidence，信息不丢。
    """
    by_day_timed: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        day, clock = _local_parts(candidate, tz)
        if day and clock is not None:
            by_day_timed.setdefault(day, []).append(candidate)

    if not by_day_timed:
        return candidates

    kept: list[Candidate] = []
    for candidate in candidates:
        day, clock = _local_parts(candidate, tz)
        if clock is None and day in by_day_timed:
            timed = by_day_timed[day]
            note = f"另有 {candidate.source.value} 给出同日全天：{candidate.title}"
            for other in timed:
                other.evidence = (
                    f"{other.evidence}；{note}" if other.evidence else note
                )
            continue
        kept.append(candidate)
    return kept


def _is_conflicting(left: Candidate, right: Candidate) -> bool:
    """两个候选是否指向「同一天但时间不一致」。"""
    if not (left.start_ts and right.start_ts):
        return False
    day_left, time_left = _split(left.start_ts)
    day_right, time_right = _split(right.start_ts)
    if day_left != day_right:
        return False
    if time_left is None or time_right is None:
        # 一个全天一个有具体时刻，且都不是全天 → 视为冲突
        return left.all_day != right.all_day
    delta = abs(
        (time_left[0] * 60 + time_left[1]) - (time_right[0] * 60 + time_right[1])
    )
    return delta > CONFLICT_MINUTES


def _split(ts: str) -> tuple[str, tuple[int, int] | None]:
    day = ts[:10]
    if len(ts) >= 16 and ts[10] in "T ":
        try:
            return day, (int(ts[11:13]), int(ts[14:16]))
        except ValueError:
            return day, None
    return day, None


def summarize(outcome: ExtractionOutcome) -> dict[str, object]:
    """产出可写入 ``runs.stats`` 的摘要。"""
    return {
        "extract": outcome.verdict.extract,
        "call_llm": outcome.verdict.call_llm,
        "llm_called": outcome.llm_called,
        "llm_ok": outcome.llm_ok,
        "rules_hits": outcome.rules_hits,
        "ics_events": outcome.ics_events,
        "candidates": len(outcome.candidates),
        "auto_pushable": sum(1 for c in outcome.candidates if c.auto_pushable),
        "requires_review": sum(1 for c in outcome.candidates if c.requires_review),
        "sources": [s.value for s in outcome.used_sources],
    }


def load_message(message: Message, *, max_chars: int) -> ParsedMail:
    """便捷封装：``email.message.Message`` → :class:`ParsedMail`。"""
    return parse_message(message, max_chars=max_chars)
