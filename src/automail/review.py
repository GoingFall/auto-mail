"""审核队列：事件审批与延迟推送调度。

## 状态机（docs/spec-event-state-machine.md §3）

```
pending ─approve→ approved ─→ [队列 queued] ─dispatch→ pushed
   ├─reject→ rejected（终态）
   ├─ignore→ ignored（终态，同 fingerprint 不再入队）
   └─edit  → approved（该字段 provenance 记 human）
```

**``events.status`` 是唯一状态源。** ``scheduled_pushes`` 只是调度队列，
其 ``state`` 不属于事件状态机。

## 为什么人工审批要放在队列里而不是立即执行

审批是「人做出判断」，判断本身不应被技术故障吞掉。因此审批只改状态
（``pending → approved``），**推送是独立的一步**（``push`` 命令）。
这样即使推送时断网，审批结果也不会丢失，下次运行继续。

## 延迟窗口只作用于自动事件

人工 ``approve`` 是显式动作，应当**立即**（在下次 push 时）执行；
自动白名单事件才进延迟窗口，用途是给自动行为一次复核机会。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import parse_iso, utcnow, utcnow_iso
from .models import EventStatus, ScheduledPushState

logger = logging.getLogger("automail.review")


class ReviewError(Exception):
    """审批操作失败。"""


@dataclass(slots=True)
class ReviewItem:
    """审核界面的一条待办。"""

    event_id: int
    title: str
    start_ts: str | None
    end_ts: str | None
    all_day: bool
    source: str
    confidence: float | None
    status: str
    evidence: str
    review_reason: str
    mail_subject: str | None
    mail_from: str | None
    mail_received: str | None
    fingerprint: str
    conflicts_with: list[int] = field(default_factory=list)
    """与之冲突的其它事件 id（同邮件内时间分歧），审核时成组展示。"""

    ics_uid: str | None = None
    ics_sequence: int | None = None
    snapshot_diff: dict[str, Any] | None = None
    """冻结态下的差异明细（仅供展示）。"""

    sibling_ids: list[int] = field(default_factory=list)
    """同一天、同一活动的其它环节（如「迎迓 10:00」与「升旗禮 10:30」）。

    它们**不是**互相矛盾，各自都应保留；提示出来只是让人知道这几条相关。
    """

    probable_duplicate_of: int | None = None
    """疑似与哪条事件重复（仅提示，**不影响**其可批准性）。"""

    duplicate_similarity: float | None = None
    """与疑似重复项的标题相似度（0~1），供使用者判断。"""


@dataclass(slots=True)
class ReviewStats:
    """审批操作的统计。"""

    requested: int = 0
    changed: int = 0
    skipped: int = 0
    not_found: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "changed": self.changed,
            "skipped": self.skipped,
            "not_found": self.not_found,
        }


#: 允许从 ``pending`` 直接转移到的目标状态（审批动作）
_ALLOWED_APPROVE_FROM = {EventStatus.PENDING.value}
_ALLOWED_REJECT_FROM = {EventStatus.PENDING.value}
_ALLOWED_IGNORE_FROM = {EventStatus.PENDING.value}


def parse_event_ids(raw: list[str]) -> list[int]:
    """解析审批命令的事件 id 参数。

    支持 ``1,2,3`` 与 ``1-5`` 两种写法（批量审批几十条是常态）。
    非法输入抛 :class:`ReviewError`，不做静默忽略——静默忽略会让用户
    以为「已处理」而实际没有。
    """
    result: list[int] = []
    for chunk in raw:
        for part in str(chunk).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                left, _, right = part.partition("-")
                try:
                    start, end = int(left), int(right)
                except ValueError as exc:
                    raise ReviewError(f"无法解析范围 {part!r}") from exc
                if start > end:
                    start, end = end, start
                result.extend(range(start, end + 1))
            else:
                try:
                    result.append(int(part))
                except ValueError as exc:
                    raise ReviewError(f"无法解析事件 id {part!r}") from exc
    # 去重且保序
    seen: dict[int, None] = {}
    for value in result:
        seen.setdefault(value, None)
    return list(seen)


class ReviewQueue:
    """审核队列的读写与状态转移。

    所有写操作都在**事务内**完成状态检查与更新，避免「查完再改」之间被
    另一个进程插入（虽然本项目的单实例锁已降低该风险，但正确性不该依赖它）。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 查询 ──────────────────────────────────────────────

    def list_items(
        self,
        *,
        statuses: tuple[EventStatus, ...] = (EventStatus.PENDING,),
        limit: int = 100,
        since_days: int | None = None,
    ) -> list[ReviewItem]:
        """列出指定状态的事件。

        ``since_days`` 用于「只看最近 N 天进来的候选」，避免历史积压淹没视野。
        """
        placeholders = ",".join("?" for _ in statuses)
        params: list[Any] = [s.value for s in statuses]

        sql = f"""
            SELECT e.*, m.subject AS mail_subject, m.from_addr AS mail_from,
                   m.received_at AS mail_received
              FROM events e LEFT JOIN messages m ON m.id = e.message_id
             WHERE e.status IN ({placeholders})
        """
        if since_days is not None:
            cutoff = (utcnow() - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            sql += " AND e.created_at >= ?"
            params.append(cutoff)

        # 待审优先按时间正序（快到期的在前），已批准的也按时间排
        sql += " ORDER BY e.start_ts ASC, e.id ASC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._to_item(row) for row in rows]

    def get_item(self, event_id: int) -> ReviewItem | None:
        row = self._conn.execute(
            """
            SELECT e.*, m.subject AS mail_subject, m.from_addr AS mail_from,
                   m.received_at AS mail_received
              FROM events e LEFT JOIN messages m ON m.id = e.message_id
             WHERE e.id = ?
            """,
            (event_id,),
        ).fetchone()
        return self._to_item(row) if row else None

    def _to_item(self, row: sqlite3.Row) -> ReviewItem:
        review_reason = ""
        evidence = row["evidence"] or ""
        # 抽取阶段把待审原因写进 evidence 前缀（events 表没有独立列）
        if evidence.startswith("[待审：") and "]" in evidence:
            closing = evidence.index("]")
            review_reason = evidence[len("[待审："): closing]
            evidence = evidence[closing + 1:].strip()

        snapshot_diff = None
        if row["status"] in {
            EventStatus.EXTERNALLY_MODIFIED.value,
            EventStatus.CONFLICT.value,
        }:
            snapshot_diff = self._load_snapshot_diff(row)

        conflicts, siblings = self._find_siblings(
            row["message_id"], row["id"], row["start_ts"]
        )

        return ReviewItem(
            event_id=row["id"],
            title=row["title"] or "",
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            all_day=bool(row["all_day"]),
            source=row["source"],
            confidence=row["confidence"],
            status=row["status"],
            evidence=evidence,
            review_reason=review_reason,
            mail_subject=row["mail_subject"] if "mail_subject" in row.keys() else None,
            mail_from=row["mail_from"] if "mail_from" in row.keys() else None,
            mail_received=row["mail_received"] if "mail_received" in row.keys() else None,
            fingerprint=row["fingerprint"],
            conflicts_with=conflicts,
            sibling_ids=siblings,
            ics_uid=row["ics_uid"],
            ics_sequence=row["ics_sequence"],
            snapshot_diff=snapshot_diff,
        )

    def _load_snapshot_diff(self, row: sqlite3.Row) -> dict[str, Any] | None:
        """加载冻结态下需要展示的信息。

        诚实说明：v1 只保存**快照** payload，不保存远端原文（远端内容随时可变，
        缓存它意义不大且增加隐私面）。因此这里给出「我方原本是什么」，
        远端当前值由 ``push`` 时实时读取并展示。
        """
        raw = None
        if "snapshot_payload" in row.keys():
            raw = row["snapshot_payload"]
        if not raw:
            return None
        try:
            snapshot = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(snapshot, dict):
            return None
        return {"snapshot": snapshot}

    #: 判定「真冲突」的时刻差阈值（分钟）。
    #:
    #: 同一封邮件里的两个时间**未必**互相矛盾——邀请函常写多个环节：
    #: 实测「迎迓 10:00」与「升旗禮 10:30」是同一活动的前后两步。
    #: 把它们标成「冲突」会让人以为必须二选一，反而误导。
    #: 只有时刻分歧超过这个阈值（无法同时成立）才算真冲突。
    CONFLICT_MINUTES = 60

    def _find_siblings(
        self, message_id: int | None, event_id: int, start_ts: str | None
    ) -> tuple[list[int], list[int]]:
        """找同一封邮件内同一天的其它候选，分成「真冲突」与「同期兄弟」。

        返回 ``(冲突 ids, 兄弟 ids)``：

        * **冲突**：时刻分歧超过 :data:`CONFLICT_MINUTES`，无法同时成立
          （例如一封邮件里写「改到 9/24」又写「原定 9/20」，需人裁决）
        * **兄弟**：同一天但时刻接近，是同一活动的多个环节
          （迎迓 10:00 + 升旗禮 10:30），各自都该保留

        全天候选与定时候选同天时也算兄弟（不构成冲突）。

        取数用「本地日 ±1 天」的 UTC 区间，而不是 ``start_ts`` 的**字符串
        前缀**——后者是 UTC 自然日，与本地日可能差一天（本地 10-01 00:30
        存成 ``2026-09-30T16:30Z``），按前缀取数会漏掉同组的兄弟。
        """
        if not message_id or not start_ts:
            return [], []

        mine = parse_iso(start_ts)
        if mine is None:
            return [], []
        tz = ZoneInfo("Asia/Shanghai")
        my_local = mine.astimezone(tz)

        rows = self._conn.execute(
            "SELECT id, start_ts, all_day FROM events WHERE message_id = ? AND id != ? "
            "AND start_ts >= ? AND start_ts < ?",
            (
                message_id,
                event_id,
                (my_local.date() - timedelta(days=1)).isoformat(),
                (my_local.date() + timedelta(days=2)).isoformat(),
            ),
        ).fetchall()

        conflicts: list[int] = []
        siblings: list[int] = []
        for row in rows:
            other = parse_iso(row["start_ts"])
            if other is None:
                continue
            other_local = other.astimezone(tz)
            if other_local.date() != my_local.date():
                continue  # UTC 日期相同但本地不同日，不算同组
            if row["all_day"]:
                siblings.append(int(row["id"]))  # 全天 vs 定时：不冲突
                continue
            delta = abs((other_local - my_local).total_seconds()) / 60
            if delta > self.CONFLICT_MINUTES:
                conflicts.append(int(row["id"]))
            else:
                siblings.append(int(row["id"]))
        return conflicts, siblings

    # ── 状态转移 ──────────────────────────────────────────

    def approve(self, event_ids: list[int]) -> ReviewStats:
        """批准事件。

        批准**不立即写入日历**——推送由 ``push`` 单独执行。这样即使推送时
        断网，人的判断也不会丢失。
        """
        return self._transition(
            event_ids,
            target=EventStatus.APPROVED,
            allowed_from=_ALLOWED_APPROVE_FROM,
            action="approve",
        )

    def reject(self, event_ids: list[int]) -> ReviewStats:
        """否决事件（终态，不再提示）。"""
        return self._transition(
            event_ids,
            target=EventStatus.REJECTED,
            allowed_from=_ALLOWED_REJECT_FROM,
            action="reject",
        )

    def ignore(self, event_ids: list[int]) -> ReviewStats:
        """忽略事件（终态；同 fingerprint 不再入队）。"""
        return self._transition(
            event_ids,
            target=EventStatus.IGNORED,
            allowed_from=_ALLOWED_IGNORE_FROM,
            action="ignore",
        )

    def _transition(
        self,
        event_ids: list[int],
        *,
        target: EventStatus,
        allowed_from: set[str],
        action: str,
    ) -> ReviewStats:
        """在事务内做状态检查与转移。

        只有处于 ``allowed_from`` 的事件会被改动；其余计入 ``skipped`` 并
        给出原因——**不静默忽略**，否则用户会以为操作生效了。
        """
        stats = ReviewStats(requested=len(event_ids))
        now = utcnow_iso()

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for event_id in event_ids:
                row = self._conn.execute(
                    "SELECT id, status, title FROM events WHERE id = ?", (event_id,)
                ).fetchone()
                if row is None:
                    stats.not_found += 1
                    stats.reasons.append(f"#{event_id} 不存在")
                    continue
                if row["status"] == target.value:
                    stats.skipped += 1
                    stats.reasons.append(f"#{event_id} 已是 {target.value}")
                    continue
                if row["status"] not in allowed_from:
                    stats.skipped += 1
                    stats.reasons.append(
                        f"#{event_id} 当前为 {row['status']}，不允许 {action}"
                    )
                    continue

                self._conn.execute(
                    "UPDATE events SET status = ?, needs_attention = 0, updated_at = ? "
                    "WHERE id = ?",
                    (target.value, now, event_id),
                )
                if target is EventStatus.APPROVED:
                    # 人工批准的事件：不走延迟窗口（人的动作应立即生效）
                    self._cancel_scheduled(event_id, reason="人工批准改为立即推送")
                stats.changed += 1
        finally:
            self._conn.execute("COMMIT")

        return stats

    def edit(
        self, event_id: int, *, title: str | None = None, start_ts: str | None = None
    ) -> ReviewStats:
        """人工修正事件内容，并置为已批准。

        被编辑的字段记录在 ``field_provenance``（``human``），
        使后续 ICS 更新不会静默覆盖用户的修正（规格 §2④）。
        """
        stats = ReviewStats(requested=1)
        row = self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            stats.not_found = 1
            stats.reasons.append(f"#{event_id} 不存在")
            return stats

        sets: list[str] = []
        params: list[Any] = []
        provenance = _load_provenance(row["field_provenance"])

        if title is not None:
            sets.append("title = ?")
            params.append(title)
            provenance["title"] = "human"
        if start_ts is not None:
            sets.append("start_ts = ?")
            params.append(start_ts)
            provenance["start_ts"] = "human"

        if not sets:
            stats.skipped = 1
            stats.reasons.append("未提供任何要修改的字段")
            return stats

        sets.extend(["status = ?", "manual_edited = 1", "field_provenance = ?",
                     "needs_attention = 0", "updated_at = ?"])
        params.extend([
            EventStatus.APPROVED.value,
            json.dumps(provenance, ensure_ascii=False),
            utcnow_iso(),
            event_id,
        ])

        self._conn.execute(f"UPDATE events SET {', '.join(sets)} WHERE id = ?", params)
        stats.changed = 1
        return stats

    def adopt(self, event_id: int) -> ReviewStats:
        """接管被外部修改的事件：以**当前远端内容**为新基线。

        语义（规格 §3）：重算 ``snapshot_hash = remote_norm_hash``，
        把 ``field_provenance`` 全部字段标为 ``human``（承认用户的改动），
        ``managed_state=adopted``，状态回到 ``approved``（解冻）。

        回到 ``approved`` 而非 ``pushed``：解冻后仍需一次显式 ``push``
        才会写入，避免 adopt 本身带来副作用。
        """
        stats = ReviewStats(requested=1)
        row = self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            stats.not_found = 1
            stats.reasons.append(f"#{event_id} 不存在")
            return stats

        if row["status"] not in {
            EventStatus.EXTERNALLY_MODIFIED.value,
            EventStatus.CONFLICT.value,
        }:
            stats.skipped = 1
            stats.reasons.append(
                f"#{event_id} 当前为 {row['status']}，仅冻结态可 adopt"
            )
            return stats

        provenance = _load_provenance(row["field_provenance"])
        # 承认远端内容为人工成果：所有已知字段标 human
        for field_name in ("title", "start_ts", "end_ts", "location"):
            provenance[field_name] = "human"

        self._conn.execute(
            """
            UPDATE events SET
                snapshot_hash = COALESCE(remote_norm_hash, snapshot_hash),
                snapshot_payload = NULL,
                field_provenance = ?,
                managed_state = 'adopted',
                status = ?,
                needs_attention = 0,
                last_checked_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(provenance, ensure_ascii=False),
                EventStatus.APPROVED.value,
                utcnow_iso(),
                utcnow_iso(),
                event_id,
            ),
        )
        stats.changed = 1
        return stats

    def retry(self, event_ids: list[int]) -> ReviewStats:
        """把推送失败的事件重新置为已批准，等待下一次推送。"""
        stats = ReviewStats(requested=len(event_ids))
        now = utcnow_iso()
        for event_id in event_ids:
            row = self._conn.execute(
                "SELECT status FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                stats.not_found += 1
                stats.reasons.append(f"#{event_id} 不存在")
                continue
            if row["status"] not in {
                EventStatus.PUSH_FAILED.value,
                EventStatus.UNCERTAIN.value,
                EventStatus.MISSING.value,
            }:
                stats.skipped += 1
                stats.reasons.append(f"#{event_id} 当前为 {row['status']}，无需重试")
                continue
            self._conn.execute(
                "UPDATE events SET status = ?, push_attempts = 0, "
                "needs_attention = 0, updated_at = ? WHERE id = ?",
                (EventStatus.APPROVED.value, now, event_id),
            )
            stats.changed += 1
        return stats

    # ── 延迟推送队列 ──────────────────────────────────────

    def schedule_push(self, event_id: int, *, delay_minutes: int) -> int:
        """把事件放入延迟推送队列。

        **只对自动白名单事件调用**：人工批准的事件不走延迟窗口。
        """
        target = (utcnow() + timedelta(minutes=delay_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        cur = self._conn.execute(
            """
            INSERT INTO scheduled_pushes (event_id, scheduled_for, state, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (event_id, target, ScheduledPushState.QUEUED.value, utcnow_iso()),
        )
        return int(cur.lastrowid)

    def due_pushes(self, *, limit: int = 100) -> list[tuple[int, int]]:
        """取出到点的调度项，返回 ``[(队列id, 事件id), ...]``。"""
        now = utcnow_iso()
        rows = self._conn.execute(
            "SELECT id, event_id FROM scheduled_pushes "
            "WHERE state = ? AND scheduled_for <= ? ORDER BY scheduled_for ASC LIMIT ?",
            (ScheduledPushState.QUEUED.value, now, limit),
        ).fetchall()
        return [(int(r["id"]), int(r["event_id"])) for r in rows]

    def pending_window(self, *, within_minutes: int = 60) -> list[ReviewItem]:
        """列出延迟窗口内**即将自动入历**的事件。

        摘要需要它——否则窗口没有可撤销的对象，`--cancel` 形同虚设。
        """
        horizon = (utcnow() + timedelta(minutes=within_minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows = self._conn.execute(
            """
            SELECT e.*, s.scheduled_for AS scheduled_for
              FROM scheduled_pushes s JOIN events e ON e.id = s.event_id
             WHERE s.state = ? AND s.scheduled_for <= ?
             ORDER BY s.scheduled_for ASC
            """,
            (ScheduledPushState.QUEUED.value, horizon),
        ).fetchall()
        return [self._to_item(row) for row in rows]

    def cancel_push(self, key: str) -> ReviewStats:
        """撤销延迟窗口内的推送。

        ``key`` 可以是队列 id 或事件 id。撤销后**事件状态回退为 pending**
        （规格 §0-D）：用户收回了自动推送的授权，因此需要重新显式批准。
        回到 ``approved`` 会被自动流程再次消费，等于撤销无效。
        """
        stats = ReviewStats(requested=1)
        row = None
        if key.isdigit():
            row = self._conn.execute(
                "SELECT * FROM scheduled_pushes WHERE id = ?", (int(key),)
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT * FROM scheduled_pushes WHERE event_id = ? AND state = ?",
                    (int(key), ScheduledPushState.QUEUED.value),
                ).fetchone()
        if row is None:
            stats.not_found = 1
            stats.reasons.append(f"未找到待撤销的推送项 {key!r}")
            return stats

        if row["state"] != ScheduledPushState.QUEUED.value:
            stats.skipped = 1
            stats.reasons.append(f"该项已处于 {row['state']}，无法撤销")
            return stats

        now = utcnow_iso()
        self._conn.execute(
            "UPDATE scheduled_pushes SET state = ? WHERE id = ?",
            (ScheduledPushState.CANCELLED.value, row["id"]),
        )
        self._conn.execute(
            "UPDATE events SET status = ?, needs_attention = 0, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (EventStatus.PENDING.value, now, row["event_id"], EventStatus.APPROVED.value),
        )
        stats.changed = 1
        return stats

    def mark_dispatched(
        self, queue_id: int, *, state: ScheduledPushState, error: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE scheduled_pushes SET state = ?, dispatched_at = ?, error = ? "
            "WHERE id = ?",
            (state.value, utcnow_iso(), error, queue_id),
        )

    def _cancel_scheduled(self, event_id: int, *, reason: str) -> None:
        self._conn.execute(
            "UPDATE scheduled_pushes SET state = ? WHERE event_id = ? AND state = ?",
            (ScheduledPushState.CANCELLED.value, event_id, ScheduledPushState.QUEUED.value),
        )

    # ── 统计 ──────────────────────────────────────────────

    def summary(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) n FROM events GROUP BY status"
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def frozen_items(self) -> list[ReviewItem]:
        """列出冻结态事件（需要 adopt 或 reject 才能继续）。"""
        return self.list_items(
            statuses=(EventStatus.EXTERNALLY_MODIFIED, EventStatus.CONFLICT),
            limit=200,
        )

    def needs_attention_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) n FROM events WHERE needs_attention = 1"
        ).fetchone()
        return int(row["n"]) if row else 0

    def upcoming_auto_pushes(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """列出所有排队的自动推送（含未到点的），供摘要提示。"""
        rows = self._conn.execute(
            """
            SELECT s.id AS queue_id, s.scheduled_for, e.id AS event_id,
                   e.title, e.start_ts
              FROM scheduled_pushes s JOIN events e ON e.id = s.event_id
             WHERE s.state = ?
             ORDER BY s.scheduled_for ASC LIMIT ?
            """,
            (ScheduledPushState.QUEUED.value, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def _load_provenance(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def is_auto_pushable_event(row: sqlite3.Row, threshold: float) -> bool:
    """判断一个抽取结果是否属于「自动入历白名单」。

    规格 §2：``source=ics`` 且 ``METHOD:REQUEST`` 且 organizer 非空且起止可解析；
    或 ``source=rules`` 且 ``confidence > threshold`` 且非模糊。

    注意这里读的是 ``needs_attention``（抽取阶段已把「需人工审核」编码进去），
    因此实现是：未被标记需要关注 + 来源可信 + 置信度**严格大于**阈值。
    """
    if row["needs_attention"]:
        return False
    if not row["start_ts"]:
        return False
    source = row["source"]
    if source == "ics":
        return bool(row["ics_uid"])
    if source == "rules":
        confidence = row["confidence"] or 0.0
        return confidence > threshold
    return False


# ──────────────────────────────────────────────────────────────
# 「可能重复」提示
# ──────────────────────────────────────────────────────────────

#: 触发提示的标题相似度阈值。
#:
#: 刻意设得较高：这只是提示，不是合并，但误报太多会让提示失去信任。
#: 实测中同一发件人的多笔独立交易（ZA Card 消费、不同八达通卡操作）
#: 标题相似度达 1.00——所以提示会命中它们。这是**期望行为**：
#: 它提示使用者「这几条看起来一样，请确认是否真是独立事件」，
#: 而不是替他做决定。
DUPLICATE_HINT_THRESHOLD = 0.8


def annotate_probable_duplicates(
    items: list[ReviewItem], *, threshold: float = DUPLICATE_HINT_THRESHOLD
) -> list[ReviewItem]:
    """给条目加上「可能重复」提示（**不合并、不隐藏任何候选**）。

    ## 为什么只提示不合并

    实测过自动合并，结论是**不安全**：

    * ZA Card 三笔消费：同一分钟内两条、4 分钟后一条，标题完全相同
      → 相似度 1.00，但它们可能是**三笔独立交易**
    * 八達通三条通知：日期相同、标题高度相似，但提到**不同的卡号**
      → 合并会静默丢掉两张卡的操作记录

    误合并的代价是**静默丢失真实信息**，比留下重复候选严重得多。
    因此只标注，由使用者判断（确认是重复可用 `events reject 31,32,33` 批量否决）。

    判据：**本地日期相同** + 标题相似度超阈值。日期必须相同——
    同一发件人不同日期的通知（周期性提醒）不构成重复嫌疑。
    """
    from zoneinfo import ZoneInfo

    from .db import parse_iso
    from .extract.fingerprint import title_similarity

    tz = ZoneInfo("Asia/Shanghai")

    def local_day(item: ReviewItem) -> str | None:
        parsed = parse_iso(item.start_ts)
        # 用**本地**日期而非 UTC：UTC 00:00 的候选在东八区是当天早上，
        # 用 UTC 日期分组会把「同一天发生的事」拆到两个组里。
        return parsed.astimezone(tz).strftime("%Y-%m-%d") if parsed else None

    days = [local_day(item) for item in items]
    for index, item in enumerate(items):
        if days[index] is None:
            continue
        for earlier_index in range(index):
            if days[earlier_index] != days[index]:
                continue
            score = title_similarity(item.title, items[earlier_index].title)
            if score >= threshold:
                item.probable_duplicate_of = items[earlier_index].event_id
                item.duplicate_similarity = round(score, 2)
                break
    return items
