"""抽取复盘：找出「可能没被提取对」的邮件。

用途是**迭代**，不是日常必看。每天固定时间跑一次，回答一个问题：

    最近的邮件里，哪些可能被抽错了？

它**只读数据库、只写本地报告**，不碰邮箱、不碰日历。默认也**不调用 LLM**——
复盘的目的是让人眼过一遍，而不是再花一次 token 得到同样的答案；
需要对比「LLM 会怎么说」时才加 ``--with-llm``。

## 为什么需要它

真实样本暴露的缺陷里，最危险的一类不是「抽错了」，而是**「静默漏掉」**：
预筛判定值得抽取、之后却没产出任何候选，流程照常报成功，使用者也看不出
少了什么（规格里这一条被反复强调）。把这类邮件主动挑出来，迭代才有目标。

因此本模块的核心是 :func:`_suspect`：按「预筛结论 × 规则命中数 × 候选数」
把邮件分成三档，最高档是「预筛说该抽、结果什么都没有」。

## 与「已抽出」的区别

报告里既列可疑项，也列已抽出的候选。已抽出**不**代表正确——它只是让
使用者能一眼扫过「今天理解成了什么」，比翻日历快得多。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import iso, parse_iso, utcnow
from .extract.pipeline import Candidate, extract_from_parsed
from .models import EventSource
from .settings import Settings

logger = logging.getLogger("automail.audit")

#: 可疑度。数值越大越该看。
SUSPECT_NONE = 0
SUSPECT_MAYBE = 1
SUSPECT_LIKELY = 2

_LEVEL_LABEL = {
    SUSPECT_NONE: "正常",
    SUSPECT_MAYBE: "值得留意",
    SUSPECT_LIKELY: "很可能漏抽",
}


def llm_configured(settings: Settings) -> bool:
    """LLM 凭据是否已配置（只影响报告措辞）。"""
    return bool(
        (settings.llm_base_url or "").strip() and settings.llm_api_key_value
    )


@dataclass(slots=True)
class AuditItem:
    """一封邮件在本轮复盘中的全部证据。"""

    message_id: int
    uid: int
    folder: str
    subject: str
    from_addr: str
    from_name: str | None
    received_at: str | None
    extract_status: str
    extract_attempts: int

    # ── 当前代码重跑一遍的结论（规则 + ICS，通常不含 LLM）──
    prefilter_extract: bool = False
    prefilter_call_llm: bool = False
    prefilter_reason: str = ""
    rules_hits: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    llm_called: bool = False
    llm_error: str | None = None
    llm_skipped_reason: str | None = None

    # ── 库中实际存着什么 ──────────────────────────────────
    stored_events: list[dict[str, Any]] = field(default_factory=list)

    # ── 判断 ──────────────────────────────────────────────
    suspicion: str | None = None
    suspicion_level: int = SUSPECT_NONE

    missing_from_store: list[str] = field(default_factory=list)
    """当前代码会产出、但库里没有的候选指纹。

    非空即说明**库中结果已过时**（修复缺陷后常见），跑一次
    ``automail extract --apply`` 即可对齐。
    """

    excerpt_head: str = ""
    """正文开头，供人快速判断这封邮件是什么。"""

    @property
    def stored_count(self) -> int:
        return len(self.stored_events)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def needs_eyes(self) -> bool:
        return self.suspicion_level > SUSPECT_NONE


@dataclass(slots=True)
class AuditReport:
    """一次复盘的汇总。"""

    generated_at: str
    tz_name: str
    hours: int
    window_start: str
    window_end: str
    items: list[AuditItem]
    used_llm: bool
    llm_available: bool = True
    """LLM 凭据是否已配置。

    未配置时报告头部要**说明一次**：此时「预筛要求 LLM 兜底」的邮件无法
    判定是否漏抽，逐封重复这句话只会淹掉报告。
    """

    @property
    def suspect_items(self) -> list[AuditItem]:
        return [i for i in self.items if i.needs_eyes]

    @property
    def stale_items(self) -> list[AuditItem]:
        return [i for i in self.items if i.missing_from_store]

    @property
    def with_candidates(self) -> list[AuditItem]:
        return [i for i in self.items if i.candidate_count]

    @property
    def unjudged_items(self) -> list[AuditItem]:
        """「需要 LLM 但本轮没调成」因此**无法判定**的邮件。

        不列为可疑（那是观测缺口，不是邮件的问题），但也**不能当作正常**——
        它们只是没被观测。单独计数，避免使用者把「未判定」误读成「已确认没问题」。

        判据与 :func:`_suspect` 保持一致：看 **LLM 这次调用有没有发生**，
        而不是看有没有配置。传入了但不可用（超预算、临时故障）同样属于
        观测缺口，应计入这里而不是被当成「这封邮件没问题」。
        """
        return [
            i
            for i in self.items
            if i.prefilter_extract
            and i.prefilter_call_llm
            and not i.llm_called
            and not i.candidate_count
        ]

    def counts(self) -> dict[str, int]:
        return {
            "messages": len(self.items),
            "likely_missed": sum(
                1 for i in self.items if i.suspicion_level == SUSPECT_LIKELY
            ),
            "maybe": sum(1 for i in self.items if i.suspicion_level == SUSPECT_MAYBE),
            "with_candidates": len(self.with_candidates),
            "stale": len(self.stale_items),
            "unjudged": len(self.unjudged_items),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "timezone": self.tz_name,
            "hours": self.hours,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "llm_used": self.used_llm,
            "counts": self.counts(),
            "items": [
                {
                    "message_id": i.message_id,
                    "uid": i.uid,
                    "subject": i.subject,
                    "from_addr": i.from_addr,
                    "received_at": i.received_at,
                    "suspicion_level": i.suspicion_level,
                    "suspicion": i.suspicion,
                    "prefilter_extract": i.prefilter_extract,
                    "prefilter_call_llm": i.prefilter_call_llm,
                    "prefilter_reason": i.prefilter_reason,
                    "rules_hits": i.rules_hits,
                    "candidate_count": i.candidate_count,
                    "stored_count": i.stored_count,
                    "missing_from_store": i.missing_from_store,
                    "candidates": [
                        {
                            "title": c.title,
                            "start_ts": c.start_ts,
                            "end_ts": c.end_ts,
                            "all_day": c.all_day,
                            "source": c.source.value,
                            "confidence": c.confidence,
                            "requires_review": c.requires_review,
                            "evidence": c.evidence,
                        }
                        for c in i.candidates
                    ],
                }
                for i in self.items
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)


def run_audit(
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    hours: int = 24,
    limit: int = 200,
    llm: Any | None = None,
    now: Any | None = None,
) -> AuditReport:
    """对最近 ``hours`` 小时收到的邮件做一次复盘。

    Args:
        llm: 传入则用它重跑 LLM 路径（``--with-llm``）。缺省**不调用 LLM**，
            因此本函数在离线、无凭据时同样可用。
        now: 当前时刻，测试注入固定值以保证可复现。
    """
    tz = ZoneInfo(settings.user_timezone)
    current = now or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)

    window_end = current
    window_start = current - timedelta(hours=hours)

    rows = _recent_messages(
        conn,
        account=settings.account,
        start=iso(window_start),
        end=iso(window_end),
        limit=limit,
    )

    items: list[AuditItem] = []
    for row in rows:
        try:
            items.append(_audit_one(settings, conn, row, llm=llm, now=current))
        except Exception as exc:  # noqa: BLE001 - 单封失败不拖垮整轮复盘
            logger.warning("复盘 UID %s 失败：%s", row["uid"], exc)
            items.append(_failed_item(row, exc))

    return AuditReport(
        generated_at=iso(current) or "",
        tz_name=settings.user_timezone,
        hours=hours,
        window_start=window_start.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        window_end=window_end.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
        items=items,
        used_llm=llm is not None,
        llm_available=llm_configured(settings),
    )


def _recent_messages(
    conn: sqlite3.Connection,
    *,
    account: str,
    start: str,
    end: str,
    limit: int,
) -> list[sqlite3.Row]:
    """取窗口内的邮件：**收到**在窗口内，或**同步**在窗口内。

    两个条件取并集，而不是只看 ``received_at``：

    * 只看 ``received_at`` 会漏掉「机器休眠/关机期间到达、恢复后才同步」
      的邮件——它们恰恰是**质量最可能出问题**的那批（隔了很久才处理，
      而使用者还以为已经看过了）。
    * 只看 ``fetched_at`` 会把首次同步的历史邮件全当成「新邮件」。

    并集同时解决两者：日常使用等价于「最近 24 小时收到的」，首次同步或
    长时间离线后等价于「刚同步进来的全部」，两种情形都是该复查的对象。

    副本（``is_canonical=0``）不参与抽取，因此不进复盘——否则同一内容
    会在报告里出现两次，白白稀释可信度。
    """
    return list(
        conn.execute(
            """
            SELECT * FROM messages
             WHERE account = ? AND is_canonical = 1
               AND (
                     (received_at IS NOT NULL AND received_at >= ? AND received_at <= ?)
                  OR (fetched_at  IS NOT NULL AND fetched_at  >= ? AND fetched_at  <= ?)
               )
             ORDER BY received_at DESC
             LIMIT ?
            """,
            (account, start, end, start, end, limit),
        ).fetchall()
    )


def _audit_one(
    settings: Settings,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    llm: Any | None,
    now: Any,
) -> AuditItem:
    from .extract.runner import _synthetic_parsed

    parsed = _synthetic_parsed(row)
    received = parse_iso(row["received_at"]) or now

    outcome = extract_from_parsed(
        parsed,
        received_at=received,
        user_timezone=settings.user_timezone,
        default_duration_minutes=settings.default_event_duration_minutes,
        confidence_auto_push_threshold=settings.confidence_auto_push_threshold,
        ambiguous_date_policy=settings.ambiguous_date_policy,
        llm=llm,
        is_known_contact=True,
        ics_non_contact_auto_push=settings.ics_auto_push_non_contact,
        now=now,
    )

    stored = _stored_events(conn, row["id"])
    missing = _stale_candidates(
        outcome.candidates, stored, llm_called=outcome.llm_called
    )

    level, reason = _suspect(
        extract=outcome.verdict.extract,
        call_llm=outcome.verdict.call_llm,
        llm_called=outcome.llm_called,
        rules_hits=outcome.rules_hits,
        candidate_count=len(outcome.candidates),
        text=parsed.body.text,
        missing_from_store=missing,
        extract_status=row["extract_status"],
    )

    return AuditItem(
        message_id=int(row["id"]),
        uid=int(row["uid"]),
        folder=row["folder"],
        subject=row["subject"] or "(无主题)",
        from_addr=row["from_addr"] or "",
        from_name=row["from_name"],
        received_at=row["received_at"],
        extract_status=row["extract_status"],
        extract_attempts=int(row["extract_attempts"] or 0),
        prefilter_extract=outcome.verdict.extract,
        prefilter_call_llm=outcome.verdict.call_llm,
        prefilter_reason=outcome.verdict.reason,
        rules_hits=outcome.rules_hits,
        candidates=list(outcome.candidates),
        llm_called=outcome.llm_called,
        llm_error=outcome.llm_error,
        llm_skipped_reason=outcome.llm_skipped_reason,
        stored_events=stored,
        suspicion=reason,
        suspicion_level=level,
        missing_from_store=missing,
        excerpt_head=_head(parsed.body.text),
    )


def _stale_candidates(
    candidates: list[Candidate],
    stored: list[dict[str, Any]],
    *,
    llm_called: bool,
) -> list[str]:
    """当前代码会产出、但库里没有等价事件的候选指纹。

    **只比较确定性来源（规则 / ICS）**，两边都排除 LLM 产物。原因是 LLM
    输出**每次都不完全一样**：同一封邮件、同一份代码，两次调用的标题与
    事件划分都可能不同。拿新一次的 LLM 结果去比对上一次存下的 LLM 结果，
    差异只反映「模型这次说了别的」，而不是「代码变了」——那样的报告每天
    都会亮，很快就会被无视。

    另外，**本轮没调 LLM、而库中该邮件有 LLM 事件时，整条判定跳过**。
    此时两条流水线不等价，比较没有意义：

    实测 #92：离线规则从「二零二六年十月一日」抽出**全天**候选，而真实
    运行时 LLM 给出了更精确的 10:30 定时候选，流水线按「同日有定时则取代
    全天」丢弃了前者，因此库里本就没有那条全天事件。离线重跑看不到这个
    取代关系，会把它误报成「库中缺失」。

    这是刻意的取舍：宁可漏报（等 `--with-llm` 复查时再判），
    不可误报——假警报会让报告失去可信度。
    """
    if not llm_called and any(
        (e.get("source") or "") == EventSource.LLM.value for e in stored
    ):
        return []

    deterministic = [c for c in candidates if c.source is not EventSource.LLM]
    if not deterministic:
        return []
    stable_stored = [
        e for e in stored if (e.get("source") or "") != EventSource.LLM.value
    ]

    missing: list[str] = []
    for candidate in deterministic:
        if not any(_same_event(candidate, event) for event in stable_stored):
            missing.append(candidate.fingerprint)
    return missing


#: 判定「库里已经有这件事」的标题相似度门槛。
#:
#: 与 review 的重复提示用同一个阈值，避免两处对「同一件事」的判断不一致。
_SAME_EVENT_SIMILARITY = 0.8


def _same_event(candidate: Candidate, stored: dict[str, Any]) -> bool:
    """库中事件与候选是否指同一件事：同一起始时刻且标题相似。

    全天事件只比日期——规则抽出的全天事件用当地零点表示，与 LLM 给的
    日期可能相差若干小时，比到分钟会误判。
    """
    if not candidate.start_ts or not stored.get("start_ts"):
        return False

    same_all_day = bool(stored.get("all_day")) == candidate.all_day
    if candidate.all_day and same_all_day:
        return candidate.start_ts[:10] == str(stored["start_ts"])[:10]

    if candidate.start_ts != stored["start_ts"]:
        return False
    return _title_similar(candidate.title, stored.get("title"))


def _title_similar(left: str | None, right: str | None) -> bool:
    from .extract.fingerprint import title_similarity

    return title_similarity(left, right) >= _SAME_EVENT_SIMILARITY


def _suspect(
    *,
    extract: bool,
    call_llm: bool,
    llm_called: bool,
    rules_hits: int,
    candidate_count: int,
    text: str,
    missing_from_store: list[str],
    extract_status: str,
) -> tuple[int, str | None]:
    """判定可疑度。

    判据全部来自**已经算出来的结论**，不依赖 LLM 是否可用——否则在没配
    LLM 的机器上复盘会一片「正常」，而现在最常见的漏抽恰恰是
    「规则配不上、本该由 LLM 兜底」那类。

    「本轮**没有** LLM 可用」与「有 LLM 却没抽出」必须分开。前者是**观测
    能力的缺口**，不是某封邮件的毛病：默认复盘就不调 LLM，于是每一封需要
    LLM 的邮件都会产出同一句话。实测 72 小时窗口里 16 个可疑项有 15 个
    是它——报告会被同一句话淹没，等于失效。

    界线的判据是**这次调用究竟发生没有**：

    * ``llm_called`` 为假（未配凭据 / 超预算 / 本轮不可用）→ 信息缺失，
      无从判断，计入「未判定」而非可疑。
    * ``llm_called`` 为真但没抽到 → 这是**可观测的失败**，属于可疑项。
    """
    # 状态 failed 优先于一切：它表示上次抽取**出过错**，无论现在能否抽出，
    # 都该看一眼。放在「过时」判断之前是必要的——失败的邮件通常没有事件，
    # 若先走「过时」分支，报告会说是「代码变了」，掩盖了「上次失败」这个真因。
    if extract_status == "failed":
        if candidate_count:
            return SUSPECT_LIKELY, (
                f"上次抽取失败；当前代码能抽出 {candidate_count} 条候选，"
                "跑 automail extract --apply 重试"
            )
        return SUSPECT_MAYBE, "该邮件上次抽取失败，且当前代码仍未抽出候选"

    # 库中结果落后于当前代码：修完缺陷后重跑抽取即可对齐，属于**确定**的遗漏
    if missing_from_store:
        return SUSPECT_LIKELY, (
            f"库中结果已过时：当前代码会多抽出 {len(missing_from_store)} 条候选，"
            "跑 automail extract --apply 对齐"
        )

    if extract and candidate_count == 0:
        if call_llm and not llm_called:
            # 本该由 LLM 兜底，而本轮它没被调用 → 无从判定（计入「未判定」）
            return SUSPECT_NONE, None
        if call_llm:
            # **已经问过最会判断的那个来源，它也说没有** → 多半确实没有事件。
            #
            # 实测：账单通知、账号升级进度、确认邮箱这类邮件会命中「登記」
            # 等宽泛关键词而被预筛放行，但正文里确实没有可入历的时间。
            # 把它们标成「很可能漏抽」会让报告充满假警报——而假警报会让
            # 使用者不再看报告，功能等于失效。
            return SUSPECT_MAYBE, (
                "预筛认为含事件线索，但规则与 LLM 都没抽出时间"
                "（可能是仅有动作要求、没有具体时间的通知类邮件）"
            )
        # 预筛说「规则足够、不必问 LLM」而规则空手而归：**我们主动放弃了
        # 最会判断的来源**，也没有任何证据说明真的没有事件。这才是最该看的。
        return SUSPECT_LIKELY, (
            "预筛判定规则足以覆盖（未调用 LLM），但规则未命中任何时间——"
            "建议核对：该邮件是否被预筛误判为「时间形态完整」"
        )

    # 被预筛拦下，但正文确实有日期与时刻形态 → 可能是误拦
    if not extract and _has_date_and_time_shapes(text):
        return SUSPECT_MAYBE, "被预筛过滤，但正文含日期与时刻形态"

    if extract and rules_hits and candidate_count == 0:
        # 规则命中了时间，却没有任何候选——去重或配对把它吃掉了
        return SUSPECT_MAYBE, f"规则命中 {rules_hits} 个时间，但最终没有候选产出"

    return SUSPECT_NONE, None


def _has_date_and_time_shapes(text: str) -> bool:
    """正文里是否同时出现日期与时刻的**形态**（不要求配对）。

    复用预筛器的正则，保证「审计视角」与「预筛视角」一致——用另一套规则
    去判断会让两边结论打架，反而更难排查。
    """
    from .extract.prefilter import _DATE_SHAPE_RE, _TIME_SHAPE_RE

    if not text:
        return False
    return bool(_DATE_SHAPE_RE.search(text) and _TIME_SHAPE_RE.search(text))


def _stored_events(conn: sqlite3.Connection, message_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, title, start_ts, all_day, source, confidence, status, fingerprint "
        "FROM events WHERE message_id = ? ORDER BY start_ts ASC",
        (message_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _head(text: str, *, limit: int = 160) -> str:
    """正文开头，压成一行——报告是一眼扫的，不是拿来读全文的。"""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def _failed_item(row: sqlite3.Row, exc: Exception) -> AuditItem:
    return AuditItem(
        message_id=int(row["id"]),
        uid=int(row["uid"]),
        folder=row["folder"],
        subject=row["subject"] or "(无主题)",
        from_addr=row["from_addr"] or "",
        from_name=row["from_name"],
        received_at=row["received_at"],
        extract_status=row["extract_status"],
        extract_attempts=int(row["extract_attempts"] or 0),
        suspicion=f"复盘时出错：{type(exc).__name__}: {exc}",
        suspicion_level=SUSPECT_LIKELY,
    )


# ──────────────────────────────────────────────────────────────
# 渲染
# ──────────────────────────────────────────────────────────────

def render_markdown(report: AuditReport) -> str:
    """渲染成 Markdown 报告。"""
    counts = report.counts()
    lines: list[str] = []
    lines.append(f"# 抽取复盘 · {report.window_end[:10]}")
    lines.append("")
    lines.append(
        f"窗口：最近 {report.hours} 小时"
        f"（{report.window_start} → {report.window_end} {report.tz_name}）"
    )
    lines.append(
        f"邮件 {counts['messages']} 封 ｜ 很可能漏抽 {counts['likely_missed']}"
        f" ｜ 值得留意 {counts['maybe']} ｜ 已抽出 {counts['with_candidates']}"
        f" ｜ 库中结果过时 {counts['stale']}"
        + (f" ｜ 未判定 {counts['unjudged']}" if counts.get("unjudged") else "")
    )
    if not report.used_llm:
        lines.append("")
        if report.llm_available:
            lines.append(
                "> 本轮**未调用 LLM**（只跑了规则 + ICS）。"
                "凡规则配不上、本该由 LLM 兜底的邮件，本轮**无法判定**是否漏抽，"
                "因此计入下方「未判定」而非可疑项。"
            )
            lines.append(
                "> 加 `--with-llm` 复查这些邮件（会消耗 LLM 额度）。"
            )
        else:
            lines.append(
                "> **LLM 未配置**：规则配不上、本该由 LLM 兜底的邮件本轮都无法判定，"
                "计入下方「未判定」。"
            )
            lines.append(
                "> 配好 `LLM_BASE_URL` / `LLM_API_KEY` 后重跑，或加 `--with-llm` 复查。"
            )
    if report.unjudged_items:
        unjudged = report.unjudged_items
        lines.append("")
        lines.append(f"### 未判定（{len(unjudged)} 封，需 LLM 才能判断）")
        lines.append("")
        for item in unjudged:
            lines.append(
                f"- `#{item.message_id}` {item.subject} — "
                f"{item.prefilter_reason}"
            )
        lines.append("")
    lines.append("")

    suspects = report.suspect_items
    lines.append(f"## 需要你看一眼（{len(suspects)} 封）")
    lines.append("")
    if not suspects:
        lines.append("没有可疑项。")
        lines.append("")
    for item in suspects:
        lines.extend(_render_item(item, verbose=True))

    ok = [i for i in report.items if not i.needs_eyes and i.candidate_count]
    lines.append(f"## 已抽出候选（{len(ok)} 封）")
    lines.append("")
    if not ok:
        lines.append("无。")
        lines.append("")
    for item in ok:
        lines.extend(_render_item(item, verbose=False))

    quiet = [i for i in report.items if not i.needs_eyes and not i.candidate_count]
    lines.append(f"## 其余（{len(quiet)} 封，未抽出且无明显线索）")
    lines.append("")
    for item in quiet:
        lines.append(f"- `#{item.message_id}` {item.subject} — {item.prefilter_reason}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("排查单个邮件：`automail audit --show <id>`")
    return "\n".join(lines)


def _render_item(item: AuditItem, *, verbose: bool) -> list[str]:
    lines: list[str] = []
    marker = ""
    if item.suspicion_level == SUSPECT_LIKELY:
        marker = " **[很可能漏抽]**"
    elif item.suspicion_level == SUSPECT_MAYBE:
        marker = " **[值得留意]**"

    lines.append(f"### `#{item.message_id}` {item.subject}{marker}")
    lines.append("")
    lines.append(f"- 收到：{_fmt_local(item.received_at)} ｜ 发件人：{_fmt_sender(item)}")
    lines.append(
        f"- 预筛：{'值得抽取' if item.prefilter_extract else '过滤'}"
        f"（{item.prefilter_reason}）"
    )
    lines.append(
        f"- 规则命中：{item.rules_hits} 个时间 ｜ 当前候选：{item.candidate_count}"
        f" ｜ 库中事件：{item.stored_count}"
    )
    if item.suspicion:
        lines.append(f"- ⚠️ {item.suspicion}")
    if item.llm_error:
        lines.append(f"- LLM 错误：{item.llm_error}")
    if item.llm_skipped_reason:
        lines.append(f"- LLM 跳过：{item.llm_skipped_reason}")
    if verbose:
        lines.append("")
        lines.append(f"  正文开头：{item.excerpt_head or '（空）'}")
    lines.append("")

    rows = item.candidates or []
    if rows:
        for c in rows:
            when = _fmt_when(c.start_ts, all_day=c.all_day)
            flag = "待审" if c.requires_review else "可自动"
            lines.append(f"  - {when} ｜ {c.title} ｜ {c.source.value} {c.confidence:.2f} ｜ {flag}")
        lines.append("")
    return lines


def _fmt_local(ts: str | None) -> str:
    if not ts:
        return "未知"
    return ts.replace("T", " ").replace("Z", "")


def _fmt_sender(item: AuditItem) -> str:
    if item.from_name:
        return f"{item.from_name} <{item.from_addr}>"
    return item.from_addr or "未知"


def _fmt_when(ts: str | None, *, all_day: bool) -> str:
    if not ts:
        return "无时间"
    stamp = ts[:16].replace("T", " ")
    return f"{stamp[:10]} 全天" if all_day else stamp


def render_detail(
    conn: sqlite3.Connection, settings: Settings, message_id: int, *, now: Any | None = None
) -> str:
    """渲染单封邮件的完整细节，供人工/开发者排查。

    与报告不同，这里**打印全部正文片段**——仓库里只存了清洗后的片段
    （隐私设计），排查漏抽时必须看到它。
    """
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        return f"没有找到邮件 #{message_id}"

    current = now or utcnow()
    item = _audit_one(settings, conn, row, llm=None, now=current)
    stored = item.stored_events

    lines: list[str] = []
    lines.append(f"# 邮件 #{item.message_id}")
    lines.append("")
    lines.append(f"- 主题：{item.subject}")
    lines.append(f"- 发件人：{_fmt_sender(item)}")
    lines.append(f"- 收到：{_fmt_local(item.received_at)} ｜ 文件夹：{item.folder} ｜ UID：{item.uid}")
    lines.append(
        f"- 抽取状态：{item.extract_status}（尝试 {item.extract_attempts} 次）"
    )
    lines.append("")
    lines.append("## 预筛")
    lines.append("")
    lines.append(f"- 结论：{'值得抽取' if item.prefilter_extract else '过滤'}")
    lines.append(f"- 需要 LLM：{'是' if item.prefilter_call_llm else '否'}")
    lines.append(f"- 理由：{item.prefilter_reason}")
    lines.append("")
    lines.append("## 当前代码的候选")
    lines.append("")
    if item.candidates:
        for c in item.candidates:
            lines.append(
                f"- {_fmt_when(c.start_ts, all_day=c.all_day)} ｜ {c.title}"
                f" ｜ {c.source.value} {c.confidence:.2f}"
                f" ｜ {'待审' if c.requires_review else '可自动'}"
            )
            if c.evidence:
                lines.append(f"  - 依据：{c.evidence}")
            if c.review_reason:
                lines.append(f"  - 待审原因：{c.review_reason}")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 库中事件")
    lines.append("")
    if stored:
        for e in stored:
            lines.append(
                f"- id={e['id']} ｜ {_fmt_when(e['start_ts'], all_day=bool(e['all_day']))}"
                f" ｜ {e['title']} ｜ {e['source']} {e['confidence']:.2f} ｜ {e['status']}"
            )
    else:
        lines.append("（无）")
    lines.append("")
    if item.suspicion:
        lines.append(f"## 可疑\n\n{item.suspicion}")
        lines.append("")
    lines.append("## 正文片段（清洗后，最多 4000 字）")
    lines.append("")
    lines.append("```")
    lines.append(row["body_excerpt"] or "(空)")
    lines.append("```")
    return "\n".join(lines)
