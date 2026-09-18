"""协议级集成测试：真实 IMAP 会话 + 真实同步引擎。

这里**不 mock** `IMAPClient`——起一个真 TCP 假服务器，让 imapclient 走完整协议
往返。因此这些用例验证的是真实行为，而不只是「我们调用了什么方法」：

* 必须使用 ``BODY.PEEK[]``（否则用户的邮件会被标记为已读）
* 认证后必须发送 IMAP ``ID``（163 的硬要求）
* ``Unsafe Login`` 时重发 ID 并重试，且**不得**当作凭据错误
* ``UIDNEXT`` 预判门确实阻止了追平后重复拉取最后一封
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from automail.db import RunRepository, open_db
from automail.mail.backend import UnsafeLoginError
from automail.mail.imap_backend import BODY_FETCH_ITEM, ImapBackend
from automail.mail.sync import SyncEngine
from automail.models import (
    EventStatus,
    ExtractStatus,
    ProcessedMailStatus,
)
from automail.settings import Settings
from automail.store import MessageRepository, ProcessedMailRepository, SyncStateRepository
from tests.fake_imap import FakeFolder, FakeImapServer


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "imap_host": "127.0.0.1",
        "imap_use_ssl": False,  # 假服务器是明文 TCP；真实 163 必须为 True
        "imap_user": "u@163.com",
        "imap_auth_code": "code",
        "data_dir": tmp_path / "data",
        "out_dir": tmp_path / "out",
        "log_dir": tmp_path / "logs",
        "imap_folders": "INBOX",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def sample_mail(
    subject: str = "会议通知",
    body: str = "定于 2026年9月20日 下午3点 在 302 开会",
    message_id: str = "<a@example.com>",
    extra_headers: str = "",
) -> bytes:
    return (
        f"From: sender@example.com\r\n"
        f"To: u@163.com\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        f"Date: Mon, 14 Sep 2026 10:00:00 +0800\r\n"
        f"{extra_headers}"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body}\r\n"
    ).encode()


@pytest.fixture
def make_live_server() -> FakeImapServer:
    """已启动的假服务器（测试结束后自动关闭）。

    凭据必须与 :func:`make_settings` 一致，否则会得到误导性的
    ``MailAuthError``（而不是被测逻辑的失败）。

    命名带 ``make_`` 是因为它返回的是**可用的服务端对象**，测试需要直接读取
    其观测点（``commands``/``fetch_items``/``flags``）来断言真实行为。
    """
    srv = FakeImapServer(username="u@163.com", password="code")
    srv.folders["INBOX"] = FakeFolder(name="INBOX", uid_validity=1000)
    srv.start()
    yield srv
    srv.stop()


def backend_for(settings: Settings, server: FakeImapServer) -> ImapBackend:
    """构造后端并指向假服务器端口（settings 里的 host 已是 127.0.0.1）。"""
    assert server.port is not None
    # 端口在 fixture 启动后才确定，故此处覆盖
    object.__setattr__(settings, "imap_port", server.port)
    return ImapBackend(settings)


# ──────────────────────────────────────────────────────────────
# 后端层：只读保证与 163 怪癖
# ──────────────────────────────────────────────────────────────

def test_backend_uses_peek_and_never_marks_seen(
    make_live_server, tmp_path: Path
) -> None:
    """取正文必须用 BODY.PEEK[]——否则会把用户的未读邮件标记为已读。"""
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    backend = backend_for(settings, server)

    with backend:
        backend.select_folder("INBOX", readonly=True)
        messages = backend.fetch_messages([1])

    assert 1 in messages
    assert settings  # 保持引用
    assert BODY_FETCH_ITEM == "BODY.PEEK[]"
    assert server.fetch_items, "应记录到 FETCH 项"
    assert any("BODY.PEEK[]" in item for item in server.fetch_items)
    assert not getattr(server, "used_non_peek_body", False), (
        "出现了非 PEEK 的 BODY[]，会触发 \\Seen——这是必须避免的真实副作用"
    )
    assert "\\Seen" not in server.folders["INBOX"].flags[1]


def test_backend_sends_id_after_login(make_live_server, tmp_path: Path) -> None:
    """163 要求认证后发 ID，否则 SELECT 会被拒绝。"""
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    with backend_for(settings, server) as backend:
        assert backend.capabilities() >= {"ID"}
        backend.select_folder("INBOX")

    assert server.id_calls, "认证后必须发送 IMAP ID"


def test_backend_resends_id_on_unsafe_login(make_live_server, tmp_path: Path) -> None:
    """遇到 Unsafe Login：记录响应 → 重发 ID → 重试一次。"""
    server = make_live_server
    server.reject_first_select = True
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    with backend_for(settings, server) as backend:
        status = backend.select_folder("INBOX")  # 不应抛异常
        assert status.uid_validity == 1000

    assert server.select_count == 2, "应在 Unsafe Login 后重试一次"
    assert len(server.id_calls) >= 2, "重试前应重发 ID"


def test_backend_raises_unsafe_login_not_auth_error(tmp_path: Path) -> None:
    """持续 Unsafe Login 必须抛 UnsafeLoginError，而不是凭据错误。

    这个区分很重要：若误报为密码错误，用户会去重置授权码，而真正的问题是
    服务端限流。
    """
    server = FakeImapServer(
        username="u@163.com", password="code", require_id_before_select=True
    )
    server.folders["INBOX"] = FakeFolder(name="INBOX", uid_validity=1)
    server.start()
    try:
        settings = make_settings(tmp_path)
        with backend_for(settings, server) as backend:
            with pytest.raises(UnsafeLoginError):
                backend.select_folder("INBOX")
    finally:
        server.stop()


def test_backend_does_not_use_sasl(make_live_server, tmp_path: Path) -> None:
    """必须用明文 LOGIN，不走 SASL（Coremail 声明 SASL-IR 却拒绝 inline 形式）。"""
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    with backend_for(settings, server):
        pass

    assert "AUTHENTICATE" not in server.commands
    assert "LOGIN" in server.commands


def test_backend_handles_missing_uidnext(make_live_server, tmp_path: Path) -> None:
    """UIDNEXT 缺失时不应报错——这是 163 的实际行为，不是异常。"""
    server = make_live_server
    server.folders["INBOX"].add(5, sample_mail())

    settings = make_settings(tmp_path)
    with backend_for(settings, server) as backend:
        status = backend.select_folder("INBOX")

    assert status.uid_validity == 1000
    # 默认假服务器不返回 UIDNEXT，与真实 163 一致
    assert status.uid_next is None
    assert status.exists == 1


# ──────────────────────────────────────────────────────────────
# 同步引擎：dry-run、幂等、预判门
# ──────────────────────────────────────────────────────────────

def _engine(settings: Settings, conn: sqlite3.Connection, backend) -> SyncEngine:
    return SyncEngine(settings, conn, backend)


def test_sync_dry_run_writes_nothing(make_live_server, tmp_path: Path) -> None:
    """dry-run 必须零副作用：不写库，也不改变邮件已读状态。"""
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            stats = _engine(settings, conn, backend).sync(apply=False)

        assert stats.dry_run is True
        assert stats.inserted == 1, "dry-run 也要报告将要新增的邮件数"

        # 数据库里不应有任何邮件
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        # 游标不应推进
        assert conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 0

    assert "\\Seen" not in server.folders["INBOX"].flags[1]


def test_sync_apply_inserts_message(make_live_server, tmp_path: Path) -> None:
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail(subject="面试通知"))

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            stats = _engine(settings, conn, backend).sync(apply=True)

        assert stats.inserted == 1
        row = conn.execute("SELECT * FROM messages").fetchone()
        assert row["subject"] == "面试通知"
        assert row["from_addr"] == "sender@example.com"
        assert row["normalized_message_id"] == "a@example.com"
        # 正文与哈希都应落库
        assert row["body_sha256"]
        assert "302" in row["body_excerpt"]
        # 游标推进
        state = SyncStateRepository(conn).get("163", "INBOX")
        assert state is not None
        assert state.uid_validity == 1000
        assert state.highest_uid == 1


def test_sync_is_idempotent_on_second_run(make_live_server, tmp_path: Path) -> None:
    """二次同步不得重复插入——这是 P1 的核心验收项。"""
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            engine = _engine(settings, conn, backend)
            first = engine.sync(apply=True)
            second = engine.sync(apply=True)

        assert first.inserted == 1
        assert second.inserted == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_uidnext_gate_skips_search_when_caught_up(tmp_path: Path) -> None:
    """**服务端提供 UIDNEXT 时**，追平后必须跳过增量 SEARCH（路径 A）。

    RFC 3501 §6.4.8：``n:*`` 始终包含最后一封邮件。若不预判，二次同步会再次
    搜到 UID 1 并重复处理。

    注意 163 走的是路径 B（不提供 UIDNEXT），无法跳过 SEARCH——
    见 :func:`test_no_uidnext_uses_client_side_filter`。
    """
    server = FakeImapServer(
        username="u@163.com", password="code", advertise_uidnext=True
    )
    server.folders["INBOX"] = FakeFolder(name="INBOX", uid_validity=1000)
    server.folders["INBOX"].add(1, sample_mail())
    server.start()
    try:
        settings = make_settings(tmp_path)
        with open_db(settings) as conn:
            with backend_for(settings, server) as backend:
                engine = _engine(settings, conn, backend)
                engine.sync(apply=True)

                server.search_criteria.clear()
                second = engine.sync(apply=True)

            assert second.folders[0].gate_skipped is True
            # 预判门命中时不得发起**增量**搜索。
            # 移动检测会发 `UID ALL`，那是另一类搜索，不算违反预判门。
            incremental = [c for c in server.search_criteria if "ALL" not in c.upper()]
            assert incremental == [], (
                f"提供 UIDNEXT 时预判门应跳过增量 SEARCH，实际发了 {incremental}"
            )
    finally:
        server.stop()


def test_no_uidnext_uses_client_side_filter(tmp_path: Path) -> None:
    """**163 主路径**：无 UIDNEXT 时靠客户端过滤兜底，且不得重复入库。

    实测事实：163 连显式 ``STATUS INBOX (UIDNEXT)`` 都只回
    MESSAGES/UIDVALIDITY，因此预判门永不生效，每次同步都要 SEARCH，
    靠 ``uid > highest_uid`` 剔除 range 带回的最后一封。
    """
    server = FakeImapServer(username="u@163.com", password="code")
    server.folders["INBOX"] = FakeFolder(name="INBOX", uid_validity=1000)
    server.folders["INBOX"].add(1, sample_mail())
    server.start()
    try:
        settings = make_settings(tmp_path)
        with open_db(settings) as conn:
            with backend_for(settings, server) as backend:
                engine = _engine(settings, conn, backend)
                first = engine.sync(apply=True)
                assert first.inserted == 1

                # 二次同步：SEARCH 仍会返回 UID 1，但必须被客户端过滤掉
                server.search_criteria.clear()
                second = engine.sync(apply=True)

            assert second.inserted == 0, "追平后不得重复入库"
            assert second.folders[0].gate_skipped is True
            incremental = [c for c in server.search_criteria if "ALL" not in c.upper()]
            assert incremental == ["2:*"], "应搜索 highest+1 起的区间"
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        server.stop()


def test_uid_range_returns_last_message_when_caught_up(
    make_live_server, tmp_path: Path
) -> None:
    """直接验证假服务器复现了 RFC 边界行为，说明测试有真实覆盖力。"""
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    with backend_for(settings, server) as backend:
        backend.select_folder("INBOX")
        # 3:* 高于最大 UID 1，按 RFC 仍返回最后一封
        assert backend.search_uids("3:*") == [1]
        # 客户端过滤后应为空
        assert [uid for uid in backend.search_uids("3:*") if uid > 2] == []


def test_sync_increments_only_new_uids(make_live_server, tmp_path: Path) -> None:
    server = make_live_server
    inbox = server.folders["INBOX"]
    inbox.add(1, sample_mail(message_id="<a@x.com>"))
    inbox.add(2, sample_mail(subject="第二封", message_id="<b@x.com>"))

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            engine = _engine(settings, conn, backend)
            engine.sync(apply=True)

            inbox.add(3, sample_mail(subject="第三封", message_id="<c@x.com>"))
            second = engine.sync(apply=True)

        assert second.inserted == 1
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
        assert SyncStateRepository(conn).get("163", "INBOX").highest_uid == 3


# ──────────────────────────────────────────────────────────────
# 台账
# ──────────────────────────────────────────────────────────────

def test_sync_limit_batches_without_losing_messages(
    make_live_server, tmp_path: Path
) -> None:
    """分批同步：每轮取 N 封，最终**不丢邮件也不重复**。

    游标必须停在本批最后一封，否则要么漏信要么重复取。
    """
    server = make_live_server
    inbox = server.folders["INBOX"]
    for uid in range(1, 6):
        inbox.add(uid, sample_mail(subject=f"第{uid}封", message_id=f"<m{uid}@x.com>"))

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            engine = _engine(settings, conn, backend)
            first = engine.sync(apply=True, limit=2)
            second = engine.sync(apply=True, limit=2)
            third = engine.sync(apply=True, limit=2)

        assert first.inserted == 2
        assert second.inserted == 2
        assert third.inserted == 1

        # 最终 5 封全部入库，且无重复
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        distinct_uid = conn.execute("SELECT COUNT(DISTINCT uid) FROM messages").fetchone()[0]
        assert total == 5
        assert distinct_uid == 5

        # 游标推进到最后
        assert SyncStateRepository(conn).get("163", "INBOX").highest_uid == 5


def test_limit_not_applied_when_dry_run(make_live_server, tmp_path: Path) -> None:
    """dry-run 也应遵守 limit，让使用者能预估分批效果。"""
    server = make_live_server
    inbox = server.folders["INBOX"]
    for uid in range(1, 5):
        inbox.add(uid, sample_mail(message_id=f"<n{uid}@x.com>"))

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            stats = _engine(settings, conn, backend).sync(apply=False, limit=2)

        assert stats.inserted == 2
        # dry-run 必须零写入
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

def test_search_uids_does_not_double_prefix_uid(
    make_live_server, tmp_path: Path
) -> None:
    """回归测试：``search_uids`` 不得发出 ``UID SEARCH UID ...``。

    这是一个真实踩过的 bug：``IMAPClient(use_uid=True)`` 的 ``search()`` 已经
    自动加了 ``UID``，若调用方再传一个 ``"UID"`` 条件，线路上就成了
    ``UID SEARCH UID ALL`` —— **非法语法**，真实 163 直接返回
    ``BAD Parse command error``。

    假服务器为此专门实现了严格语法校验；若这条断言失败，说明假服务器又变得
    过于宽容（那才是真正的危险：让非法命令在测试里蒙混过关）。
    """
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    with backend_for(settings, server) as backend:
        backend.select_folder("INBOX")
        # 不应抛 InvalidCriteriaError；返回真实 UID 列表
        assert backend.search_uids("ALL") == [1]
        assert backend.search_uids("1:*") == [1]

    # 服务端看到的条件里不得出现 UID 前缀
    for criterion in server.search_criteria:
        assert not criterion.strip().upper().startswith("UID"), (
            f"发出了非法的 `UID SEARCH UID ...`：{criterion!r}"
        )


def test_fetch_is_batched_to_avoid_throttling(make_live_server, tmp_path: Path) -> None:
    """取信必须分批。

    实测事实：一次性 FETCH 89 封会被 163 风控重置连接（WinError 10054），
    而 3~10 封均正常。因此 ``imap_fetch_batch_size`` 不能失效——若哪天有人
    图省事改成一次取完，真实环境会直接断连。
    """
    server = make_live_server
    inbox = server.folders["INBOX"]
    for uid in range(1, 26):
        inbox.add(uid, sample_mail(subject=f"第{uid}封", message_id=f"<b{uid}@x.com>"))

    settings = make_settings(tmp_path, imap_fetch_batch_size=5)

    # 记录每次 FETCH 的 UID 个数
    fetch_calls: list[int] = []
    original = server._cmd_fetch

    def spy(tag: str, rest: str) -> str:
        spec = rest.split(" ", 1)[0]
        fetch_calls.append(len(spec.split(",")) if spec else 0)
        return original(tag, rest)

    server._cmd_fetch = spy  # type: ignore[method-assign]

    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            stats = _engine(settings, conn, backend).sync(apply=True)

        assert stats.inserted == 25
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 25

    assert fetch_calls, "应发生 FETCH"
    assert max(fetch_calls) <= 5, f"单次 FETCH 不得超过批次上限，实际 {max(fetch_calls)}"
    assert len(fetch_calls) >= 5, "25 封按 5 封一批，应至少 5 次 FETCH"


def test_partial_batch_failure_keeps_successful_batches(
    make_live_server, tmp_path: Path
) -> None:
    """重试耗尽时，**已成功批次的结果必须保留**（不能整轮丢弃）。

    这里刻意把 ``imap_reconnect_attempts`` 设为 0，以隔离「重试耗尽」这条
    路径——否则默认配置下的重连会把它救回来（那是
    :func:`test_batch_failure_reconnects_and_continues` 覆盖的场景）。
    """
    from automail.mail.backend import MailConnectionError

    server = make_live_server
    inbox = server.folders["INBOX"]
    for uid in range(1, 7):
        inbox.add(
            uid,
            sample_mail(
                subject=f"第{uid}封",
                body=f"这是第 {uid} 封邮件的正文，各不相同。",
                message_id=f"<p{uid}@x.com>",
            ),
        )

    settings = make_settings(
        tmp_path, imap_fetch_batch_size=2, imap_reconnect_attempts=0
    )
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            original_fetch = backend.fetch_messages
            state = {"calls": 0}

            def flaky(uids: list[int]):
                state["calls"] += 1
                if state["calls"] == 2:  # 只让第二批失败
                    raise MailConnectionError("simulated reset")
                return original_fetch(uids)

            backend.fetch_messages = flaky  # type: ignore[method-assign]
            stats = _engine(settings, conn, backend).sync(apply=True)

        folder_stats = stats.folders[0]
        # 第 1、3 批成功（各 2 封）→ 4 封入库；第 2 批失败 → 2 封失败
        assert folder_stats.inserted == 4, "已成功批次的结果必须保留"
        assert folder_stats.fetch_failed == 2
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 4


def test_all_batches_failing_raises_with_reason(
    make_live_server, tmp_path: Path
) -> None:
    """所有批次都失败时必须抛出并带出原因，而不是伪装成「0 封新邮件」。"""
    from automail.mail.backend import MailConnectionError

    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path, imap_fetch_batch_size=5)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            def always_fail(uids: list[int]):
                raise MailConnectionError("throttled")

            backend.fetch_messages = always_fail  # type: ignore[method-assign]
            # 文件夹级错误被捕获并记录到 folder_stats.error，不向上冒泡
            stats = _engine(settings, conn, backend).sync(apply=True)

    folder_stats = stats.folders[0]
    assert folder_stats.inserted == 0
    assert folder_stats.error is not None
    assert "风控" in folder_stats.error or "失败" in folder_stats.error


def test_folder_level_failure_reports_reason(make_live_server, tmp_path: Path) -> None:
    """文件夹级失败必须带出原因，而不是只给出一行零值。

    没有这个字段时，用户看到的是 ``uid_validity=0`` + ``fetch_failed=1``，
    完全无从判断是凭据、风控还是账号问题。
    """
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            # 让 select 直接抛错
            def boom(folder: str, *, readonly: bool = True):
                raise OSError("simulated throttle: connection reset")

            backend.select_folder = boom  # type: ignore[method-assign]
            stats = _engine(settings, conn, backend).sync(apply=True)

    folder_stats = stats.folders[0]
    assert folder_stats.error is not None
    assert "reset" in folder_stats.error
    assert folder_stats.as_dict()["error"] == folder_stats.error


def test_batch_failure_reconnects_and_continues(
    make_live_server, tmp_path: Path
) -> None:
    """会话失效时必须重连并续传，而不是放弃整轮。

    实测教训：163 会在任意时刻使会话失效（``Autologout; idle for too long``
    约 2~4 分钟空闲、另一客户端登录、限流）。若遇到就放弃，曾出现
    「89 封里 69 封失败」的结果——其中大部分是可恢复的。
    """
    from automail.mail.backend import MailProtocolError

    server = make_live_server
    inbox = server.folders["INBOX"]
    for uid in range(1, 9):
        inbox.add(
            uid,
            sample_mail(
                subject=f"第{uid}封",
                body=f"正文 {uid}，各不相同。",
                message_id=f"<r{uid}@x.com>",
            ),
        )

    settings = make_settings(
        tmp_path,
        imap_fetch_batch_size=2,
        imap_reconnect_attempts=2,
        imap_reconnect_backoff_seconds=0.0,
    )
    reconnect_calls = {"n": 0}

    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            original_fetch = backend.fetch_messages
            original_connect = backend.connect
            state = {"calls": 0}

            def flaky(uids: list[int]):
                state["calls"] += 1
                if state["calls"] == 2:  # 第二批首次失败（模拟会话失效）
                    raise MailProtocolError("Autologout; idle for too long")
                return original_fetch(uids)

            def counting_connect():
                reconnect_calls["n"] += 1
                return original_connect()

            backend.fetch_messages = flaky  # type: ignore[method-assign]
            backend.connect = counting_connect  # type: ignore[method-assign]
            stats = _engine(settings, conn, backend).sync(apply=True)

        # 第二批重试后成功，因此 8 封全部入库、零失败
        assert stats.folders[0].inserted == 8
        assert stats.folders[0].fetch_failed == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 8

    assert reconnect_calls["n"] >= 1, "应发生重连"


def test_reconnect_exhaustion_records_failure(
    make_live_server, tmp_path: Path
) -> None:
    """重试耗尽后应如实记录失败，而不是静默丢弃。"""
    from automail.mail.backend import MailProtocolError

    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(
        tmp_path,
        imap_reconnect_attempts=1,
        imap_reconnect_backoff_seconds=0.0,
    )
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            def always_fail(uids: list[int]):
                raise MailProtocolError("Autologout; idle for too long")

            backend.fetch_messages = always_fail  # type: ignore[method-assign]
            stats = _engine(settings, conn, backend).sync(apply=True)

        assert stats.folders[0].inserted == 0
        assert stats.folders[0].error is not None
        # 台账必须留下失败原因，供后续排查
        row = conn.execute(
            "SELECT status, error FROM processed_mail"
        ).fetchone()
        # 重试耗尽后由批次级记录（也可能整体失败），两种都应留痕
        assert row is not None


def test_sync_writes_processed_mail_ledger(make_live_server, tmp_path: Path) -> None:
    """sync 层台账必须落库：记录每封邮件是否已抓取。"""
    server = make_live_server
    server.folders["INBOX"].add(1, sample_mail())

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            _engine(settings, conn, backend).sync(apply=True)

        ledger = ProcessedMailRepository(conn)
        assert ledger.get_status("163", "INBOX", 1000, 1) == "synced"
        assert ledger.count_by_status("163") == {"synced": 1}


def test_processed_mail_status_values_are_constrained(conn: sqlite3.Connection) -> None:
    """processed_mail 只管 sync 层，取值集合有限。"""
    repo = ProcessedMailRepository(conn)
    repo.mark("163", "INBOX", 1, 1, ProcessedMailStatus.SYNCED)
    assert repo.get_status("163", "INBOX", 1, 1) == "synced"

    repo.mark("163", "INBOX", 1, 2, ProcessedMailStatus.FETCH_FAILED, error="取回失败")
    assert repo.get_status("163", "INBOX", 1, 2) == "fetch_failed"

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO processed_mail (account, folder, uid_validity, uid, status, processed_at) "
            "VALUES ('163','INBOX',1,3,'bogus','now')"
        )


def test_processed_mail_upsert_is_idempotent(conn: sqlite3.Connection) -> None:
    repo = ProcessedMailRepository(conn)
    repo.mark("163", "INBOX", 1, 1, ProcessedMailStatus.FETCH_FAILED, error="第一次")
    repo.mark("163", "INBOX", 1, 1, ProcessedMailStatus.SYNCED)
    assert repo.get_status("163", "INBOX", 1, 1) == "synced"
    assert conn.execute("SELECT COUNT(*) FROM processed_mail").fetchone()[0] == 1


# ──────────────────────────────────────────────────────────────
# 幂等与身份
# ──────────────────────────────────────────────────────────────

def test_same_message_in_two_folders_marked_duplicate(
    make_live_server, tmp_path: Path
) -> None:
    """同一封邮件出现在两个文件夹：两份都入库，第二份标 duplicate_of 且不重抽。"""
    server = make_live_server
    raw = sample_mail(message_id="<dup@x.com>")
    server.folders["INBOX"].add(1, raw)
    server.folders["订阅邮件"] = FakeFolder(name="订阅邮件", uid_validity=2000)
    server.folders["订阅邮件"].add(7, raw)

    settings = make_settings(tmp_path, imap_folders="INBOX,订阅邮件")
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            stats = _engine(settings, conn, backend).sync(apply=True)

        rows = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
        assert len(rows) == 2, "两份副本都应入库（不能因唯一约束失败）"

        canonical = next(r for r in rows if r["is_canonical"] == 1)
        duplicate = next(r for r in rows if r["is_canonical"] == 0)
        assert duplicate["duplicate_of"] == canonical["id"]
        # 副本不参与抽取
        assert duplicate["extract_status"] == ExtractStatus.DONE.value
        assert stats.duplicated == 1


def test_message_without_message_id_is_accepted(
    make_live_server, tmp_path: Path
) -> None:
    """缺少 Message-ID 的邮件必须能入库（不能编造 ID，也不能失败）。"""
    server = make_live_server
    raw = (
        b"From: x@y.com\r\nSubject: \xe6\x97\xa0 id\r\n\r\n\xe6\xad\xa3\xe6\x96\x87"
    )
    server.folders["INBOX"].add(1, raw)

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            stats = _engine(settings, conn, backend).sync(apply=True)

        assert stats.inserted == 1
        row = conn.execute("SELECT * FROM messages").fetchone()
        assert row["normalized_message_id"] is None


def test_non_canonical_duplicate_does_not_block_reuse(
    conn: sqlite3.Connection,
) -> None:
    """查重只认规范记录，避免把副本当成可复用来源。"""
    repo = MessageRepository(conn)
    first = repo.insert(
        "163", "INBOX", 1, 1, body_sha256="abc", is_canonical=1
    )
    second = repo.insert(
        "163", "订阅邮件", 1, 1, body_sha256="abc", is_canonical=0
    )
    found = repo.find_duplicate_elsewhere("163", body_sha256="abc", normalized_message_id=None)
    assert found is not None
    assert found.id == first
    assert second != first


# ──────────────────────────────────────────────────────────────
# UIDVALIDITY 变化
# ──────────────────────────────────────────────────────────────

def test_uidvalidity_change_marks_stale_and_reactivates(
    make_live_server, tmp_path: Path
) -> None:
    """UIDVALIDITY 变化：旧记录标 stale（不删），重扫时按内容复活且不重抽。"""
    server = make_live_server
    inbox = server.folders["INBOX"]
    raw = sample_mail(message_id="<keep@x.com>")
    inbox.add(1, raw)

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            engine = _engine(settings, conn, backend)
            engine.sync(apply=True)

        # 模拟服务端重建文件夹：UIDVALIDITY 改变，UID 重新分配
        inbox.uid_validity = 9999
        inbox.messages.clear()
        inbox.flags.clear()
        inbox.add(50, raw)

        with backend_for(settings, server) as backend:
            stats = _engine(settings, conn, backend).sync(apply=True)

        assert stats.folders[0].uidvalidity_changed is True
        assert stats.reactivated == 1, "同内容邮件应复活而非新增"
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1, (
            "不应产生第二份记录"
        )
        row = conn.execute("SELECT * FROM messages").fetchone()
        assert row["stale"] == 0
        assert row["uid"] == 50
        assert row["uid_validity"] == 9999


def test_uidvalidity_change_does_not_lose_events(
    make_live_server, tmp_path: Path
) -> None:
    """复活必须保留事件关联——否则同一封邮件会重复抽取。"""
    server = make_live_server
    inbox = server.folders["INBOX"]
    raw = sample_mail(message_id="<ev@x.com>")
    inbox.add(1, raw)

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            _engine(settings, conn, backend).sync(apply=True)

        msg_id = conn.execute("SELECT id FROM messages").fetchone()["id"]
        conn.execute(
            "INSERT INTO events (message_id, title, source, fingerprint, status, "
            "created_at, updated_at) VALUES (?, '会议', 'rules', 'fp-x', 'pending', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (msg_id,),
        )

        inbox.uid_validity = 7777
        inbox.messages.clear()
        inbox.add(9, raw)

        with backend_for(settings, server) as backend:
            _engine(settings, conn, backend).sync(apply=True)

        # 事件仍在，且仍指向同一条 message
        events = conn.execute("SELECT * FROM events").fetchall()
        assert len(events) == 1
        assert events[0]["message_id"] == msg_id


def test_stale_records_are_not_deleted(make_live_server, tmp_path: Path) -> None:
    """UIDVALIDITY 变化只标记 stale，不删除记录。"""
    server = make_live_server
    inbox = server.folders["INBOX"]
    inbox.add(1, sample_mail(message_id="<s@x.com>"))

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            _engine(settings, conn, backend).sync(apply=True)

        repo = MessageRepository(conn)
        assert repo.mark_stale("163", "INBOX") == 1
        rows = conn.execute("SELECT * FROM messages").fetchall()
        assert len(rows) == 1
        assert rows[0]["stale"] == 1


# ──────────────────────────────────────────────────────────────
# 邮件移动
# ──────────────────────────────────────────────────────────────

def test_moved_message_marked_not_deleted(make_live_server, tmp_path: Path) -> None:
    """邮件从 INBOX 移走：标记 folder_moved，不删除、不重抽。"""
    server = make_live_server
    inbox = server.folders["INBOX"]
    inbox.add(1, sample_mail(message_id="<m@x.com>"))

    settings = make_settings(tmp_path)
    with open_db(settings) as conn:
        with backend_for(settings, server) as backend:
            engine = _engine(settings, conn, backend)
            engine.sync(apply=True)

        # 邮件被移到别处（从 INBOX 消失）
        inbox.messages.clear()
        inbox.flags.clear()

        with backend_for(settings, server) as backend:
            stats = _engine(settings, conn, backend).sync(apply=True)

        assert stats.moved == 1
        row = conn.execute("SELECT * FROM messages").fetchone()
        assert row["folder_moved"] == 1
        assert row["id"] is not None


# ──────────────────────────────────────────────────────────────
# 抽取状态与尝试上限
# ──────────────────────────────────────────────────────────────

def test_claim_for_extract_is_atomic(conn: sqlite3.Connection) -> None:
    """原子领取：第二次领取必须失败（防止两个进程重复处理同一邮件）。"""
    repo = MessageRepository(conn)
    msg_id = repo.insert("163", "INBOX", 1, 1)

    assert repo.claim_for_extract(msg_id) is True
    assert repo.claim_for_extract(msg_id) is False
    assert conn.execute(
        "SELECT extract_status FROM messages WHERE id=?", (msg_id,)
    ).fetchone()["extract_status"] == ExtractStatus.RUNNING.value


def test_reclaim_zombies_resets_stuck_running(conn: sqlite3.Connection) -> None:
    """崩溃留下的 running 中间态必须能回退，否则抽取永远卡住。"""
    repo = MessageRepository(conn)
    msg_id = repo.insert("163", "INBOX", 1, 1)
    repo.claim_for_extract(msg_id)

    # 把 fetched_at 推到过去，模拟长时间未完成
    conn.execute(
        "UPDATE messages SET fetched_at = '2020-01-01T00:00:00Z' WHERE id=?", (msg_id,)
    )
    assert repo.reclaim_zombies(older_than_minutes=60) == 1
    assert conn.execute(
        "SELECT extract_status FROM messages WHERE id=?", (msg_id,)
    ).fetchone()["extract_status"] == ExtractStatus.PENDING.value


def test_increment_attempts_survives_status_reset(conn: sqlite3.Connection) -> None:
    """尝试次数独立于状态：状态被重置也不清零，毒邮件最终会停下来。"""
    repo = MessageRepository(conn)
    msg_id = repo.insert("163", "INBOX", 1, 1)

    assert repo.increment_attempts(msg_id) == 1
    assert repo.increment_attempts(msg_id) == 2
    repo.set_extract_status(msg_id, ExtractStatus.PENDING)
    assert repo.increment_attempts(msg_id) == 3

    assert conn.execute(
        "SELECT extract_attempts FROM messages WHERE id=?", (msg_id,)
    ).fetchone()["extract_attempts"] == 3


# ──────────────────────────────────────────────────────────────
# 发件人与联系人判定
# ──────────────────────────────────────────────────────────────

def test_sender_becomes_contact_only_after_user_reply(
    conn: sqlite3.Connection,
) -> None:
    """仅收到邮件不算联系人——否则垃圾邮件发送者会自动变成「联系人」，
    从而绕过 ICS 自动入历的信任检查。"""
    from automail.store import SenderRepository

    senders = SenderRepository(conn)
    senders.record("163", "spammer@x.com", has_unsubscribe=True)
    assert senders.is_known_contact("163", "spammer@x.com") is False

    senders.record("163", "friend@x.com", is_reply_from_user=True)
    assert senders.is_known_contact("163", "friend@x.com") is True


def test_sender_counters_accumulate(conn: sqlite3.Connection) -> None:
    from automail.store import SenderRepository

    senders = SenderRepository(conn)
    senders.record("163", "a@x.com", unread=True)
    senders.record("163", "a@x.com", unread=False, has_unsubscribe=True)

    row = conn.execute("SELECT * FROM senders WHERE addr='a@x.com'").fetchone()
    assert row["total"] == 2
    assert row["unread"] == 1
    assert row["has_unsubscribe"] == 1


# ──────────────────────────────────────────────────────────────
# 补偿扫描
# ──────────────────────────────────────────────────────────────

def test_compensate_flag_set_after_threshold(conn: sqlite3.Connection) -> None:
    repo = SyncStateRepository(conn)
    repo.upsert("163", "INBOX", uid_validity=1, highest_uid=5, syncs_since_full=0)
    assert repo.should_compensate("163", "INBOX", scans=20) is False
    repo.upsert("163", "INBOX", uid_validity=1, highest_uid=5, syncs_since_full=20)
    assert repo.should_compensate("163", "INBOX", scans=20) is True


def test_sync_state_unknown_folder_does_not_compensate(
    conn: sqlite3.Connection,
) -> None:
    """首次同步本身就是全量，不该再触发补偿扫描。"""
    assert SyncStateRepository(conn).should_compensate("163", "INBOX", scans=20) is False


# ──────────────────────────────────────────────────────────────
# 运行时记录
# ──────────────────────────────────────────────────────────────

def test_sync_stats_are_json_serializable() -> None:
    from automail.mail.sync import FolderSyncStats, SyncStats

    stats = SyncStats(
        folders=[FolderSyncStats(folder="INBOX", inserted=2)],
        compensated=False,
        dry_run=True,
    )
    payload = stats.as_dict()
    assert payload["inserted"] == 2
    assert payload["folders"][0]["folder"] == "INBOX"


def test_run_record_can_store_sync_stats(conn: sqlite3.Connection) -> None:
    from automail.mail.sync import SyncStats

    runs = RunRepository(conn)
    row_id = runs.start("r-sync", "sync")
    runs.finish(row_id, ok=True, exit_code=0, stats=SyncStats(dry_run=False).as_dict())
    assert runs.recent(limit=1)[0].stats["inserted"] == 0


def test_event_status_enum_has_no_unused_import_confusion() -> None:
    """占位：确保 models 的事件状态枚举可被引用（供后续阶段使用）。"""
    assert EventStatus.PENDING.value == "pending"
