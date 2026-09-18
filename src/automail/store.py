"""邮件域的仓库层：messages / processed_mail / sync_state / senders。

为什么与 :mod:`automail.db` 分开：``db`` 管的是**基础设施**（连接、迁移、备份、
锁、运行记录），本模块管的是**邮件领域的读写**。混在一起会让 db.py 无限膨胀，
且基础设施的读者被迫面对邮件字段细节。

三层账本的职责在这里落地（规格 §5，docs/spec-imap-sync.md §3）：

* ``processed_mail``  —— **只**管 sync 层：这封邮件是否已抓取入库
* ``messages.extract_status`` —— extract 层的进度
* ``messages.extract_attempts`` —— **只**统计 LLM 调用次数，防毒邮件持续烧钱
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .db import iso, utcnow, utcnow_iso
from .models import (
    EventSource,
    EventStatus,
    ExtractStatus,
    ProcessedMailStatus,
    SyncState,
)


@dataclass(slots=True)
class MessageRecord:
    """``messages`` 表的一行（仅列出同步流程需要的字段）。"""

    id: int
    account: str
    folder: str
    uid_validity: int
    uid: int
    normalized_message_id: str | None
    is_canonical: int
    duplicate_of: int | None
    stale: int
    folder_moved: int
    body_sha256: str | None
    extract_status: str
    extract_attempts: int


class SyncStateRepository:
    """``sync_state``：每个 account+folder 的增量游标。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, account: str, folder: str) -> SyncState | None:
        row = self._conn.execute(
            "SELECT * FROM sync_state WHERE account = ? AND folder = ?",
            (account, folder),
        ).fetchone()
        if row is None:
            return None
        return SyncState(
            account=row["account"],
            folder=row["folder"],
            uid_validity=row["uid_validity"],
            highest_uid=row["highest_uid"],
            syncs_since_full=row["syncs_since_full"],
            last_sync_at=row["last_sync_at"],
            last_full_sync_at=row["last_full_sync_at"],
        )

    def upsert(
        self,
        account: str,
        folder: str,
        *,
        uid_validity: int,
        highest_uid: int,
        syncs_since_full: int,
        mark_synced: bool = True,
        mark_full: bool = False,
    ) -> None:
        now = utcnow_iso()
        self._conn.execute(
            """
            INSERT INTO sync_state (
                account, folder, uid_validity, highest_uid,
                syncs_since_full, last_sync_at, last_full_sync_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, folder) DO UPDATE SET
                uid_validity      = excluded.uid_validity,
                highest_uid       = excluded.highest_uid,
                syncs_since_full  = excluded.syncs_since_full,
                last_sync_at      = COALESCE(excluded.last_sync_at, sync_state.last_sync_at),
                last_full_sync_at = COALESCE(excluded.last_full_sync_at,
                                             sync_state.last_full_sync_at)
            """,
            (
                account,
                folder,
                uid_validity,
                highest_uid,
                syncs_since_full,
                now if mark_synced else None,
                now if mark_full else None,
            ),
        )

    def should_compensate(
        self, account: str, folder: str, *, scans: int, hours: int = 24
    ) -> bool:
        """是否该做补偿扫描：增量次数达阈值，或距上次全量超过 N 小时。"""
        state = self.get(account, folder)
        if state is None:
            # 首次同步本身就是一次全量
            return False
        if state.syncs_since_full >= scans:
            return True
        last_full = state.last_full_sync_at
        if not last_full:
            return False
        from datetime import timedelta

        from .db import parse_iso

        parsed = parse_iso(last_full)
        if parsed is None:
            return True
        return utcnow() - parsed > timedelta(hours=hours)


class MessageRepository:
    """``messages``：邮件的落库、查重与状态更新。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── 查询 ──────────────────────────────────────────────

    def find_by_uid(
        self, account: str, folder: str, uid_validity: int, uid: int
    ) -> MessageRecord | None:
        row = self._conn.execute(
            "SELECT * FROM messages WHERE account=? AND folder=? "
            "AND uid_validity=? AND uid=?",
            (account, folder, uid_validity, uid),
        ).fetchone()
        return _to_record(row)

    def find_reusable(
        self,
        account: str,
        folder: str,
        *,
        body_sha256: str | None,
        normalized_message_id: str | None,
    ) -> MessageRecord | None:
        """查找可复用的旧记录（UIDVALIDITY 变化或邮件被移动后的 reactivate）。

        **只返回 ``stale`` 或 ``folder_moved`` 的记录**。这一点很关键：

        * 若把正常记录也算作「可复用」，那么同一封邮件在两个文件夹各有一份副本时，
          第二份会被误判成「旧记录复活」而跳过入库——副本反而丢了。
        * 副本的正确处理是走 ``duplicate_of`` 标记（见 ``find_duplicate_elsewhere``）。

        匹配顺序：先按 ``body_sha256``（内容相同即为同一封），再按规范化 Message-ID。
        """
        for column, value in (
            ("body_sha256", body_sha256),
            ("normalized_message_id", normalized_message_id),
        ):
            if not value:
                continue
            row = self._conn.execute(
                f"SELECT * FROM messages WHERE account=? AND folder=? AND {column}=? "
                "AND (stale=1 OR folder_moved=1) ORDER BY id ASC LIMIT 1",
                (account, folder, value),
            ).fetchone()
            if row is not None:
                return _to_record(row)
        return None

    def find_duplicate_elsewhere(
        self,
        account: str,
        *,
        body_sha256: str | None,
        normalized_message_id: str | None,
        exclude_id: int | None = None,
    ) -> MessageRecord | None:
        """查找其它 folder 里内容相同的邮件（跨文件夹副本）。

        命中即标记为 ``duplicate_of``，**不重跑抽取**。
        """
        clauses: list[str] = []
        params: list[Any] = [account]
        if body_sha256:
            clauses.append("body_sha256 = ?")
            params.append(body_sha256)
        if normalized_message_id:
            clauses.append("normalized_message_id = ?")
            params.append(normalized_message_id)
        if not clauses:
            return None

        sql = (
            f"SELECT * FROM messages WHERE account=? AND ({' OR '.join(clauses)}) "
            "AND is_canonical=1"
        )
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        sql += " ORDER BY id ASC LIMIT 1"

        row = self._conn.execute(sql, params).fetchone()
        return _to_record(row)

    def list_stale(self, account: str, folder: str) -> list[MessageRecord]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE account=? AND folder=? AND stale=1",
            (account, folder),
        ).fetchall()
        return [r for r in (_to_record(row) for row in rows) if r is not None]

    def count_by_status(self, account: str) -> dict[str, int]:
        """按 extract_status 统计，供 stats 与验收使用。"""
        rows = self._conn.execute(
            "SELECT extract_status, COUNT(*) AS n FROM messages "
            "WHERE account=? GROUP BY extract_status",
            (account,),
        ).fetchall()
        return {row["extract_status"]: row["n"] for row in rows}

    # ── 写入 ──────────────────────────────────────────────

    def insert(self, account: str, folder: str, uid_validity: int, uid: int, **fields: Any) -> int:
        """插入一封邮件，返回其内部 id。

        时间字段用 ``fetched_at`` 兜底；``stale``/``folder_moved`` 等整数列
        由调用方显式给出（默认值由 schema 提供）。
        """
        columns = ["account", "folder", "uid_validity", "uid", "fetched_at"]
        values: list[Any] = [account, folder, uid_validity, uid, utcnow_iso()]

        for key, value in fields.items():
            columns.append(key)
            values.append(value)

        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO messages ({', '.join(columns)}) VALUES ({placeholders})"
        cur = self._conn.execute(sql, values)
        return int(cur.lastrowid)

    def reactivate(
        self,
        message_id: int,
        *,
        account: str,
        folder: str,
        uid_validity: int,
        uid: int,
        **fields: Any,
    ) -> None:
        """复活一条旧记录：更新 UID 归属并清除 stale / folder_moved。

        **保留原有事件关联与 extract_status**——这正是避免重复抽取的关键。
        不覆盖 ``id``，因此指向它的 events 记录依旧有效。
        """
        assignments = [
            "account = ?",
            "folder = ?",
            "uid_validity = ?",
            "uid = ?",
            "stale = 0",
            "folder_moved = 0",
            "moved_to_folder = NULL",
            "fetched_at = ?",
        ]
        values: list[Any] = [account, folder, uid_validity, uid, utcnow_iso()]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(message_id)

        self._conn.execute(
            f"UPDATE messages SET {', '.join(assignments)} WHERE id = ?", values
        )

    def update_flags(self, message_id: int, flags: str | None) -> None:
        self._conn.execute(
            "UPDATE messages SET flags = ? WHERE id = ?", (flags, message_id)
        )

    def mark_read_at(self, message_id: int) -> None:
        """记录「本程序把它标为已读」的时刻。

        与 ``flags`` 列刻意分开：``flags`` 反映服务端当前状态（可能是用户在
        别的客户端读的），这一列只记**我们自己的写操作**。回退时据此判断
        哪些能动、哪些不能。
        """
        from .db import utcnow_iso

        self._conn.execute(
            "UPDATE messages SET marked_read_at = ? WHERE id = ?",
            (utcnow_iso(), message_id),
        )

    def clear_marked_read_at(self, message_id: int) -> None:
        """清除标记记录（已恢复为未读）。"""
        self._conn.execute(
            "UPDATE messages SET marked_read_at = NULL WHERE id = ?", (message_id,)
        )

    def mark_stale(self, account: str, folder: str) -> int:
        """把某 folder 下所有记录标 stale（UIDVALIDITY 变化时调用），返回受影响行数。

        **不删除**：保留事件关联，待重扫时按内容匹配复活。
        """
        cur = self._conn.execute(
            "UPDATE messages SET stale = 1 WHERE account=? AND folder=?",
            (account, folder),
        )
        return cur.rowcount

    def mark_folder_moved(self, message_id: int, moved_to: str | None) -> None:
        self._conn.execute(
            "UPDATE messages SET folder_moved = 1, moved_to_folder = ? WHERE id = ?",
            (moved_to, message_id),
        )

    def mark_duplicate(self, message_id: int, canonical_id: int) -> None:
        self._conn.execute(
            "UPDATE messages SET is_canonical = 0, duplicate_of = ? WHERE id = ?",
            (canonical_id, message_id),
        )

    def set_extract_status(self, message_id: int, status: ExtractStatus) -> None:
        self._conn.execute(
            "UPDATE messages SET extract_status = ? WHERE id = ?",
            (status.value, message_id),
        )

    def claim_for_extract(self, message_id: int) -> bool:
        """原子领取抽取任务。

        ``UPDATE ... WHERE extract_status='pending'`` 保证只有真正抢到的进程
        会得到 ``rowcount=1``。这是崩溃安全的领取方式。
        """
        cur = self._conn.execute(
            "UPDATE messages SET extract_status='running' "
            "WHERE id = ? AND extract_status = 'pending'",
            (message_id,),
        )
        return cur.rowcount > 0

    def reclaim_zombies(self, *, older_than_minutes: int = 60) -> int:
        """把超时仍停在 running 的记录回退为 pending。

        进程崩溃会留下 ``running`` 中间态；不回退就会永远卡住，抽取再也不会重试。
        """
        from datetime import timedelta

        cutoff = iso(utcnow() - timedelta(minutes=older_than_minutes))
        cur = self._conn.execute(
            "UPDATE messages SET extract_status='pending' "
            "WHERE extract_status='running' AND (fetched_at IS NULL OR fetched_at <= ?)",
            (cutoff,),
        )
        return cur.rowcount

    def increment_attempts(self, message_id: int) -> int:
        """LLM 调用计数 +1，返回累加后的值。

        独立于 extract_status：即使状态被重置，尝试次数也不清零，
        从而让「每次都失败的毒邮件」在达到上限后停下。
        """
        self._conn.execute(
            "UPDATE messages SET extract_attempts = extract_attempts + 1 WHERE id = ?",
            (message_id,),
        )
        row = self._conn.execute(
            "SELECT extract_attempts FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        return int(row["extract_attempts"]) if row else 0

    def list_pending_extract(self, account: str, *, limit: int = 200) -> list[MessageRecord]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE account=? AND extract_status='pending' "
            "AND is_canonical=1 ORDER BY id ASC LIMIT ?",
            (account, limit),
        ).fetchall()
        return [r for r in (_to_record(row) for row in rows) if r is not None]

    def list_all_for_extract(self, account: str) -> list[MessageRecord]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE account=? ORDER BY id ASC", (account,)
        ).fetchall()
        return [r for r in (_to_record(row) for row in rows) if r is not None]


class ProcessedMailRepository:
    """``processed_mail``：sync 层台账。**只管抓取，不管抽取。**"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def mark(
        self,
        account: str,
        folder: str,
        uid_validity: int,
        uid: int,
        status: ProcessedMailStatus,
        *,
        error: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO processed_mail (
                account, folder, uid_validity, uid, status, error, processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, folder, uid_validity, uid) DO UPDATE SET
                status       = excluded.status,
                error        = excluded.error,
                processed_at = excluded.processed_at
            """,
            (account, folder, uid_validity, uid, status.value, error, utcnow_iso()),
        )

    def get_status(
        self, account: str, folder: str, uid_validity: int, uid: int
    ) -> str | None:
        row = self._conn.execute(
            "SELECT status FROM processed_mail WHERE account=? AND folder=? "
            "AND uid_validity=? AND uid=?",
            (account, folder, uid_validity, uid),
        ).fetchone()
        return row["status"] if row else None

    def count_by_status(self, account: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM processed_mail WHERE account=? "
            "GROUP BY status",
            (account,),
        ).fetchall()
        return {row["status"]: row["n"] for row in rows}


class SenderRepository:
    """``senders``：发件人统计，供噪音判定与「非联系人」判据使用。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(
        self,
        account: str,
        addr: str,
        *,
        name: str | None = None,
        has_unsubscribe: bool = False,
        unread: bool = False,
        last_seen_at: str | None = None,
        is_reply_from_user: bool = False,
    ) -> None:
        """累计一次收信。

        ``replied_count`` 只在明确是用户回复时增加——它是判断「非联系人」的
        依据，因此不能由「收到过邮件」推断。
        """
        now = utcnow_iso()
        self._conn.execute(
            """
            INSERT INTO senders (
                account, addr, name, total, unread, replied_count,
                last_seen_at, has_unsubscribe, noise_score
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, NULL)
            ON CONFLICT(account, addr) DO UPDATE SET
                name            = COALESCE(excluded.name, senders.name),
                total           = senders.total + 1,
                unread          = senders.unread + excluded.unread,
                replied_count   = senders.replied_count + excluded.replied_count,
                last_seen_at    = excluded.last_seen_at,
                has_unsubscribe = MAX(senders.has_unsubscribe, excluded.has_unsubscribe)
            """,
            (
                account,
                addr.lower(),
                name,
                1 if unread else 0,
                1 if is_reply_from_user else 0,
                last_seen_at or now,
                1 if has_unsubscribe else 0,
            ),
        )

    def is_known_contact(self, account: str, addr: str) -> bool:
        """是否算是「联系人」——用于 ICS 自动入历的信任判断。

        定义保守：必须有过用户主动回复（``replied_count > 0``）。
        仅「收到过对方的邮件」不算联系人，否则垃圾邮件发送者会自动成为联系人。
        """
        row = self._conn.execute(
            "SELECT replied_count FROM senders WHERE account=? AND addr=?",
            (account, addr.lower()),
        ).fetchone()
        return bool(row and row["replied_count"] > 0)

    def top_senders(self, account: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM senders WHERE account=? ORDER BY total DESC LIMIT ?",
            (account, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def _to_record(row: sqlite3.Row | None) -> MessageRecord | None:
    if row is None:
        return None
    return MessageRecord(
        id=row["id"],
        account=row["account"],
        folder=row["folder"],
        uid_validity=row["uid_validity"],
        uid=row["uid"],
        normalized_message_id=row["normalized_message_id"],
        is_canonical=row["is_canonical"],
        duplicate_of=row["duplicate_of"],
        stale=row["stale"],
        folder_moved=row["folder_moved"],
        body_sha256=row["body_sha256"],
        extract_status=row["extract_status"],
        extract_attempts=row["extract_attempts"],
    )


class EventRepository:
    """``events`` 表的读写：候选事件的落库与查询。

    P2 只负责**写入候选**（status 默认 pending）；审批与推送属 P3/P4。
    幂等由唯一索引 ``(message_id, fingerprint)`` 保证——同一封邮件重跑抽取
    不会产生重复事件。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_candidate(
        self,
        *,
        message_id: int,
        title: str,
        start_ts: str | None,
        end_ts: str | None,
        all_day: bool,
        source: EventSource,
        confidence: float,
        fingerprint: str,
        evidence: str = "",
        location: str | None = None,
        organizer: str | None = None,
        tz: str | None = None,
        ics_uid: str | None = None,
        ics_sequence: int | None = None,
        ics_recurrence_id: str | None = None,
        ics_rrule: str | None = None,
        status: EventStatus = EventStatus.PENDING,
        requires_review: bool = True,
        review_reason: str = "",
    ) -> int:
        """写入或更新一个候选事件，返回其 id。

        冲突时更新可变的展示字段（标题、时间、置信度等），但**不覆盖人工状态**：
        若该事件已被 approve/reject/ignore，重跑抽取不得把它拉回 pending——
        否则用户审批过的结果会被静默撤销。
        """
        now = utcnow_iso()
        existing = self._conn.execute(
            "SELECT id, status, manual_edited FROM events "
            "WHERE message_id = ? AND fingerprint = ?",
            (message_id, fingerprint),
        ).fetchone()

        if existing is not None:
            protected = existing["status"] in {
                EventStatus.REJECTED.value,
                EventStatus.IGNORED.value,
                EventStatus.PUSHED.value,
                EventStatus.APPROVED.value,
            } or existing["manual_edited"]
            if protected:
                # 保留人工决策，只刷新证据类字段
                self._conn.execute(
                    "UPDATE events SET title = ?, evidence = ?, updated_at = ? WHERE id = ?",
                    (title, evidence, now, existing["id"]),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE events SET
                        title = ?, start_ts = ?, end_ts = ?, all_day = ?, tz = ?,
                        location = ?, organizer = ?, confidence = ?, evidence = ?,
                        status = ?, needs_attention = ?, ics_uid = ?, ics_sequence = ?,
                        ics_recurrence_id = ?, ics_rrule = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title, start_ts, end_ts, 1 if all_day else 0, tz,
                        location, organizer, confidence, evidence,
                        status.value, 1 if requires_review else 0,
                        ics_uid, ics_sequence, ics_recurrence_id, ics_rrule,
                        now, existing["id"],
                    ),
                )
            return int(existing["id"])

        # 需要审核时把原因并入 evidence 之外的位置：
        # events 表没有独立的 review_reason 列，因此写入 evidence 的前缀
        stored_evidence = evidence
        if requires_review and review_reason:
            stored_evidence = f"[待审：{review_reason}] {evidence}".strip()

        cur = self._conn.execute(
            """
            INSERT INTO events (
                message_id, title, start_ts, end_ts, all_day, tz, location, organizer,
                source, confidence, fingerprint, evidence, status, needs_attention,
                ics_uid, ics_sequence, ics_recurrence_id, ics_rrule,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id, title, start_ts, end_ts, 1 if all_day else 0, tz,
                location, organizer, source.value, confidence, fingerprint,
                stored_evidence, status.value, 1 if requires_review else 0,
                ics_uid, ics_sequence, ics_recurrence_id, ics_rrule,
                now, now,
            ),
        )
        return int(cur.lastrowid)

    def count_by_status(self, *, since: str | None = None) -> dict[str, int]:
        if since:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) n FROM events WHERE created_at >= ? GROUP BY status",
                (since,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) n FROM events GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def count_by_source(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT source, COUNT(*) n FROM events GROUP BY source"
        ).fetchall()
        return {row["source"]: row["n"] for row in rows}

    def list_by_status(
        self, status: EventStatus, *, limit: int = 100
    ) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM events WHERE status = ? ORDER BY start_ts ASC LIMIT ?",
                (status.value, limit),
            ).fetchall()
        )

    def list_pending_with_message(self, *, limit: int = 100) -> list[sqlite3.Row]:
        """列出待审事件并附上来源邮件信息（审核界面需要）。"""
        return list(
            self._conn.execute(
                """
                SELECT e.*, m.subject AS mail_subject, m.from_addr AS mail_from,
                       m.received_at AS mail_received
                  FROM events e LEFT JOIN messages m ON m.id = e.message_id
                 WHERE e.status = ?
                 ORDER BY e.requires_attention DESC, e.start_ts ASC
                 LIMIT ?
                """,
                (EventStatus.PENDING.value, limit),
            ).fetchall()
        )
