"""已读回写：把「无需你再做什么」的邮件在邮箱里标为已读。

**这是本项目唯一的邮箱写操作。** v1 全程只读（``EXAMINE`` + ``BODY.PEEK[]``），
因此本模块的每一处判断都以「宁可少标，不可误标」为准——误标会让一封真正需要
你处理的邮件从「未读」里消失，而这正是这个功能要解决的问题。弄错了反而更难发现。

## 为什么值得做

「未读」是邮箱里最有效的注意力信号。如果它被已处理的邮件占满，这个信号就废了。
让未读继续表示「需要你处理」，是这个功能的全部价值。

## 三道闸门（缺一不可）

1. **默认关闭**：``mark_read_policy=off``。v1 是只读工具，升级不应改变邮箱状态。
2. **默认 dry-run**：``automail mark-read`` 只报告将标记哪些，``--apply`` 才真写。
3. **UIDVALIDITY 必须一致**：库里记录的 UIDVALIDITY 与服务端不符时，
   UID 已经指不准哪封邮件了，一切回写必须中止（见 :func:`_guard_uidvalidity`）。

## 什么算「处理完」

``resolved``（默认建议）定义得比 ``processed`` 保守——只有在**没有任何事件等着
你**的时候才标已读：

* 没有任何事件（通知、营销、回执）→ 已处理 ✓
* 事件全部已批准/已入历/已否决/已忽略 → 你的决定做完了 ✓
* 仍有 ``pending`` / ``uncertain`` / ``push_failed`` → **保持未读** ✗

最后一条是关键：那些状态正等着你（或等着程序重试），把「未读」这个信号保留给
它们才符合直觉。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .mail.backend import MailBackend, MailError
from .models import ExtractStatus
from .settings import Settings
from .store import MessageRepository, SyncStateRepository

logger = logging.getLogger("automail.read_state")

#: 事件处于这些状态时，邮件仍「等着你」→ 不标已读。
#:
#: * ``pending``      —— 等你审批
#: * ``uncertain``    —— 需要确认（反查失败，无法判定日历侧状态）
#: * ``push_failed``  —— 推送失败，需要关注
#:
#: 其余状态（approved/pushed/rejected/ignored/cancelled/superseded）都表示
#: 你的那部分已经做完，或事件已被程序妥善处理。
_BLOCKING_EVENT_STATUSES = ("pending", "uncertain", "push_failed")

#: 「这封邮件在等你做点什么」的信号词。
#:
#: **为什么必须有这一层**：``resolved`` 的判据是「没有待处理的**日历事件**」，
#: 但「没有日历事件」不等于「没事要做」。实测踩到过：GitHub 的仓库邀请
#: （``X invited you to X/repo``）不产生任何日历事件，却被判为「无需处理」——
#: 而那正是一封需要回应的邮件。把它标成已读，等于让它从「未读」里消失。
#:
#: 因此这里再补一道**偏向保守**的检查：措辞上有「请你响应」意味的邮件一律
#: 保持未读。词表偏宽是刻意的——漏标只是少省一件事，误标会藏起待办。
_ACTIONABLE_SIGNALS = (
    # 邀请 / 请求
    "invited you", "invitation", "invite", "邀请", "邀請", "邀約",
    # 请你确认 / 验证
    "confirm", "confirmation", "verify", "verification",
    "確認", "确认", "驗證", "验证", "核對", "核对",
    "action required", "action needed", "requires your action",
    "please review", "please sign", "sign here", "待签署", "待簽署",
    # 请你回复
    "please reply", "reply to", "respond", "response required",
    "回覆", "回复", "尽快回复", "盡快回覆", "請回覆", "请回复",
    # 期限类
    "deadline", "due by", "expires", "expiring", "截止", "到期", "逾期",
    # 账号/安全类需要动作的
    "reset your password", "重設密碼", "重置密码",
    "verify your account", "confirm your email", "確認電郵", "确认邮箱",
    "unusual sign-in", "异常登录", "異常登入",
)


def _looks_actionable(subject: str, from_addr: str) -> str | None:
    """邮件是否像是「在等你做点什么」；命中则返回命中的信号词。

    只扫**主题**与发件人，不扫正文：正文里「如有疑问请联系」这类套话太常见，
    ``confirmation`` 之类的词在收据里也到处都是，扫正文会让几乎所有邮件都被
    判为 actionable，等于这道闸门失效。
    """
    haystack = f"{subject}\n{from_addr}".casefold()
    for signal in _ACTIONABLE_SIGNALS:
        if signal in haystack:
            return signal
    return None


@dataclass(slots=True)
class MarkReadCandidate:
    """一封待标记的邮件。"""

    message_id: int
    uid: int
    folder: str
    subject: str
    from_addr: str
    received_at: str | None
    extract_status: str
    reason: str
    """为什么判定它「处理完」——便于复核，避免这是个黑盒。"""


@dataclass(slots=True)
class FolderMarkStats:
    """单个文件夹的回写统计。"""

    folder: str = ""
    candidates: int = 0
    marked: int = 0
    already_seen: int = 0
    """服务端本来就带 ``\\Seen`` —— 无需重复 STORE。"""

    already_marked: int = 0
    """本程序此前标记过（``marked_read_at`` 非空）。"""

    failed: int = 0
    skipped_reason: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "folder": self.folder,
            "candidates": self.candidates,
            "marked": self.marked,
            "already_seen": self.already_seen,
            "already_marked": self.already_marked,
            "failed": self.failed,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
        }


@dataclass(slots=True)
class MarkReadStats:
    """一轮回写的汇总。"""

    policy: str = "off"
    dry_run: bool = True
    folders: list[FolderMarkStats] = field(default_factory=list)
    candidates: list[MarkReadCandidate] = field(default_factory=list)
    """dry-run 时用于展示「将标记哪些」；apply 时同样保留以便报告。"""

    @property
    def marked(self) -> int:
        return sum(f.marked for f in self.folders)

    @property
    def total_candidates(self) -> int:
        return sum(f.candidates for f in self.folders)

    @property
    def already_seen(self) -> int:
        """服务端本来就是已读的封数（不会产生 STORE）。"""
        return sum(f.already_seen for f in self.folders)

    @property
    def to_write(self) -> int:
        """**实际会发出 STORE 的封数**。

        dry-run 时报告这个数字，而不是包含「已是已读」的总候选数——
        否则使用者看到的「将标记 55 封」里有 50 封本来就已读，
        会让这个唯一改变邮箱状态的操作显得比实际激进得多。
        """
        return self.total_candidates - self.already_seen

    @property
    def has_errors(self) -> bool:
        return any(f.error for f in self.folders)

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "dry_run": self.dry_run,
            "marked": self.marked,
            "candidates": self.total_candidates,
            "already_seen": self.already_seen,
            "to_write": self.to_write,
            "folders": [f.as_dict() for f in self.folders],
        }


class MarkReadError(Exception):
    """回写过程中的致命错误（调用方应中止本文件夹）。"""


def select_candidates(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    folder: str,
    account: str | None = None,
    limit: int | None = None,
) -> list[MarkReadCandidate]:
    """挑出该文件夹下「处理完」且尚未标记的邮件。

    只挑**已入库且仍在该文件夹**的邮件：``stale=0``（UIDVALIDITY 没变过）、
    ``folder_moved=0``（没被移走）、``is_canonical=1``（副本不重复处理）。

    ``limit`` 用于首次启用时小批量试跑——先标几封看看效果，再放开。
    """
    account = account or settings.account
    policy = settings.mark_read_policy
    if policy == "off":
        return []

    params: list[Any] = [account, folder]
    if policy == "processed":
        # 抽取完成即算处理完——不看事件状态，因此无需 NOT EXISTS 子查询
        sql = """
            SELECT m.id, m.uid, m.subject, m.from_addr, m.received_at,
                   m.extract_status, m.flags
              FROM messages m
             WHERE m.account = ? AND m.folder = ?
               AND m.is_canonical = 1
               AND m.stale = 0 AND m.folder_moved = 0
               AND m.extract_status = ?
               AND m.marked_read_at IS NULL
             ORDER BY m.uid ASC
        """
        params.append(ExtractStatus.DONE.value)
    else:
        # resolved：抽取完成，且**没有任何事件等你处理**
        placeholders = ",".join("?" for _ in _BLOCKING_EVENT_STATUSES)
        sql = f"""
            SELECT m.id, m.uid, m.subject, m.from_addr, m.received_at,
                   m.extract_status, m.flags
              FROM messages m
             WHERE m.account = ? AND m.folder = ?
               AND m.is_canonical = 1
               AND m.stale = 0 AND m.folder_moved = 0
               AND m.extract_status = ?
               AND m.marked_read_at IS NULL
               AND NOT EXISTS (
                     SELECT 1 FROM events e
                      WHERE e.message_id = m.id
                        AND e.status IN ({placeholders})
                   )
             ORDER BY m.uid ASC
        """
        params.append(ExtractStatus.DONE.value)
        params.extend(_BLOCKING_EVENT_STATUSES)

    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(sql, params).fetchall()

    candidates: list[MarkReadCandidate] = []
    for row in rows:
        flags = (row["flags"] or "").lower()
        subject = row["subject"] or "(无主题)"
        from_addr = row["from_addr"] or ""

        if "\\seen" in flags:
            # 服务端已经是已读——不必再 STORE，但仍要记下来，
            # 这样报告能区分「已读过」与「我们标的」。
            candidates.append(
                MarkReadCandidate(
                    message_id=int(row["id"]),
                    uid=int(row["uid"]),
                    folder=folder,
                    subject=subject,
                    from_addr=from_addr,
                    received_at=row["received_at"],
                    extract_status=row["extract_status"],
                    reason="already-seen",
                )
            )
            continue

        # 「没有日历事件」不等于「没事要做」。措辞上像在等你响应的邮件一律
        # 保持未读——见 _ACTIONABLE_SIGNALS 的说明。
        #
        # 这条**只对 resolved 生效**：processed 的语义是「抽取完成就标」，
        # 使用者明确选择了不按「是否待办」筛选。
        if policy == "resolved":
            signal = _looks_actionable(subject, from_addr)
            if signal:
                logger.debug(
                    "跳过 #%s：主题/发件人像待办（命中 %r）", row["id"], signal
                )
                continue

        candidates.append(
            MarkReadCandidate(
                message_id=int(row["id"]),
                uid=int(row["uid"]),
                folder=folder,
                subject=subject,
                from_addr=from_addr,
                received_at=row["received_at"],
                extract_status=row["extract_status"],
                reason=_describe_reason(conn, message_id=int(row["id"]), policy=policy),
            )
        )
    return candidates


def _describe_reason(conn: sqlite3.Connection, *, message_id: int, policy: str) -> str:
    """给出可读的判定依据。"""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE message_id = ?", (message_id,)
    ).fetchone()["n"]
    if policy == "processed":
        return "抽取已完成"
    if not total:
        return "无事件（无需处理）"
    return f"{total} 个事件均已处理完毕"


def _guard_uidvalidity(
    conn: sqlite3.Connection, *, account: str, folder: str, live: int
) -> str | None:
    """UIDVALIDITY 不一致时返回原因串，一致返回 ``None``。

    **这是写回写最重要的一道闸门。** UIDVALIDITY 变化意味着该文件夹里的 UID
    已经全部重新分配——库里存的 UID 可能指向完全不同的邮件。此时若继续 STORE，
    就会把**别人的邮件**标为已读。

    返回原因而不是抛异常，因为调用方要把它记进报告（用户需要知道为什么跳过）。
    """
    state = SyncStateRepository(conn).get(account, folder)
    if state is None:
        return "尚无同步记录（先跑一次 sync）"
    if int(state.uid_validity) != int(live):
        return (
            f"UIDVALIDITY 已变化（库中 {state.uid_validity} ≠ 服务端 {live}）："
            "UID 已重新分配，拒绝回写。请先跑一次 sync 重建"
        )
    return None


def mark_read(
    settings: Settings,
    conn: sqlite3.Connection,
    backend: MailBackend,
    *,
    apply: bool = False,
    limit: int | None = None,
    account: str | None = None,
) -> MarkReadStats:
    """把处理完的邮件标为已读。

    Args:
        apply: ``False``（默认）为 dry-run——只报告将标记哪些，**不发任何 STORE**。
        limit: 每个文件夹最多标记几封（首次启用时建议给小值）。
    """
    account = account or settings.account
    stats = MarkReadStats(policy=settings.mark_read_policy, dry_run=not apply)

    if not settings.mark_read_enabled:
        return stats

    messages = MessageRepository(conn)
    seen_flag_writer = getattr(backend, "mark_seen", None)

    for folder in settings.mark_read_folder_list:
        folder_stats = FolderMarkStats(folder=folder)

        try:
            # 可写选中：只读（EXAMINE）会让 STORE 被服务端拒绝。
            # 这里显式传 readonly=False，是因为**接下来要写**。
            status = backend.select_folder(folder, readonly=False)
        except MailError as exc:
            folder_stats.error = str(exc)
            stats.folders.append(folder_stats)
            continue

        guard = _guard_uidvalidity(
            conn, account=account, folder=folder, live=status.uid_validity
        )
        if guard:
            folder_stats.skipped_reason = guard
            stats.folders.append(folder_stats)
            continue

        candidates = select_candidates(
            conn, settings, folder=folder, account=account, limit=limit
        )
        stats.candidates.extend(candidates)
        folder_stats.candidates = len(candidates)
        # 「已是已读」的不需要写，因此不能算进「将标记」——
        # 这是唯一会改变邮箱状态的操作，数字必须诚实地反映实际会发出的 STORE。
        folder_stats.already_seen = sum(
            1 for c in candidates if c.reason == "already-seen"
        )

        if not apply:
            stats.folders.append(folder_stats)
            continue

        # 已经是已读的不用再发 STORE（already_seen 已在上方计好）
        pending = [c for c in candidates if c.reason != "already-seen"]
        if pending and seen_flag_writer is None:
            folder_stats.error = "该后端不支持已读回写"
            stats.folders.append(folder_stats)
            continue

        batch = max(1, settings.mark_read_batch_size)
        for start in range(0, len(pending), batch):
            chunk = pending[start : start + batch]
            uids = [c.uid for c in chunk]
            try:
                seen_flag_writer(uids, seen=True)
            except MailError as exc:
                # 单批失败不放弃整轮：其余邮件仍值得标记。但必须记账，
                # 否则「有多少没标成功」对使用者不可见。
                folder_stats.failed += len(chunk)
                folder_stats.error = str(exc)
                logger.warning("标记 %s 的 %d 封失败：%s", folder, len(chunk), exc)
                continue
            for c in chunk:
                messages.mark_read_at(c.message_id)
                folder_stats.marked += 1

        stats.folders.append(folder_stats)

    return stats


def unmark_read(
    settings: Settings,
    conn: sqlite3.Connection,
    backend: MailBackend,
    *,
    apply: bool = False,
    limit: int | None = None,
    account: str | None = None,
) -> MarkReadStats:
    """撤销：把**本程序标记过**的邮件恢复为未读。

    只处理 ``marked_read_at`` 非空的记录——那是我们动过的痕迹。刻意不去碰
    用户自己在别的客户端读过的邮件：那属于用户的动作，不该被我们撤销。

    用途是「标记错了」的补救路径。有补救路径才敢开这个功能。
    """
    account = account or settings.account
    stats = MarkReadStats(policy="undo", dry_run=not apply)

    messages = MessageRepository(conn)
    seen_flag_writer = getattr(backend, "mark_seen", None)

    for folder in settings.mark_read_folder_list:
        folder_stats = FolderMarkStats(folder=folder)
        try:
            status = backend.select_folder(folder, readonly=False)
        except MailError as exc:
            folder_stats.error = str(exc)
            stats.folders.append(folder_stats)
            continue

        guard = _guard_uidvalidity(
            conn, account=account, folder=folder, live=status.uid_validity
        )
        if guard:
            folder_stats.skipped_reason = guard
            stats.folders.append(folder_stats)
            continue

        sql = """
            SELECT id, uid, subject, from_addr, received_at, extract_status
              FROM messages
             WHERE account = ? AND folder = ?
               AND is_canonical = 1
               AND marked_read_at IS NOT NULL
             ORDER BY uid ASC
        """
        params: list[Any] = [account, folder]
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()

        candidates = [
            MarkReadCandidate(
                message_id=int(r["id"]),
                uid=int(r["uid"]),
                folder=folder,
                subject=r["subject"] or "(无主题)",
                from_addr=r["from_addr"] or "",
                received_at=r["received_at"],
                extract_status=r["extract_status"],
                reason="本程序标记过",
            )
            for r in rows
        ]
        stats.candidates.extend(candidates)
        folder_stats.candidates = len(candidates)

        if not apply or not candidates:
            stats.folders.append(folder_stats)
            continue
        if seen_flag_writer is None:
            folder_stats.error = "该后端不支持已读回写"
            stats.folders.append(folder_stats)
            continue

        batch = max(1, settings.mark_read_batch_size)
        for start in range(0, len(candidates), batch):
            chunk = candidates[start : start + batch]
            uids = [c.uid for c in chunk]
            try:
                seen_flag_writer(uids, seen=False)
            except MailError as exc:
                folder_stats.failed += len(chunk)
                folder_stats.error = str(exc)
                continue
            for c in chunk:
                messages.clear_marked_read_at(c.message_id)
                folder_stats.marked += 1

        stats.folders.append(folder_stats)

    return stats
