"""已读回写（``automail mark-read``）的测试。

这是本项目**唯一对邮箱的写操作**，因此测试的重心不是「能不能标已读」，
而是**不该标的时候绝不标**：

* 默认关闭（升级不得静默改变邮箱状态）
* 默认 dry-run（不发任何 STORE）
* 有事件等你处理 → 保持未读
* UIDVALIDITY 变化 → 拒绝回写（否则会标到**别人的邮件**上）
* 只处理我们标记过的，回退时不碰用户自己读过的邮件
"""

from __future__ import annotations

import sqlite3

import pytest

from automail.db import utcnow_iso
from automail.mail.backend import FolderStatus, MailProtocolError
from automail.read_state import mark_read, unmark_read
from automail.settings import Settings
from tests.fake_imap import FakeFolder, FakeImapServer


def _settings(tmp_path, **overrides) -> Settings:
    """构造隔离的配置。

    ``_env_file=None`` 是必须的：否则会读到开发者本机的 ``.env``，
    测试结果就取决于本机配置（在真机验证时把策略改成 resolved 之后，
    这里立刻失败了——这正是它的价值）。
    """
    base = {
        "_env_file": None,
        "account": "163",
        "user_timezone": "Asia/Shanghai",
        "data_dir": tmp_path,
        "out_dir": tmp_path / "out",
        "log_dir": tmp_path / "logs",
        "backup_dir": tmp_path / "backup",
    }
    base.update(overrides)
    return Settings(**base)


class _Backend:
    """极简假后端：记录 STORE 调用，并可模拟只读拒绝。"""

    def __init__(self, *, uid_validity: int = 1, readonly_rejects: bool = True) -> None:
        self.uid_validity = uid_validity
        self.calls: list[tuple[list[int], bool]] = []
        self.selected: list[tuple[str, bool]] = []
        self._readonly = True
        self._readonly_rejects = readonly_rejects
        self.fail_with: Exception | None = None

    def select_folder(self, folder: str, *, readonly: bool = True) -> FolderStatus:
        self.selected.append((folder, readonly))
        self._readonly = readonly
        return FolderStatus(uid_validity=self.uid_validity, uid_next=100, exists=10)

    def mark_seen(self, uids: list[int], *, seen: bool = True) -> None:
        if self._readonly and self._readonly_rejects:
            raise MailProtocolError("STORE: mailbox is read-only")
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((list(uids), seen))


def _message(
    conn: sqlite3.Connection,
    *,
    uid: int,
    subject: str = "主題",
    from_addr: str = "a@b.com",
    flags: str | None = None,
    extract_status: str = "done",
    marked_read_at: str | None = None,
    is_canonical: int = 1,
    stale: int = 0,
    folder_moved: int = 0,
    folder: str = "INBOX",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            from_addr, received_at, body_excerpt, is_canonical, stale,
            folder_moved, fetched_at, extract_status, extract_attempts, flags,
            marked_read_at)
        VALUES ('163', ?, 1, ?, ?, ?, '2026-09-16T02:00:00Z',
                'body', ?, ?, ?, ?, 'done', 0, ?, ?)
        """,
        (
            folder,
            uid,
            subject,
            from_addr,
            is_canonical,
            stale,
            folder_moved,
            utcnow_iso(),
            flags,
            marked_read_at,
        ),
    )
    if extract_status != "done":
        conn.execute(
            "UPDATE messages SET extract_status = ? WHERE id = ?",
            (extract_status, int(cur.lastrowid)),
        )
    return int(cur.lastrowid)


def _event(conn: sqlite3.Connection, message_id: int, status: str, *, title: str = "e") -> None:
    conn.execute(
        """
        INSERT INTO events (message_id, title, start_ts, all_day, source,
            confidence, fingerprint, status, created_at, updated_at)
        VALUES (?, ?, '2026-10-01T02:00:00Z', 0, 'rules', 0.95, ?, ?, ?, ?)
        """,
        (message_id, title, f"{title}-{message_id}", status, utcnow_iso(), utcnow_iso()),
    )


def _sync_state(conn: sqlite3.Connection, folder: str = "INBOX", uid_validity: int = 1) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (account, folder, uid_validity, highest_uid, syncs_since_full)
        VALUES ('163', ?, ?, 100, 0)
        """,
        (folder, uid_validity),
    )


# ══════════════════════════════════════════════════════════════
# 默认必须安全
# ══════════════════════════════════════════════════════════════


def test_default_policy_is_off(conn, tmp_path) -> None:
    """默认不开启——v1 是只读工具，升级不得改变邮箱状态。"""
    settings = _settings(tmp_path)
    assert settings.mark_read_policy == "off"
    assert settings.mark_read_enabled is False

    _message(conn, uid=1)
    _sync_state(conn)
    conn.commit()
    backend = _Backend()
    stats = mark_read(settings, conn, backend, apply=True)
    assert stats.marked == 0
    assert backend.calls == [], "off 状态下不得发出任何 STORE"


def test_dry_run_sends_no_store(conn, tmp_path) -> None:
    """dry-run 只报告，**不发任何 STORE**。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1)
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    stats = mark_read(settings, conn, backend, apply=False)

    assert stats.dry_run is True
    assert stats.total_candidates == 1
    assert backend.calls == [], "dry-run 绝不允许写服务端"
    assert conn.execute(
        "SELECT marked_read_at FROM messages WHERE uid = 1"
    ).fetchone()["marked_read_at"] is None, "dry-run 也不得写库"


# ══════════════════════════════════════════════════════════════
# resolved 策略：有事件等你 → 保持未读
# ══════════════════════════════════════════════════════════════


def test_no_events_is_marked(conn, tmp_path) -> None:
    """没有任何事件的邮件（通知、回执）→ 处理完，可标已读。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    mid = _message(conn, uid=1)
    _sync_state(conn)
    conn.commit()

    stats = mark_read(settings, conn, _Backend(), apply=True)
    assert stats.marked == 1
    assert conn.execute(
        "SELECT marked_read_at FROM messages WHERE id = ?", (mid,)
    ).fetchone()["marked_read_at"] is not None


@pytest.mark.parametrize("status", ["pending", "uncertain", "push_failed"])
def test_blocking_event_status_keeps_unread(conn, tmp_path, status: str) -> None:
    """仍有等你处理的事件 → **保持未读**。

    这正是这个功能的意义所在：让「未读」继续表示「需要你处理」。
    """
    settings = _settings(tmp_path, mark_read_policy="resolved")
    mid = _message(conn, uid=1)
    _event(conn, mid, status)
    _sync_state(conn)
    conn.commit()

    stats = mark_read(settings, conn, _Backend(), apply=True)
    assert stats.marked == 0, f"{status} 状态的事件正等着你，不该标已读"


@pytest.mark.parametrize(
    "status", ["approved", "pushed", "rejected", "ignored", "cancelled"]
)
def test_resolved_event_statuses_allow_marking(conn, tmp_path, status: str) -> None:
    """你的那部分已经做完（批准/否决/已入历）→ 可以标已读。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    mid = _message(conn, uid=1)
    _event(conn, mid, status)
    _sync_state(conn)
    conn.commit()

    stats = mark_read(settings, conn, _Backend(), apply=True)
    assert stats.marked == 1, f"{status} 表示已处理完，应可标已读"


def test_processed_policy_ignores_event_status(conn, tmp_path) -> None:
    """``processed`` 策略只看抽取是否完成，不看事件状态。"""
    settings = _settings(tmp_path, mark_read_policy="processed")
    mid = _message(conn, uid=1)
    _event(conn, mid, "pending")
    _sync_state(conn)
    conn.commit()

    stats = mark_read(settings, conn, _Backend(), apply=True)
    assert stats.marked == 1


def test_unfinished_extraction_is_not_marked(conn, tmp_path) -> None:
    """抽取还没完成的邮件不能标——我们还没看过它。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1, extract_status="pending")
    _sync_state(conn)
    conn.commit()
    stats = mark_read(settings, conn, _Backend(), apply=True)
    assert stats.marked == 0


def test_already_seen_is_not_restored(conn, tmp_path) -> None:
    """服务端已是已读 → 不发 STORE（省一次往返），但记账以便区分。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1, flags="\\Seen")
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    stats = mark_read(settings, conn, backend, apply=True)
    assert backend.calls == [], "已是已读不必再写"
    assert stats.folders[0].already_seen == 1


# ══════════════════════════════════════════════════════════════
# 安全闸门
# ══════════════════════════════════════════════════════════════


def test_uidvalidity_change_blocks_writes(conn, tmp_path) -> None:
    """**最重要的闸门**：UIDVALIDITY 变化时拒绝回写。

    UIDVALIDITY 一变，UID 就全部重新分配了——库里存的 UID 可能指向完全不同的
    邮件。此时继续 STORE 会把**别人的邮件**标为已读。
    """
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1)
    _sync_state(conn, uid_validity=1)
    conn.commit()

    backend = _Backend(uid_validity=99)  # 服务端已重新分配
    stats = mark_read(settings, conn, backend, apply=True)

    assert stats.marked == 0
    assert backend.calls == [], "UIDVALIDITY 不一致时绝不能写"
    assert "UIDVALIDITY" in (stats.folders[0].skipped_reason or "")


def test_missing_sync_state_blocks_writes(conn, tmp_path) -> None:
    """没有同步记录 → 不知道 UIDVALIDITY → 不敢写。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1)
    conn.commit()

    backend = _Backend()
    stats = mark_read(settings, conn, backend, apply=True)
    assert backend.calls == []
    assert stats.folders[0].skipped_reason


def test_actionable_wording_keeps_unread(conn, tmp_path) -> None:
    """**实测踩到的坑**：没有日历事件 ≠ 没事要做。

    GitHub 的仓库邀请（``X invited you to X/repo``）不产生任何日历事件，
    按「无事件 → 已处理」的判据会被标为已读——而那正是一封需要回应的邮件。
    标了就等于让它从「未读」里消失。
    """
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1, subject="elias-wu invited you to elias-wu/repo")
    _message(conn, uid=2, subject="請確認您的電郵地址以完成登記")
    _message(conn, uid=3, subject="Action required: verify your account")
    _message(conn, uid=4, subject="您的 8 月電子月結單已備妥")
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    stats = mark_read(settings, conn, backend, apply=True)

    assert backend.calls == [([4], True)], (
        "只有纯通知类（月结单）该被标记；邀请/确认类必须保持未读"
    )
    assert stats.marked == 1


def test_actionable_filter_only_applies_to_resolved(conn, tmp_path) -> None:
    """``processed`` 是使用者明确选择的「抽取完就标」，不加待办筛选。"""
    settings = _settings(tmp_path, mark_read_policy="processed")
    _message(conn, uid=1, subject="elias-wu invited you to elias-wu/repo")
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    stats = mark_read(settings, conn, backend, apply=True)
    assert stats.marked == 1


def test_actionable_scan_ignores_body(tmp_path) -> None:
    """只扫主题与发件人，不扫正文。

    正文里「如有疑问请联系」「confirmation」这类词太普遍，扫正文会让几乎所有
    邮件都被判为待办，这道闸门就失效了。
    """
    from automail.read_state import _looks_actionable

    assert _looks_actionable("elias-wu invited you to x/y", "noreply@github.com")
    assert _looks_actionable("請確認電郵", "a@b.com")
    assert _looks_actionable("Action required", "a@b.com")
    assert not _looks_actionable("電子月結單已備妥", "bank@example.com")
    assert not _looks_actionable("Your statement is ready", "bank@example.com")


def test_stale_and_moved_messages_are_skipped(conn, tmp_path) -> None:
    """UIDVALIDITY 变过（stale）或已移出文件夹的邮件不处理。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1, stale=1)
    _message(conn, uid=2, folder_moved=1)
    _message(conn, uid=3)
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    stats = mark_read(settings, conn, backend, apply=True)
    assert stats.total_candidates == 1, "只有 UID 3 该被处理"
    assert backend.calls == [([3], True)]


def test_non_canonical_duplicates_are_skipped(conn, tmp_path) -> None:
    """副本不重复处理。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1, is_canonical=0)
    _sync_state(conn)
    conn.commit()
    stats = mark_read(settings, conn, _Backend(), apply=True)
    assert stats.total_candidates == 0


def test_readonly_selection_is_rejected_by_backend(conn, tmp_path) -> None:
    """只读选中的文件夹上 STORE 会被服务端拒绝——必须如实失败，不能假装成功。

    这条保证「忘记切到写模式」是一个显式错误，而不是静默无效。
    """
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1)
    _sync_state(conn)
    conn.commit()

    backend = _Backend()

    def _select(folder: str, *, readonly: bool = True) -> FolderStatus:
        backend.selected.append((folder, readonly))
        backend._readonly = True  # 服务端始终当只读（模拟忘切模式）
        return FolderStatus(uid_validity=1, uid_next=10, exists=1)

    backend.select_folder = _select  # type: ignore[method-assign]
    stats = mark_read(settings, conn, backend, apply=True)
    assert stats.marked == 0
    assert stats.folders[0].failed == 1
    assert stats.has_errors


def test_mark_read_selects_folder_writable(conn, tmp_path) -> None:
    """回写必须以**可写**方式选中文件夹（readonly=False）。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1)
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    mark_read(settings, conn, backend, apply=True)
    assert backend.selected == [("INBOX", False)], "回写必须用可写选中"


# ══════════════════════════════════════════════════════════════
# 幂等与回退
# ══════════════════════════════════════════════════════════════


def test_second_run_is_idempotent(conn, tmp_path) -> None:
    """已标记过的不再重复 STORE。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1)
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    first = mark_read(settings, conn, backend, apply=True)
    assert first.marked == 1

    backend.calls.clear()
    second = mark_read(settings, conn, backend, apply=True)
    assert second.marked == 0
    assert backend.calls == [], "第二轮不应再发 STORE"


def test_unmark_restores_only_our_marks(conn, tmp_path) -> None:
    """回退只动**我们标记过**的邮件，不碰用户自己读过的。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    ours = _message(conn, uid=1, marked_read_at=utcnow_iso())
    users = _message(conn, uid=2, flags="\\Seen")  # 用户自己在别处读的
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    stats = unmark_read(settings, conn, backend, apply=True)

    assert stats.marked == 1
    assert backend.calls == [([1], False)], "只应恢复 UID 1"
    assert conn.execute(
        "SELECT marked_read_at FROM messages WHERE id = ?", (ours,)
    ).fetchone()["marked_read_at"] is None
    # 用户自己读的那封完全不动
    assert conn.execute(
        "SELECT marked_read_at FROM messages WHERE id = ?", (users,)
    ).fetchone()["marked_read_at"] is None


def test_unmark_is_dry_run_by_default(conn, tmp_path) -> None:
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1, marked_read_at=utcnow_iso())
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    stats = unmark_read(settings, conn, backend, apply=False)
    assert stats.total_candidates == 1
    assert backend.calls == []


def test_undo_output_says_restored_not_marked_read(tmp_path) -> None:
    """**实测发现的措辞 bug**：撤销的输出必须说「恢复为未读」。

    撤销复用同一套统计，但方向相反。原先把 ``marked`` 一律渲染成
    「已在邮箱中标记 N 封为已读」——在撤销时这句话与事实**正好相反**，
    而这是唯一改变邮箱状态的操作，说反了会让人不敢用或误判结果。
    """
    import io

    from rich.console import Console

    from automail.read_state import MarkReadStats
    from automail.report import render_mark_read

    stats = MarkReadStats(policy="undo", dry_run=False)
    buf = io.StringIO()
    render_mark_read(stats, out=Console(file=buf, highlight=False))
    text = buf.getvalue()

    assert "未读" in text, f"撤销的输出必须说明是恢复未读：{text!r}"
    assert "标记 0 封为已读" not in text, f"不得说成「标记为已读」：{text!r}"


# ══════════════════════════════════════════════════════════════
# 分批与限额
# ══════════════════════════════════════════════════════════════


def test_batches_respect_configured_size(conn, tmp_path) -> None:
    """按 batch_size 分批下发 STORE——一次命令过大也会触发风控。"""
    settings = _settings(tmp_path, mark_read_policy="resolved", mark_read_batch_size=2)
    for uid in range(1, 6):
        _message(conn, uid=uid)
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    stats = mark_read(settings, conn, backend, apply=True)
    assert stats.marked == 5
    assert [len(u) for u, _ in backend.calls] == [2, 2, 1]


def test_limit_caps_candidates(conn, tmp_path) -> None:
    """limit 用于首次启用时小批量试跑。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    for uid in range(1, 6):
        _message(conn, uid=uid)
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    stats = mark_read(settings, conn, backend, apply=True, limit=2)
    assert stats.marked == 2


def test_failed_batch_does_not_abort_rest(conn, tmp_path) -> None:
    """单批失败不放弃整轮，但要如实记账（否则「多少没标成功」不可见）。"""
    settings = _settings(tmp_path, mark_read_policy="resolved", mark_read_batch_size=1)
    for uid in range(1, 4):
        _message(conn, uid=uid)
    _sync_state(conn)
    conn.commit()

    backend = _Backend()
    calls: list[int] = []

    def _mark(uids: list[int], *, seen: bool = True) -> None:
        calls.append(uids[0])
        if uids[0] == 2:
            raise MailProtocolError("boom")
        backend.calls.append((list(uids), seen))

    backend.mark_seen = _mark  # type: ignore[method-assign]
    stats = mark_read(settings, conn, backend, apply=True)

    assert stats.marked == 2, "UID 1 与 3 应成功"
    assert stats.folders[0].failed == 1, "失败的要计数"
    assert stats.folders[0].error


def test_backend_without_mark_seen_is_reported(conn, tmp_path) -> None:
    """后端不支持回写时明确失败，而不是静默跳过。"""
    settings = _settings(tmp_path, mark_read_policy="resolved")
    _message(conn, uid=1)
    _sync_state(conn)
    conn.commit()

    class _NoStore:
        def select_folder(self, folder: str, *, readonly: bool = True) -> FolderStatus:
            return FolderStatus(uid_validity=1, uid_next=10, exists=1)

    stats = mark_read(settings, conn, _NoStore(), apply=True)
    assert stats.folders[0].error
    assert stats.marked == 0


def test_mark_read_folder_defaults_to_synced_folders(tmp_path) -> None:
    """未单独配置时，回写范围跟随同步范围。"""
    s = _settings(tmp_path, imap_folders="INBOX,Archive")
    assert s.mark_read_folder_list == ["INBOX", "Archive"]

    s2 = _settings(tmp_path, imap_folders="INBOX,Archive", mark_read_folders="INBOX")
    assert s2.mark_read_folder_list == ["INBOX"]


# ══════════════════════════════════════════════════════════════
# 与假 IMAP 服务器的联通（真实协议往返）
# ══════════════════════════════════════════════════════════════


def test_end_to_end_against_fake_imap(conn, tmp_path) -> None:
    """走真实 IMAPClient 往返，确认 STORE 语法与状态变化都对。

    单测里的假后端无法验证「我们发出的 STORE 命令是否合法」——那正是
    ``UID SEARCH UID ALL`` 那次教训：假替身太宽松，真机才暴露。
    """
    from automail.mail.imap_backend import ImapBackend

    server = FakeImapServer()
    folder = FakeFolder(name="INBOX")
    folder.add(uid=1, raw=b"Subject: a\r\n\r\nbody")
    folder.add(uid=2, raw=b"Subject: b\r\n\r\nbody")
    server.folders["INBOX"] = folder
    port = server.start()

    settings = _settings(
        tmp_path,
        mark_read_policy="resolved",
        imap_host="127.0.0.1",
        imap_port=port,
        imap_user="user@163.com",
        imap_auth_code="authcode",
        imap_use_ssl=False,
    )
    _message(conn, uid=1)
    _sync_state(conn)
    conn.commit()

    try:
        with ImapBackend(settings) as backend:
            stats = mark_read(settings, conn, backend, apply=True)
    finally:
        server.stop()

    assert stats.marked == 1
    assert "\\Seen" in folder.flags[1], "服务端上该邮件应变为已读"
    assert "\\Seen" not in folder.flags[2], "未被选中的邮件不得被动到"
    assert folder.flags[2] == set()
    # STORE 确实发过，且只针对 UID 1
    assert server.store_calls == [("add", [1])]


def test_end_to_end_rejects_store_on_readonly(tmp_path) -> None:
    """只读选中时 STORE 必须被拒绝（真实协议层面验证）。"""
    from automail.mail.imap_backend import ImapBackend

    server = FakeImapServer()
    folder = FakeFolder(name="INBOX")
    folder.add(uid=1, raw=b"Subject: a\r\n\r\nbody")
    server.folders["INBOX"] = folder
    port = server.start()

    settings = _settings(
        tmp_path,
        imap_host="127.0.0.1",
        imap_port=port,
        imap_user="user@163.com",
        imap_auth_code="authcode",
        imap_use_ssl=False,
    )
    try:
        with ImapBackend(settings) as backend:
            backend.select_folder("INBOX", readonly=True)  # 只读
            with pytest.raises(MailProtocolError):
                backend.mark_seen([1], seen=True)
    finally:
        server.stop()
    assert folder.flags[1] == set()


def test_sync_stats_seen_detection_uses_flags(conn, tmp_path) -> None:
    """同步时已读判定与回写判定必须一致（都看 \\Seen）。"""
    server = FakeImapServer()
    folder = FakeFolder(name="INBOX")
    folder.add(uid=1, raw=b"Subject: a\r\n\r\nbody", flags={"\\Seen"})
    server.folders["INBOX"] = folder
    port = server.start()
    try:
        from automail.mail.imap_backend import ImapBackend

        settings = _settings(
            tmp_path,
            imap_host="127.0.0.1",
            imap_port=port,
            imap_user="user@163.com",
            imap_auth_code="authcode",
            imap_use_ssl=False,
        )
        with ImapBackend(settings) as backend:
            backend.select_folder("INBOX", readonly=True)
            flags = backend.fetch_flags([1])
            assert "\\Seen" in flags[1]
    finally:
        server.stop()
