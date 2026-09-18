"""只读统计：让使用者看清「系统实际做了什么」。

## 为什么统计必须包含「降级/跳过」类指标

失败与跳过是两种不同的信号：

* **失败**：值得排查（凭据错了、服务端拒绝、网络问题）
* **跳过/降级**：系统按设计**主动放弃**了某个动作

若只报「成功 N 条」，使用者无法知道「有多少邮件本可被抽取但因为没配
LLM 而只做了规则」。这会让抽取质量看起来是系统能力上限，实则是配置问题。
因此 ``llm_skipped``、``events_protected`` 这类指标必须单列。

## 全部只读

本模块**不做任何写操作**——统计不该有副作用。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .db import utcnow
from .extract.runner import ExtractStats
from .models import EventStatus, ProcessedMailStatus


@dataclass(slots=True)
class StatsSnapshot:
    """一次统计快照。"""

    # ── 邮件 ──
    messages_total: int = 0
    messages_by_extract_status: dict[str, int] = field(default_factory=dict)
    messages_with_ics: int = 0
    messages_with_unsubscribe: int = 0
    accounts_senders: int = 0

    # ── 同步台账 ──
    sync_by_status: dict[str, int] = field(default_factory=dict)
    sync_state: list[dict[str, Any]] = field(default_factory=list)

    # ── 事件 ──
    events_by_status: dict[str, int] = field(default_factory=dict)
    events_by_source: dict[str, int] = field(default_factory=dict)

    # ── 线程 ──
    threads_total: int = 0
    threads_by_strength: dict[str, int] = field(default_factory=dict)
    threads_longest: list[dict[str, Any]] = field(default_factory=list)

    # ── 运行 ──
    runs_total: int = 0
    runs_failed: int = 0

    # ── 需要关注 ──
    needs_attention: int = 0
    pending_window: int = 0
    """延迟窗口内即将自动入历的数量（可撤销对象）。"""

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages_total": self.messages_total,
            "messages_by_extract_status": dict(self.messages_by_extract_status),
            "messages_with_ics": self.messages_with_ics,
            "messages_with_unsubscribe": self.messages_with_unsubscribe,
            "accounts_senders": self.accounts_senders,
            "sync_by_status": dict(self.sync_by_status),
            "sync_state": list(self.sync_state),
            "events_by_status": dict(self.events_by_status),
            "events_by_source": dict(self.events_by_source),
            "threads_total": self.threads_total,
            "threads_by_strength": dict(self.threads_by_strength),
            "threads_longest": list(self.threads_longest),
            "runs_total": self.runs_total,
            "runs_failed": self.runs_failed,
            "needs_attention": self.needs_attention,
            "pending_window": self.pending_window,
        }

    @property
    def auto_pushable(self) -> int:
        """已批准且即将自动入历的事件数。"""
        return self.events_by_status.get(EventStatus.APPROVED.value, 0)

    @property
    def pending_review(self) -> int:
        return self.events_by_status.get(EventStatus.PENDING.value, 0)


class StatsCollector:
    """收集统计快照（只读）。"""

    def __init__(self, settings, conn: sqlite3.Connection) -> None:
        self._settings = settings
        self._conn = conn
        self._account = settings.account

    def collect(self) -> StatsSnapshot:
        snapshot = StatsSnapshot()
        self._collect_messages(snapshot)
        self._collect_sync(snapshot)
        self._collect_events(snapshot)
        self._collect_threads(snapshot)
        self._collect_runs(snapshot)
        return snapshot

    # ── 邮件 ──────────────────────────────────────────────

    def _collect_messages(self, snapshot: StatsSnapshot) -> None:
        row = self._conn.execute(
            "SELECT COUNT(*) n FROM messages WHERE account = ?", (self._account,)
        ).fetchone()
        snapshot.messages_total = int(row["n"]) if row else 0

        rows = self._conn.execute(
            "SELECT extract_status, COUNT(*) n FROM messages WHERE account = ? "
            "GROUP BY extract_status",
            (self._account,),
        ).fetchall()
        snapshot.messages_by_extract_status = {r["extract_status"]: int(r["n"]) for r in rows}

        for column, attr in (
            ("has_ics", "messages_with_ics"),
            ("has_unsubscribe", "messages_with_unsubscribe"),
        ):
            row = self._conn.execute(
                f"SELECT COUNT(*) n FROM messages WHERE account = ? AND {column} = 1",
                (self._account,),
            ).fetchone()
            setattr(snapshot, attr, int(row["n"]) if row else 0)

        row = self._conn.execute(
            "SELECT COUNT(*) n FROM senders WHERE account = ?", (self._account,)
        ).fetchone()
        snapshot.accounts_senders = int(row["n"]) if row else 0

    # ── 同步台账 ──────────────────────────────────────────

    def _collect_sync(self, snapshot: StatsSnapshot) -> None:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) n FROM processed_mail WHERE account = ? "
            "GROUP BY status",
            (self._account,),
        ).fetchall()
        snapshot.sync_by_status = {r["status"]: int(r["n"]) for r in rows}

        states = self._conn.execute(
            "SELECT folder, uid_validity, highest_uid, syncs_since_full, last_sync_at "
            "FROM sync_state WHERE account = ? ORDER BY folder",
            (self._account,),
        ).fetchall()
        snapshot.sync_state = [dict(r) for r in states]

    # ── 事件 ──────────────────────────────────────────────

    def _collect_events(self, snapshot: StatsSnapshot) -> None:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) n FROM events GROUP BY status"
        ).fetchall()
        snapshot.events_by_status = {r["status"]: int(r["n"]) for r in rows}

        rows = self._conn.execute(
            "SELECT source, COUNT(*) n FROM events GROUP BY source"
        ).fetchall()
        snapshot.events_by_source = {r["source"]: int(r["n"]) for r in rows}

        row = self._conn.execute(
            "SELECT COUNT(*) n FROM events WHERE needs_attention = 1"
        ).fetchone()
        snapshot.needs_attention = int(row["n"]) if row else 0

        row = self._conn.execute(
            "SELECT COUNT(*) n FROM scheduled_pushes WHERE state = 'queued'"
        ).fetchone()
        snapshot.pending_window = int(row["n"]) if row else 0

    # ── 线程 ──────────────────────────────────────────────

    def _collect_threads(self, snapshot: StatsSnapshot) -> None:
        row = self._conn.execute(
            "SELECT COUNT(*) n FROM threads WHERE account = ?", (self._account,)
        ).fetchone()
        snapshot.threads_total = int(row["n"]) if row else 0

        rows = self._conn.execute(
            "SELECT link_strength, COUNT(*) n FROM threads WHERE account = ? "
            "GROUP BY link_strength",
            (self._account,),
        ).fetchall()
        snapshot.threads_by_strength = {
            (r["link_strength"] or "unknown"): int(r["n"]) for r in rows
        }

        longest = self._conn.execute(
            """
            SELECT t.id, t.root_message_id, t.subject_norm, t.link_strength,
                   t.last_message_at, COUNT(m.id) AS n
              FROM threads t JOIN messages m ON m.thread_id = t.id
             WHERE t.account = ?
             GROUP BY t.id
             HAVING n > 1
             ORDER BY n DESC, t.last_message_at DESC
             LIMIT 10
            """,
            (self._account,),
        ).fetchall()
        snapshot.threads_longest = [dict(r) for r in longest]

    # ── 运行 ──────────────────────────────────────────────

    def _collect_runs(self, snapshot: StatsSnapshot) -> None:
        row = self._conn.execute("SELECT COUNT(*) n FROM runs").fetchone()
        snapshot.runs_total = int(row["n"]) if row else 0

        row = self._conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE ok = 0"
        ).fetchone()
        snapshot.runs_failed = int(row["n"]) if row else 0


def merge_extract_stats(into: dict[str, Any], stats: ExtractStats) -> None:
    """把一轮抽取的统计并入字典（供 runs.stats 与统计报告共用）。"""
    into.update(stats.as_dict())


def describe_extract_skips(stats: ExtractStats) -> list[str]:
    """解释「为什么有些邮件没被 LLM 抽取」。

    单独成函数是因为这段说明必须出现在报告里：使用者看到「降级跳过 19」
    时应该同时知道**原因**（未配置 LLM 凭据 / 超预算），
    否则会误以为是系统能力如此。
    """
    notes: list[str] = []
    if stats.llm_skipped:
        reasons = "、".join(stats.llm_skipped_reasons) or "原因未记录"
        notes.append(
            f"{stats.llm_skipped} 封邮件本可交给 LLM 抽取，但被跳过（{reasons}）。"
            "这些邮件只做了规则与 ICS 抽取。"
        )
    if stats.llm_unavailable:
        notes.append(
            "LLM 凭据未配置，抽取运行在「仅规则 + ICS」模式。"
            "在 .env 中填入 LLM_BASE_URL 与 LLM_API_KEY 可启用兜底。"
        )
    if stats.attempts_exhausted:
        notes.append(
            f"{stats.attempts_exhausted} 封邮件已达抽取尝试上限，转为终态不再重试"
            "（防止毒邮件反复消耗预算）。"
        )
    if stats.events_protected:
        notes.append(
            f"{stats.events_protected} 个事件因已被人工审批而未被抽取结果覆盖。"
        )
    return notes


def collect(settings, conn: sqlite3.Connection) -> StatsSnapshot:
    """便捷入口。"""
    return StatsCollector(settings, conn).collect()


def now_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "StatsCollector",
    "StatsSnapshot",
    "collect",
    "describe_extract_skips",
    "merge_extract_stats",
    "now_iso",
    "EventStatus",
    "ProcessedMailStatus",
]
