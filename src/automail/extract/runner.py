"""抽取运行器：把流水线接到数据库。

职责：
* 逐封领取待抽取邮件（原子领取 + 僵尸回收）
* 用流水线抽取候选
* 落库（唯一索引保证幂等）
* 记录 ``extract_status`` 与 ``extract_attempts``
* 统计 LLM 降级（``llm_skipped``）——规格要求单列，否则「本可抽取但被跳过」
  的邮件对使用者不可见

**LLM 不可用时降级而非失败**：抽取仍产出规则与 ICS 的结果，
只是少了 LLM 那一路，并在统计里如实报告。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..db import parse_iso, utcnow
from ..models import ExtractStatus
from ..progress import ProgressReporter
from ..settings import Settings
from ..store import EventRepository, MessageRepository, SenderRepository
from .llm import LlmExtractor
from .pipeline import ExtractionOutcome, extract_from_parsed

logger = logging.getLogger("automail.extract.runner")


@dataclass(slots=True)
class ExtractStats:
    """一轮抽取的统计。"""

    considered: int = 0
    """本轮领取的邮件数。"""

    skipped_by_prefilter: int = 0
    with_candidates: int = 0
    candidates: int = 0
    auto_pushable: int = 0
    requires_review: int = 0
    events_inserted: int = 0
    events_updated: int = 0
    events_protected: int = 0
    """因已被人工审批（approve/reject/ignore）而未被覆盖的事件数。"""

    events_removed: int = 0
    """重跑后**不再产出**、且可安全删除的旧候选数（孤儿清理）。

    意义：改进抽取后重跑会改变标题与指纹，只写不删会让同一件事在库里
    留两条。测到就删，但要满足「人未表态、未手改、未入历」三个条件。
    """

    events_orphaned: int = 0
    """重跑后不再产出、但**不能安全删除**的旧候选数（保留原样）。

    这些已经承载了人的决定或外部副作用（approved/pushed、手改过、已写进
    日历）。删除它们等于销毁人的判断与日历条目，因此只报告不处理。
    """

    failed: int = 0
    attempts_exhausted: int = 0
    zombies_reclaimed: int = 0
    """从崩溃留下的 running 中间态回收的记录数。"""
    by_source: dict[str, int] = field(default_factory=dict)
    llm_called: int = 0
    llm_ok: int = 0
    llm_failed: int = 0
    llm_skipped: int = 0
    """预筛放行且值得调 LLM、但**因 LLM 不可用或超预算而未调**的邮件数。

    规格 §14 要求单列此项：否则「本可被 LLM 抽取但没抽」对使用者不可见，
    会让人误以为抽取质量就是这样。
    """

    llm_skipped_reasons: list[str] = field(default_factory=list)
    """跳过的原因集合（去重），便于在报告里说明为什么没调 LLM。"""

    llm_unavailable: bool = False
    dry_run: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "considered": self.considered,
            "skipped_by_prefilter": self.skipped_by_prefilter,
            "with_candidates": self.with_candidates,
            "candidates": self.candidates,
            "auto_pushable": self.auto_pushable,
            "requires_review": self.requires_review,
            "events_inserted": self.events_inserted,
            "events_updated": self.events_updated,
            "events_protected": self.events_protected,
            "events_removed": self.events_removed,
            "events_orphaned": self.events_orphaned,
            "failed": self.failed,
            "attempts_exhausted": self.attempts_exhausted,
            "zombies_reclaimed": self.zombies_reclaimed,
            "by_source": dict(self.by_source),
            "llm_called": self.llm_called,
            "llm_ok": self.llm_ok,
            "llm_failed": self.llm_failed,
            "llm_skipped": self.llm_skipped,
            "llm_skipped_reasons": list(self.llm_skipped_reasons),
            "llm_unavailable": self.llm_unavailable,
        }


class ExtractRunner:
    """按配置抽取待处理邮件。"""

    def __init__(
        self,
        settings: Settings,
        conn,
        *,
        llm: LlmExtractor | None = None,
        account: str | None = None,
        reporter: ProgressReporter | None = None,
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._account = account or settings.account
        self._messages = MessageRepository(conn)
        self._events = EventRepository(conn)
        self._senders = SenderRepository(conn)
        self._llm = llm
        # 进度上报。默认 None → 空操作，行为与引入前一致。
        self._reporter = reporter or ProgressReporter()

    def run(self, *, apply: bool = False, limit: int | None = None) -> ExtractStats:
        """抽取待处理邮件。

        Args:
            apply: ``False``（默认）为 dry-run——不写库，只报告将产生的候选。
            limit: 本轮最多处理几封。
        """
        stats = ExtractStats(dry_run=not apply)

        if self._llm is not None and not self._llm.available:
            stats.llm_unavailable = True
            logger.warning("LLM 凭据未配置，抽取降级为「仅规则 + ICS」模式")

        if apply:
            # 僵尸回收必须**先**做：崩溃会留下 extract_status='running' 的记录，
            # 不被回收就永远不会再被领取（领取条件是 pending）。
            # 这在真机上确实发生过——一条记录卡在 running。
            stats.zombies_reclaimed = self._messages.reclaim_zombies(
                older_than_minutes=self._settings.extract_zombie_minutes
            )
            # 已达尝试上限的直接转终态，避免每轮重复尝试（烧钱）
            stats.attempts_exhausted = self._reclaim_exhausted()

        pending = self._messages.list_pending_extract(
            self._account, limit=limit or 200
        )
        stats.considered = len(pending)

        for index, record in enumerate(pending, start=1):
            if not apply:
                # dry-run 也走一遍抽取以便报告候选，但不写库、不改状态
                self._process(record, stats, apply=False)
                self._reporter.progress("extract", index, len(pending))
                continue
            if not self._messages.claim_for_extract(record.id):
                continue  # 被其它进程领走
            self._process(record, stats, apply=True)
            # 逐封上报：抽取可能调用 LLM，每封都要等网络往返，
            # 整批下来可能几十秒到几分钟
            self._reporter.progress("extract", index, len(pending))

        return stats

    def _process(self, record, stats: ExtractStats, *, apply: bool) -> None:
        """处理一封邮件。"""

        row = self._conn.execute(
            "SELECT * FROM messages WHERE id = ?", (record.id,)
        ).fetchone()
        if row is None:
            return

        # 库里只存了清洗后的片段（隐私设计），无法重建 ICS 部件。
        # 因此这里用片段做规则抽取；含 ICS 的邮件在同步阶段已记录 has_ics，
        # 真正的 ICS 解析在 P4 接入「按 UID 回取原文」后补全。
        parsed = _synthetic_parsed(row)
        received = parse_iso(row["received_at"]) or utcnow()

        is_contact = self._senders.is_known_contact(
            self._account, row["from_addr"] or ""
        )

        try:
            outcome = extract_from_parsed(
                parsed,
                received_at=received,
                user_timezone=self._settings.user_timezone,
                default_duration_minutes=self._settings.default_event_duration_minutes,
                confidence_auto_push_threshold=self._settings.confidence_auto_push_threshold,
                ambiguous_date_policy=self._settings.ambiguous_date_policy,
                llm=self._llm,
                is_known_contact=is_contact,
                ics_non_contact_auto_push=self._settings.ics_auto_push_non_contact,
                now=utcnow(),
            )
        except Exception as exc:  # noqa: BLE001 - 单封失败不拖垮整轮
            stats.failed += 1
            logger.warning("抽取 UID %s 失败：%s", row["uid"], exc)
            if apply:
                attempts = self._messages.increment_attempts(record.id)
                if attempts >= self._settings.extract_max_attempts:
                    self._messages.set_extract_status(record.id, ExtractStatus.FAILED)
                    stats.attempts_exhausted += 1
                else:
                    self._messages.set_extract_status(record.id, ExtractStatus.PENDING)
            return

        self._record_verdict(row, outcome, stats, apply=apply)

        if not outcome.verdict.extract:
            stats.skipped_by_prefilter += 1
            if apply:
                self._messages.set_extract_status(record.id, ExtractStatus.DONE)
            return

        # LLM 使用统计：区分「跳过」与「失败」——前者是配置/预算问题，
        # 后者是值得排查的错误，混在一起会误导使用者。
        if outcome.llm_called:
            stats.llm_called += 1
            if outcome.llm_ok:
                stats.llm_ok += 1
            else:
                stats.llm_failed += 1
        elif outcome.llm_skipped_reason:
            stats.llm_skipped += 1
            if outcome.llm_skipped_reason not in stats.llm_skipped_reasons:
                stats.llm_skipped_reasons.append(outcome.llm_skipped_reason)

        if outcome.candidates:
            stats.with_candidates += 1
        stats.candidates += len(outcome.candidates)
        stats.auto_pushable += sum(1 for c in outcome.candidates if c.auto_pushable)
        stats.requires_review += sum(1 for c in outcome.candidates if c.requires_review)
        for candidate in outcome.candidates:
            key = candidate.source.value
            stats.by_source[key] = stats.by_source.get(key, 0) + 1

        if apply:
            self._persist(row["id"], outcome, stats)
            self._messages.set_extract_status(record.id, ExtractStatus.DONE)

    def _record_verdict(self, row, outcome: ExtractionOutcome, stats: ExtractStats, *, apply: bool) -> None:
        if not apply:
            return

    def _persist(self, message_id: int, outcome: ExtractionOutcome, stats: ExtractStats) -> None:
        """把候选写入 ``events``，并清理**本次不再产出**的旧候选。

        只写不删会留下孤儿：改进抽取后重跑，新候选按新指纹插入，旧指纹的事件
        仍留在库里。实测这会让同一封邮件的同一时刻出现两条（标题不同），
        审核队列里看起来像两个独立事件。

        **但绝不能无条件删。** 保护人的判断与外部副作用是硬约束，因此
        只有同时满足以下条件的旧候选才可删除：

        * 状态仍是 ``pending``（人还没表态）
        * ``manual_edited = 0``（人没改过它）
        * ``gcal_event_id IS NULL``（没有写进日历，删掉不会造成外部不一致）
        * 没有 ``dispatched`` 的延迟窗口记录（确实推送过）

        不满足的保留不动——删除一个已批准/已推送的事件等于**销毁人的决定**
        与日历中的实际条目。这类孤儿单列统计，让人自己决定怎么处理。
        """
        persisted: set[str] = set()
        for candidate in outcome.candidates:
            before = self._conn.execute(
                "SELECT id FROM events WHERE message_id = ? AND fingerprint = ?",
                (message_id, candidate.fingerprint),
            ).fetchone()
            persisted.add(candidate.fingerprint)
            self._events.upsert_candidate(
                message_id=message_id,
                title=candidate.title,
                start_ts=candidate.start_ts,
                end_ts=candidate.end_ts,
                all_day=candidate.all_day,
                source=candidate.source,
                confidence=candidate.confidence,
                fingerprint=candidate.fingerprint,
                evidence=candidate.evidence,
                location=candidate.location,
                organizer=candidate.organizer,
                tz=candidate.tz,
                ics_uid=candidate.ics_uid,
                ics_sequence=candidate.ics_sequence,
                ics_recurrence_id=candidate.ics_recurrence_id,
                ics_rrule=candidate.ics_rrule,
                requires_review=candidate.requires_review,
                review_reason=candidate.review_reason,
            )
            if before is None:
                stats.events_inserted += 1
            else:
                stats.events_updated += 1
                # 已被人工审批的事件不会被覆盖，记录下来以便使用者知道
                row = self._conn.execute(
                    "SELECT status FROM events WHERE id = ?", (before["id"],)
                ).fetchone()
                if row and row["status"] in {
                    "approved", "rejected", "ignored", "pushed"
                }:
                    stats.events_protected += 1

        self._remove_orphans(message_id, persisted, stats)

    #: 可以安全删除的旧候选所需的状态。
    #:
    #: 只有 ``pending`` 是「机器产出、人未表态」。其余状态都承载了人的决定
    #: （approve/reject/ignore）或外部副作用（pushed），绝不能因重跑而消失。
    _REMOVABLE_STATUSES = ("pending",)

    def _remove_orphans(
        self, message_id: int, persisted: set[str], stats: ExtractStats
    ) -> None:
        """删掉本次重跑不再产出的旧候选（仅限可安全删除的）。

        删除前先取消其延迟窗口（``scheduled_pushes``）：否则一个已排队的
        推送会指向不存在的事件，调度器处理时就会出错。
        """
        rows = self._conn.execute(
            "SELECT id, status, manual_edited, gcal_event_id, fingerprint, source "
            "FROM events WHERE message_id = ?",
            (message_id,),
        ).fetchall()

        for row in rows:
            if row["fingerprint"] in persisted:
                continue
            if row["status"] not in self._REMOVABLE_STATUSES:
                stats.events_orphaned += 1  # 人已表态/已入历，保留不动
                continue
            if row["manual_edited"]:
                stats.events_orphaned += 1  # 人改过，保留
                continue
            if row["gcal_event_id"]:
                stats.events_orphaned += 1  # 已写进日历，删掉会造成外部不一致
                continue
            if row["source"] == "llm" and not self._llm_usable():
                # 本轮没有 LLM 可用，因此**无法复现** LLM 抽出的结果。
                # 「这次没产出」不代表它已经不对——只是在离线模式下看不见。
                # 不加这条保护，一次不带 LLM 的重跑就会悄悄抹掉此前花代价
                # 换来的 LLM 结果。
                stats.events_orphaned += 1
                continue
            if self._has_dispatched_push(row["id"]):
                # 延迟窗口已经派发过 → 说明确实推送过。此时即使 gcal_event_id
                # 因故为空，也不该删（外部可能已有条目）。
                stats.events_orphaned += 1
                continue

            # scheduled_pushes.event_id 有外键约束，且事件一旦删除其排队项就
            # 失去意义，因此先把队列行清掉再删事件。
            self._conn.execute(
                "DELETE FROM scheduled_pushes WHERE event_id = ?", (row["id"],)
            )
            self._conn.execute("DELETE FROM events WHERE id = ?", (row["id"],))
            stats.events_removed += 1

    def _llm_usable(self) -> bool:
        """本轮是否真的能用 LLM（缺凭据时不可用 → 离线模式）。"""
        if self._llm is None:
            return False
        return getattr(self._llm, "available", True) is not False

    def _has_dispatched_push(self, event_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM scheduled_pushes WHERE event_id = ? AND state = 'dispatched' "
            "LIMIT 1",
            (event_id,),
        ).fetchone()
        return row is not None

    def _reclaim_exhausted(self) -> int:
        """把已达尝试上限的邮件直接标为终态，避免每轮重复尝试（烧钱）。"""
        rows = self._conn.execute(
            "SELECT id, extract_attempts FROM messages "
            "WHERE account = ? AND extract_status = 'pending' AND extract_attempts >= ?",
            (self._account, self._settings.extract_max_attempts),
        ).fetchall()
        for row in rows:
            self._messages.set_extract_status(row["id"], ExtractStatus.FAILED)
        return len(rows)


def _synthetic_parsed(row) -> object:
    """用库里的清洗片段构造一个 :class:`ParsedMail`。

    为什么不存完整原文：隐私设计（规格 §12）——库里只保留脱敏片段。
    代价是**离线重解析无法拿到 ICS 部件**，因此当前实现中 ICS 抽取依赖
    同步阶段就能拿到原文。P4 会补上「按 UID 回取原文」的路径。
    """
    from ..mail.mime import BodyResult, ParsedMail

    excerpt = row["body_excerpt"] or ""
    return ParsedMail(
        subject=row["subject"] or "",
        from_addr=row["from_addr"] or "",
        from_name=row["from_name"] or "",
        to_addrs=[],
        message_id=row["normalized_message_id"],
        in_reply_to=row["in_reply_to"],
        references=[],
        sent_at=row["sent_at"],
        has_ics=bool(row["has_ics"]),
        has_unsubscribe=bool(row["has_unsubscribe"]),
        unsubscribe_mailto=None,
        unsubscribe_links=[],
        list_id=row["list_id"],
        auto_submitted=row["auto_submitted"],
        body=BodyResult(
            text=excerpt,
            full_text=excerpt,
            sha256=row["body_sha256"] or "",
        ),
        ics_parts=[],
    )
