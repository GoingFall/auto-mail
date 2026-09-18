"""推送引擎：把已批准的事件写入日历（受控写入）。

## 安全保证（docs/spec-gcal-ownership.md）

1. **只处理 ``approved``** 的事件。``pending`` 永不写入。
2. **每次 create 前先按 ``auto_mail_key`` 反查**。命中则回填
   ``gcal_event_id`` 而不新建——这覆盖「创建成功但写库前进程崩溃」的场景。
   没有这一步，重跑就会在用户日历里产生重复事件。
3. **只管理带我方标记的事件**。所有权不匹配一律冻结。
4. **每次比对三方哈希**，检测到用户手改则冻结，不覆盖。
5. **删除默认是归档**（``status=cancelled``，可恢复），硬删除需显式请求。

## 为什么不能用 etag 做条件请求

Google Calendar API v3 **未定义** ``If-Match``（已核对官方 discovery 文档）。
因此用「get → 比对 → update」的乐观流程，接受一个极小的竞态窗口。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .calendar.backend import (
    CalendarAuthError,
    CalendarBackend,
    CalendarError,
    CalendarNotFoundError,
    build_event_payload,
)
from .calendar.normalize import normalize_hash
from .calendar.ownership import OwnershipVerdict, compare, describe_verdict
from .db import utcnow_iso
from .models import EventStatus, NotFoundPolicy, ScheduledPushState
from .settings import Settings

logger = logging.getLogger("automail.push")


@dataclass(slots=True)
class PushStats:
    """一轮推送的统计。"""

    considered: int = 0
    created: int = 0
    updated: int = 0
    backfilled: int = 0
    """命中幂等反查、回填了已存在事件（未新建）。"""

    skipped: int = 0
    failed: int = 0
    frozen: int = 0
    archived: int = 0
    limit_hit: int = 0
    """因本轮上限而未处理的数量（保持 approved，下轮继续）。"""

    errors: list[str] = field(default_factory=list)
    dry_run: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "considered": self.considered,
            "created": self.created,
            "updated": self.updated,
            "backfilled": self.backfilled,
            "skipped": self.skipped,
            "failed": self.failed,
            "frozen": self.frozen,
            "archived": self.archived,
            "limit_hit": self.limit_hit,
            "errors": list(self.errors),
        }


class PushEngine:
    """把已批准事件写入日历。"""

    def __init__(
        self, settings: Settings, conn: sqlite3.Connection, backend: CalendarBackend
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._backend = backend

    # ── 入口 ──────────────────────────────────────────────

    def push_approved(self, *, apply: bool = False) -> PushStats:
        """推送所有已批准的事件。

        Args:
            apply: ``False``（默认）为 dry-run——只报告将执行的动作，不写入。
        """
        stats = PushStats(dry_run=not apply)

        limit = self._settings.auto_push_limit_per_run
        rows = self._conn.execute(
            "SELECT * FROM events WHERE status = ? ORDER BY start_ts ASC",
            (EventStatus.APPROVED.value,),
        ).fetchall()

        if limit and len(rows) > limit:
            stats.limit_hit = len(rows) - limit
            rows = rows[:limit]

        stats.considered = len(rows)

        for row in rows:
            try:
                self._push_one(row, stats, apply=apply)
            except CalendarAuthError:
                raise
            except CalendarError as exc:
                self._mark_failure(row["id"], str(exc))
                stats.failed += 1
                stats.errors.append(f"#{row['id']} {exc}")

        return stats

    def dispatch_due(self, *, apply: bool = False) -> PushStats:
        """处理到点的延迟窗口项。

        这是自动白名单事件的正式写入路径：事件在窗口内保持 ``approved``，
        到点后由本方法推送。
        """
        stats = PushStats(dry_run=not apply)

        from .review import ReviewQueue

        queue = ReviewQueue(self._conn)
        due = queue.due_pushes()
        if not due:
            return stats

        for queue_id, event_id in due:
            if not apply:
                stats.considered += 1
                continue

            row = self._conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None or row["status"] != EventStatus.APPROVED.value:
                # 事件已被取消/审批改动：队列项作废，不推送
                queue.mark_dispatched(
                    queue_id, state=ScheduledPushState.CANCELLED,
                    error=f"事件状态为 {row['status'] if row else 'missing'}，跳过",
                )
                stats.skipped += 1
                continue

            stats.considered += 1
            try:
                self._push_one(row, stats, apply=True)
                queue.mark_dispatched(queue_id, state=ScheduledPushState.DISPATCHED)
            except CalendarError as exc:
                queue.mark_dispatched(
                    queue_id, state=ScheduledPushState.FAILED, error=str(exc)
                )
                self._mark_failure(event_id, str(exc))
                stats.failed += 1
                stats.errors.append(f"#{event_id} {exc}")

        return stats

    # ── 单条推送 ──────────────────────────────────────────

    def _push_one(self, row: sqlite3.Row, stats: PushStats, *, apply: bool) -> None:
        event_id = int(row["id"])
        key = row["fingerprint"] or f"event-{event_id}"

        existing_id = row["gcal_event_id"]

        if existing_id:
            self._update_existing(row, existing_id, key, stats, apply=apply)
        else:
            self._create_new(row, key, stats, apply=apply)

    def _create_new(
        self, row: sqlite3.Row, key: str, stats: PushStats, *, apply: bool
    ) -> None:
        """创建事件。

        **先反查再创建**：``insert`` 之前按 ``auto_mail_key`` 查一次，
        命中说明上次「创建成功但写库失败（进程崩溃）」——此时应回填而不新建。
        """
        event_id = int(row["id"])

        if not apply:
            stats.created += 1
            return

        found = self._backend.find_by_auto_mail_key(key)
        if found:
            # 幂等回填：不新建
            existing = found[0]
            payload = existing.payload
            self._conn.execute(
                """
                UPDATE events SET gcal_event_id = ?, gcal_etag = ?,
                    snapshot_hash = ?, snapshot_payload = ?, remote_norm_hash = ?,
                    managed_state = 'owned', status = ?, needs_attention = 0,
                    last_checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    existing.event_id,
                    existing.etag,
                    normalize_hash(payload),
                    json.dumps(payload, ensure_ascii=False),
                    normalize_hash(payload),
                    EventStatus.PUSHED.value,
                    utcnow_iso(),
                    utcnow_iso(),
                    event_id,
                ),
            )
            stats.backfilled += 1
            return

        payload = self._build_payload(row, key)
        created = self._backend.insert_event(payload)

        self._conn.execute(
            """
            UPDATE events SET gcal_event_id = ?, gcal_etag = ?,
                snapshot_hash = ?, snapshot_payload = ?, remote_norm_hash = ?,
                managed_state = 'owned', status = ?, needs_attention = 0,
                last_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                created.event_id,
                created.etag,
                normalize_hash(created.payload),
                json.dumps(payload, ensure_ascii=False),
                normalize_hash(created.payload),
                EventStatus.PUSHED.value,
                utcnow_iso(),
                utcnow_iso(),
                event_id,
            ),
        )
        stats.created += 1

    def _update_existing(
        self,
        row: sqlite3.Row,
        gcal_event_id: str,
        key: str,
        stats: PushStats,
        *,
        apply: bool,
    ) -> None:
        """更新已存在的事件，**先做三方比对**。"""
        event_id = int(row["id"])

        if not apply:
            stats.updated += 1
            return

        # 1) 读回远端
        try:
            remote = self._backend.get_event(gcal_event_id)
        except CalendarNotFoundError:
            self._handle_not_found(row, stats)
            return

        # 2) 三方比对
        payload = self._build_payload(row, key)
        result = compare(
            remote=remote.payload,
            snapshot_hash=row["snapshot_hash"],
            local_hash=normalize_hash(payload),
            remote_etag=remote.etag,
            last_etag=row["gcal_etag"],
            expected_auto_mail_key=key,
            snapshot_payload=self._load_snapshot(row),
        )

        if result.frozen:
            self._apply_frozen(row, result, remote, stats)
            return

        if result.verdict is OwnershipVerdict.NO_CHANGE:
            stats.skipped += 1
            self._touch(row["id"], remote)
            return

        # 3) 允许写入
        updated = self._backend.update_event(gcal_event_id, payload)
        self._conn.execute(
            """
            UPDATE events SET gcal_etag = ?, snapshot_hash = ?, snapshot_payload = ?,
                remote_norm_hash = ?, status = ?, needs_attention = 0,
                last_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                updated.etag,
                normalize_hash(updated.payload),
                json.dumps(payload, ensure_ascii=False),
                normalize_hash(updated.payload),
                EventStatus.PUSHED.value,
                utcnow_iso(),
                utcnow_iso(),
                event_id,
            ),
        )
        stats.updated += 1

    def _handle_not_found(self, row: sqlite3.Row, stats: PushStats) -> None:
        """远端事件不存在 → 按 ``NOT_FOUND_POLICY`` 处理。"""
        policy = NotFoundPolicy(self._settings.not_found_policy)

        if policy is NotFoundPolicy.RECREATE:
            # 重建：清空 gcal 关联，下一轮会走 create 路径
            self._conn.execute(
                "UPDATE events SET gcal_event_id = NULL, gcal_etag = NULL, "
                "snapshot_hash = NULL, snapshot_payload = NULL, status = ?, "
                "needs_attention = 0, updated_at = ? WHERE id = ?",
                (EventStatus.APPROVED.value, utcnow_iso(), row["id"]),
            )
            stats.failed += 1
            stats.errors.append(f"#{row['id']} 远端不存在，按策略将重建")
            return

        if policy is NotFoundPolicy.FAIL:
            self._conn.execute(
                "UPDATE events SET status = ?, needs_attention = 1, updated_at = ? "
                "WHERE id = ?",
                (EventStatus.MISSING.value, utcnow_iso(), row["id"]),
            )
            stats.failed += 1
            stats.errors.append(f"#{row['id']} 远端不存在（策略 fail）")
            return

        # 默认 pending：交人工判断（可能是用户删的，也可能是被清空）
        self._conn.execute(
            "UPDATE events SET status = ?, needs_attention = 1, updated_at = ? WHERE id = ?",
            (EventStatus.MISSING.value, utcnow_iso(), row["id"]),
        )
        stats.failed += 1
        stats.errors.append(f"#{row['id']} 远端不存在，已转人工确认")

    def _apply_frozen(
        self, row: sqlite3.Row, result: Any, remote: Any, stats: PushStats
    ) -> None:
        """冻结：记录状态与差异，**不做任何写入**。"""
        target = (
            EventStatus.CONFLICT
            if result.verdict is OwnershipVerdict.CONFLICT
            else EventStatus.EXTERNALLY_MODIFIED
        )
        detail = describe_verdict(result)
        logger.warning("事件 #%s 冻结（%s）：%s", row["id"], target.value, detail)

        self._conn.execute(
            """
            UPDATE events SET status = ?, needs_attention = 1,
                managed_state = 'frozen', remote_norm_hash = ?,
                gcal_etag = ?, last_checked_at = ?, updated_at = ?,
                evidence = COALESCE(evidence, '') || ?
            WHERE id = ?
            """,
            (
                target.value,
                normalize_hash(remote.payload),
                remote.etag,
                utcnow_iso(),
                utcnow_iso(),
                f"\n[冻结：{detail}]",
                row["id"],
            ),
        )
        stats.frozen += 1

    # ── 删除与归档 ────────────────────────────────────────

    def archive(
        self, event_id: int, *, apply: bool = False
    ) -> PushStats:
        """归档式取消：把远端事件置为 ``cancelled``（可恢复）。

        这是**默认**的删除语义。硬删除需显式调用 :meth:`hard_delete`。
        """
        stats = PushStats(dry_run=not apply)
        row = self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            stats.errors.append(f"#{event_id} 不存在")
            return stats

        gcal_id = row["gcal_event_id"]
        if not gcal_id:
            # 没推送过：直接标记归档即可，无远端副作用
            if apply:
                self._conn.execute(
                    "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
                    (EventStatus.CANCELLED.value, utcnow_iso(), event_id),
                )
            stats.archived += 1
            return stats

        if not apply:
            stats.archived += 1
            return stats

        # 归档前必须确认所有权：不能取消别人的事件
        key = row["fingerprint"]
        try:
            remote = self._backend.get_event(gcal_id)
        except CalendarNotFoundError:
            self._conn.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
                (EventStatus.CANCELLED.value, utcnow_iso(), event_id),
            )
            stats.archived += 1
            return stats

        from .calendar.normalize import extract_auto_mail_key

        if extract_auto_mail_key(remote.payload) != key:
            stats.frozen += 1
            stats.errors.append(f"#{event_id} 所有权不匹配，拒绝归档")
            return stats

        payload = dict(remote.payload)
        payload["status"] = "cancelled"
        self._backend.update_event(gcal_id, payload)
        self._conn.execute(
            "UPDATE events SET status = ?, gcal_etag = ?, last_checked_at = ?, "
            "updated_at = ? WHERE id = ?",
            (EventStatus.CANCELLED.value, None, utcnow_iso(), utcnow_iso(), event_id),
        )
        stats.archived += 1
        return stats

    def hard_delete(self, event_id: int, *, apply: bool = False) -> PushStats:
        """硬删除远端事件。

        需要**显式**请求，且会校验所有权标记匹配。这是唯一不可恢复的操作。
        """
        stats = PushStats(dry_run=not apply)
        row = self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            stats.errors.append(f"#{event_id} 不存在")
            return stats

        gcal_id = row["gcal_event_id"]
        if not gcal_id:
            stats.errors.append(f"#{event_id} 尚未推送到日历，无需删除")
            return stats

        if not apply:
            stats.archived += 1
            return stats

        try:
            remote = self._backend.get_event(gcal_id)
        except CalendarNotFoundError:
            stats.skipped += 1
            return stats

        from .calendar.normalize import extract_auto_mail_key

        if extract_auto_mail_key(remote.payload) != row["fingerprint"]:
            stats.frozen += 1
            stats.errors.append(f"#{event_id} 所有权不匹配，拒绝删除")
            return stats

        self._backend.delete_event(gcal_id)
        self._conn.execute(
            "UPDATE events SET status = ?, gcal_event_id = NULL, updated_at = ? "
            "WHERE id = ?",
            (EventStatus.CANCELLED.value, utcnow_iso(), event_id),
        )
        stats.archived += 1
        return stats

    # ── 辅助 ──────────────────────────────────────────────

    def _build_payload(self, row: sqlite3.Row, key: str) -> dict[str, Any]:
        """构造日历 payload。

        **description 必须稳定**：刻意不用每次重新生成的文案，而是沿用
        ``snapshot_payload`` 里的值。原因如下——

        ``local_hash`` 与 ``snapshot_hash`` 的差异会被判为「本地也改了」，
        从而把纯外部改动（用户手改）升级成 ``conflict``。若 description 每次
        都重新生成（例如 evidence 里追加了新的来源），那个措辞变化就会让
        **每一次更新都被误判为冲突**，使用者被迫多做一次无谓裁决。

        这不危险（两种情况都是冻结），但会让审核队列充满噪音。稳定性优先。
        """
        description = self._stable_description(row, key)

        return build_event_payload(
            title=row["title"] or "(无标题)",
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            all_day=bool(row["all_day"]),
            auto_mail_key=key,
            location=row["location"],
            description=description,
            timezone=self._settings.user_timezone,
        )

    def _stable_description(self, row: sqlite3.Row, key: str) -> str:
        """生成稳定（可重复）的描述文本。

        优先沿用已保存的快照描述，确保重复构造同一事件的 payload 得到
        **完全相同**的内容——这是 local_hash 可比性的前提。
        """
        snapshot = self._load_snapshot(row)
        if snapshot and isinstance(snapshot.get("description"), str):
            existing = snapshot["description"]
            if existing:
                return existing

        parts = []
        if row["evidence"]:
            parts.append(f"来源：{row['evidence']}")
        parts.append(f"由 auto-mail 自动创建（{row['source']}）")
        return "\n".join(parts)

    def _load_snapshot(self, row: sqlite3.Row) -> dict[str, Any] | None:
        raw = row["snapshot_payload"] if "snapshot_payload" in row.keys() else None
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _touch(self, event_id: int, remote: Any) -> None:
        """仅更新检查时间与 etag（无内容变化）。"""
        self._conn.execute(
            "UPDATE events SET gcal_etag = ?, last_checked_at = ?, updated_at = ? "
            "WHERE id = ?",
            (remote.etag, utcnow_iso(), utcnow_iso(), event_id),
        )

    def _mark_failure(self, event_id: int, error: str) -> None:
        """记录推送失败并按次数决定是否升级为「需人工关注」。"""
        from .sanitize import sanitize_error

        self._conn.execute(
            "UPDATE events SET push_attempts = push_attempts + 1, updated_at = ? "
            "WHERE id = ?",
            (utcnow_iso(), event_id),
        )
        row = self._conn.execute(
            "SELECT push_attempts FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        attempts = int(row["push_attempts"]) if row else 0

        exceeded = attempts >= self._settings.push_max_attempts
        self._conn.execute(
            "UPDATE events SET status = ?, needs_attention = ? WHERE id = ?",
            (
                EventStatus.PUSH_FAILED.value,
                1 if exceeded else 0,
                event_id,
            ),
        )
        logger.warning(
            "推送事件 #%s 失败（第 %s 次）：%s",
            event_id, attempts, sanitize_error(error),
        )
