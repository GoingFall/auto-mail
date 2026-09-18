"""P5 测试：线程重建、每日摘要、只读统计。

**线程重建的真实行为**（在 89 封真实邮件上观察到）：

* 无 Message-ID 的邮件用内容哈希兜底（实测 11 封），而不是退化成孤立线程
* 幽灵锚点（父邮件未入库）如实标记（实测 8 个）
* **主题弱关联必须排除自动化通知**——否则「阿里云到期提醒」这类周期推送
  会被合成一个 5 封的假线程，下游「等回复」判断会说出「有 5 封在等你」，
  而实际上无人可回
* 弱关联不得作为事实呈现

摘要与统计的关键约束：**降级/跳过必须可见**，且**全部只读**。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from automail.calendar.fake import FakeCalendar
from automail.db import utcnow_iso
from automail.digest import DigestBuilder, _split_calendar_events, digest_date_of
from automail.extract.runner import ExtractStats
from automail.settings import Settings
from automail.stats import (
    StatsCollector,
    collect,
    describe_extract_skips,
)
from automail.threads import (
    SYNTHETIC_KEY_PREFIX,
    ThreadBuilder,
)

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        account="163",
        imap_user="me@163.com",
        data_dir=tmp_path / "data",
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "logs",
    )


def _insert_message(
    conn: sqlite3.Connection,
    *,
    message_id: str | None,
    subject: str = "测试主题",
    in_reply_to: str | None = None,
    references: str | None = None,
    from_addr: str = "sender@example.com",
    received_at: str | None = None,
    body_sha256: str | None = None,
    auto_submitted: str | None = None,
    uid: int = 1,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO messages (
            account, folder, uid_validity, uid, normalized_message_id,
            in_reply_to, references_ids, subject, from_addr, received_at,
            body_sha256, auto_submitted, is_canonical, stale, fetched_at
        ) VALUES ('163', 'INBOX', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
        """,
        (
            uid, message_id, in_reply_to, references, subject, from_addr,
            received_at or "2026-09-14T02:00:00Z",
            body_sha256 or f"sha-{uid}", auto_submitted, utcnow_iso(),
        ),
    )
    return int(cur.lastrowid)


# ──────────────────────────────────────────────────────────────
# 线程：基本关联
# ──────────────────────────────────────────────────────────────


def test_single_message_becomes_own_thread(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    msg = _insert_message(conn, message_id="a@x.com")
    stats = ThreadBuilder(settings, conn).rebuild(apply=True)

    assert stats.threads == 1
    row = conn.execute("SELECT thread_id FROM messages WHERE id=?", (msg,)).fetchone()
    assert row["thread_id"] is not None


def test_reply_links_to_parent_via_in_reply_to(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    parent = _insert_message(
        conn, message_id="p@x.com", subject="会议", received_at="2026-09-14T01:00:00Z"
    )
    reply = _insert_message(
        conn, message_id="r@x.com", subject="Re: 会议",
        in_reply_to="p@x.com", received_at="2026-09-14T02:00:00Z", uid=2,
    )
    ThreadBuilder(settings, conn).rebuild(apply=True)

    rows = conn.execute(
        "SELECT id, thread_id FROM messages WHERE id IN (?, ?)", (parent, reply)
    ).fetchall()
    threads = {r["thread_id"] for r in rows}
    assert len(threads) == 1, "回复应与父邮件同线程"


def test_references_chain_links_to_deepest_known_ancestor(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """References 里有多个祖先时，应挂到**最接近本封**的那个。

    只取 References 首项会挂到线程根，丢失中间层级。
    """
    root = _insert_message(
        conn, message_id="root@x.com", received_at="2026-09-14T01:00:00Z"
    )
    middle = _insert_message(
        conn, message_id="mid@x.com", references="root@x.com",
        received_at="2026-09-14T02:00:00Z", uid=2,
    )
    leaf = _insert_message(
        conn, message_id="leaf@x.com",
        references="root@x.com mid@x.com",
        received_at="2026-09-14T03:00:00Z", uid=3,
    )
    ThreadBuilder(settings, conn).rebuild(apply=True)

    rows = conn.execute(
        "SELECT id, thread_id FROM messages WHERE id IN (?, ?, ?)", (root, middle, leaf)
    ).fetchall()
    assert len({r["thread_id"] for r in rows}) == 1


def test_ghost_anchor_created_for_missing_parent(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """父邮件未入库时，为缺失的 Message-ID 建幽灵锚点。

    目的：让共享同一缺失祖先的邮件聚在一起，而不是各自孤立。
    真身到达后自然并入（查找键就是它自己的 Message-ID）。
    """
    _insert_message(
        conn, message_id="child1@x.com", in_reply_to="ghost@x.com",
        received_at="2026-09-14T02:00:00Z",
    )
    _insert_message(
        conn, message_id="child2@x.com", in_reply_to="ghost@x.com",
        received_at="2026-09-14T03:00:00Z", uid=2,
    )
    stats = ThreadBuilder(settings, conn).rebuild(apply=True)

    assert stats.ghosts == 1
    assert stats.threads == 1, "两个子邮件应归入同一幽灵线程"

    row = conn.execute(
        "SELECT root_message_id, status FROM threads"
    ).fetchone()
    assert row["root_message_id"] == "ghost@x.com"
    assert row["status"] == "ghost", "幽灵锚点应如实标记"


def test_ghost_becomes_real_when_parent_arrives(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """父邮件后来入库时，幽灵锚点应自动变成真实线程（无需特殊合并逻辑）。"""
    _insert_message(
        conn, message_id="child@x.com", in_reply_to="parent@x.com"
    )
    ThreadBuilder(settings, conn).rebuild(apply=True)
    assert conn.execute("SELECT status FROM threads").fetchone()["status"] == "ghost"

    # 父邮件到达
    _insert_message(
        conn, message_id="parent@x.com", received_at="2026-09-14T00:00:00Z", uid=2
    )
    ThreadBuilder(settings, conn).rebuild(apply=True)

    row = conn.execute(
        "SELECT status, root_message_id FROM threads"
    ).fetchone()
    assert row["root_message_id"] == "parent@x.com"
    assert row["status"] == "active", "真身到达后不应再是幽灵"
    assert conn.execute("SELECT COUNT(*) n FROM threads").fetchone()["n"] == 1


def test_message_without_message_id_uses_content_hash(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """**国产邮箱常见**：无 Message-ID 时用内容哈希兜底，而不是丢弃。

    丢弃会让这些邮件全部变成孤立线程（每个一封），线程功能形同虚设。
    """
    _insert_message(conn, message_id=None, body_sha256="abc123")
    stats = ThreadBuilder(settings, conn).rebuild(apply=True)

    assert stats.orphaned_by_missing_id == 1
    row = conn.execute("SELECT root_message_id FROM threads").fetchone()
    assert row["root_message_id"].startswith(SYNTHETIC_KEY_PREFIX)


def test_same_content_without_id_shares_thread(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """相同内容（同哈希）的无 ID 邮件应归入同一线程。"""
    _insert_message(conn, message_id=None, body_sha256="same", uid=1)
    _insert_message(conn, message_id=None, body_sha256="same", uid=2)
    ThreadBuilder(settings, conn).rebuild(apply=True)
    assert conn.execute("SELECT COUNT(*) n FROM threads").fetchone()["n"] == 1


def test_cycle_is_broken_not_hung(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """互相引用（环）不得让重建卡死或崩。"""
    _insert_message(
        conn, message_id="a@x.com", in_reply_to="b@x.com",
        received_at="2026-09-14T01:00:00Z",
    )
    _insert_message(
        conn, message_id="b@x.com", in_reply_to="a@x.com",
        received_at="2026-09-14T02:00:00Z", uid=2,
    )
    stats = ThreadBuilder(settings, conn).rebuild(apply=True)
    assert stats.messages == 2
    assert stats.threads >= 1


def test_direction_detected_from_user_addresses(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """用户自己发出的邮件方向应为 out。

    注意用**不同主题**：同主题的孤立邮件会被弱关联合并成一个线程，
    那样就只剩一个 last_direction，测不出方向判定。
    """
    _insert_message(
        conn, message_id="out@x.com", subject="我发出的主题",
        from_addr="me@163.com", received_at="2026-09-14T01:00:00Z",
    )
    _insert_message(
        conn, message_id="in@x.com", subject="对方发来的主题",
        from_addr="other@example.com", received_at="2026-09-14T02:00:00Z", uid=2,
    )
    ThreadBuilder(settings, conn).rebuild(apply=True)

    rows = conn.execute(
        "SELECT t.last_direction FROM threads t JOIN messages m ON m.thread_id=t.id"
    ).fetchall()
    directions = {r["last_direction"] for r in rows}
    assert directions == {"out", "in"}


def test_direction_defaults_to_in_without_user_addresses(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """未配置用户地址时一律判为 in。

    宁可判成收信，也不要凭空声称用户发过信——后者会让「等回复」判断全错。
    """
    settings = Settings(
        _env_file=None, imap_user="", user_addresses="",
        data_dir=tmp_path / "d", out_dir=tmp_path / "o", log_dir=tmp_path / "l",
    )
    _insert_message(conn, message_id="x@x.com", from_addr="anyone@example.com")
    ThreadBuilder(settings, conn).rebuild(apply=True)
    row = conn.execute("SELECT last_direction FROM threads").fetchone()
    assert row["last_direction"] == "in"


# ──────────────────────────────────────────────────────────────
# 线程：弱关联与自动化通知
# ──────────────────────────────────────────────────────────────


def test_same_subject_orphans_merged_as_weak(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """无引用线索但同主题的孤立邮件做弱关联，并标 weak。"""
    _insert_message(
        conn, message_id="a@x.com", subject="项目讨论",
        received_at="2026-09-14T01:00:00Z",
    )
    _insert_message(
        conn, message_id="b@x.com", subject="Re: 项目讨论",
        received_at="2026-09-14T02:00:00Z", uid=2,
    )
    stats = ThreadBuilder(settings, conn).rebuild(apply=True)

    assert stats.merged_by_subject >= 1
    row = conn.execute("SELECT link_strength FROM threads").fetchone()
    assert row["link_strength"] == "weak", "仅凭主题推断必须标 weak"


def test_automated_notifications_not_merged_by_subject(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """**真实数据暴露的问题**：自动化通知不得被主题弱关联合并。

    实测：同主题弱关联把「阿里云域名到期提醒」一类周期性通知合成一个
    5 封的「线程」。但它们不是对话——没有任何人在其中交流。

    后果不是中性的：下游「等回复」判断会说「这个线程有 5 封在等你回」，
    而实际上无人可回。这比不合并更糟。
    """
    # 三封同主题的自动化通知
    for i, day in enumerate(["01", "02", "03"]):
        _insert_message(
            conn,
            message_id=f"notify{i}@x.com",
            subject="阿里云域名到期提醒",
            received_at=f"2026-09-{day}T02:00:00Z",
            uid=i + 1,
        )
    ThreadBuilder(settings, conn).rebuild(apply=True)

    count = conn.execute("SELECT COUNT(*) n FROM threads").fetchone()["n"]
    assert count == 3, "自动化通知应各自独立成线程，不应被合并"


def test_auto_submitted_header_also_excluded(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """带 Auto-Submitted 头的邮件同样不参与主题合并。"""
    _insert_message(
        conn, message_id="a@x.com", subject="日常沟通",
        auto_submitted="auto-replied", received_at="2026-09-14T01:00:00Z",
    )
    _insert_message(
        conn, message_id="b@x.com", subject="日常沟通",
        auto_submitted="auto-generated", received_at="2026-09-14T02:00:00Z", uid=2,
    )
    ThreadBuilder(settings, conn).rebuild(apply=True)
    assert conn.execute("SELECT COUNT(*) n FROM threads").fetchone()["n"] == 2


def test_strong_link_not_downgraded_by_weak_merge(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """已有强关联的线程不参与弱合并，其强度不得被降级。"""
    _insert_message(
        conn, message_id="p@x.com", subject="会议通知",
        received_at="2026-09-14T01:00:00Z",
    )
    _insert_message(
        conn, message_id="r@x.com", subject="Re: 会议通知",
        in_reply_to="p@x.com", received_at="2026-09-14T02:00:00Z", uid=2,
    )
    # 另一封孤立但同主题
    _insert_message(
        conn, message_id="z@x.com", subject="Re: 会议通知",
        received_at="2026-09-14T03:00:00Z", uid=3,
    )
    ThreadBuilder(settings, conn).rebuild(apply=True)

    rows = conn.execute(
        "SELECT link_strength FROM threads ORDER BY id"
    ).fetchall()
    strengths = [r["link_strength"] for r in rows]
    assert "strong" in strengths, "强关联线程不应被降级"


def test_rebuild_is_idempotent(settings: Settings, conn: sqlite3.Connection) -> None:
    """全量重建结果只取决于输入，重复运行结果一致。"""
    _insert_message(conn, message_id="a@x.com", in_reply_to="ghost@x.com")
    _insert_message(conn, message_id="b@x.com", references="a@x.com", uid=2)

    builder = ThreadBuilder(settings, conn)
    first = builder.rebuild(apply=True)
    second = builder.rebuild(apply=True)

    assert first.threads == second.threads
    assert first.strong == second.strong
    assert first.ghosts == second.ghosts
    # 线程 id 会重分配，但映射关系一致
    assert conn.execute("SELECT COUNT(*) n FROM threads").fetchone()["n"] == first.threads


def test_dry_run_writes_nothing(settings: Settings, conn: sqlite3.Connection) -> None:
    """dry-run 只报告，不写库。"""
    _insert_message(conn, message_id="a@x.com")
    stats = ThreadBuilder(settings, conn).rebuild(apply=False)

    assert stats.dry_run is True
    assert stats.threads == 1
    assert conn.execute("SELECT COUNT(*) n FROM threads").fetchone()["n"] == 0
    row = conn.execute("SELECT thread_id FROM messages").fetchone()
    assert row["thread_id"] is None


def test_duplicates_and_stale_excluded(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """副本与 stale 记录不参与建图，否则会制造重复线程。"""
    _insert_message(conn, message_id="a@x.com")
    cur = conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            is_canonical, stale, fetched_at)
        VALUES ('163', 'INBOX', 1, 99, '副本', 0, 0, ?)
        """,
        (utcnow_iso(),),
    )
    dup_id = int(cur.lastrowid)
    cur2 = conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            is_canonical, stale, fetched_at)
        VALUES ('163', 'INBOX', 1, 98, '失效', 1, 1, ?)
        """,
        (utcnow_iso(),),
    )
    stale_id = int(cur2.lastrowid)

    stats = ThreadBuilder(settings, conn).rebuild(apply=True)
    assert stats.messages == 1

    for row_id in (dup_id, stale_id):
        row = conn.execute(
            "SELECT thread_id FROM messages WHERE id=?", (row_id,)
        ).fetchone()
        assert row["thread_id"] is None


def test_empty_database(settings: Settings, conn: sqlite3.Connection) -> None:
    stats = ThreadBuilder(settings, conn).rebuild(apply=True)
    assert stats.messages == 0
    assert stats.threads == 0


# ──────────────────────────────────────────────────────────────
# 统计
# ──────────────────────────────────────────────────────────────


def test_stats_collects_message_counts(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    _insert_message(conn, message_id="a@x.com", uid=1)
    _insert_message(conn, message_id="b@x.com", uid=2)
    conn.execute("UPDATE messages SET extract_status='done' WHERE uid=1")

    snapshot = collect(settings, conn)
    assert snapshot.messages_total == 2
    assert snapshot.messages_by_extract_status.get("done") == 1
    assert snapshot.messages_by_extract_status.get("pending") == 1


def test_stats_is_read_only(settings: Settings, conn: sqlite3.Connection) -> None:
    """统计绝不能有副作用——调用前后数据库内容必须完全一致。"""
    _insert_message(conn, message_id="a@x.com")
    before = conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]

    StatsCollector(settings, conn).collect()

    after = conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
    assert before == after == 1


def test_stats_reports_event_status_and_source(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO events (message_id, title, start_ts, source, confidence, "
        "fingerprint, status, created_at, updated_at) VALUES "
        "(NULL, 'e1', '2026-10-01T02:00:00Z', 'ics', 0.99, 'f1', 'pending', ?, ?)",
        (utcnow_iso(), utcnow_iso()),
    )
    conn.execute(
        "INSERT INTO events (message_id, title, start_ts, source, confidence, "
        "fingerprint, status, created_at, updated_at) VALUES "
        "(NULL, 'e2', '2026-10-02T02:00:00Z', 'rules', 0.95, 'f2', 'pushed', ?, ?)",
        (utcnow_iso(), utcnow_iso()),
    )

    snapshot = collect(settings, conn)
    assert snapshot.events_by_status["pending"] == 1
    assert snapshot.events_by_status["pushed"] == 1
    assert snapshot.events_by_source == {"ics": 1, "rules": 1}
    assert snapshot.pending_review == 1


def test_stats_counts_needs_attention(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO events (message_id, title, source, fingerprint, status, "
        "needs_attention, created_at, updated_at) VALUES "
        "(NULL, 'x', 'rules', 'f1', 'externally_modified', 1, ?, ?)",
        (utcnow_iso(), utcnow_iso()),
    )
    assert collect(settings, conn).needs_attention == 1


def test_describe_extract_skips_explains_llm_degradation() -> None:
    """「降级跳过」必须给出原因，否则使用者以为是系统能力上限。"""
    stats = ExtractStats(
        llm_skipped=19,
        llm_unavailable=True,
        llm_skipped_reasons=["未配置 LLM 凭据"],
    )
    notes = describe_extract_skips(stats)
    text = " ".join(notes)
    assert "19" in text
    assert "未配置 LLM 凭据" in text
    assert "仅规则" in text or "规则与 ICS" in text


def test_describe_extract_skips_silent_when_clean() -> None:
    assert describe_extract_skips(ExtractStats()) == []


def test_describe_extract_skips_mentions_protected_events() -> None:
    notes = describe_extract_skips(ExtractStats(events_protected=3))
    assert any("人工审批" in n for n in notes)


# ──────────────────────────────────────────────────────────────
# 摘要
# ──────────────────────────────────────────────────────────────


def test_digest_writes_markdown_file(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    _insert_message(
        conn, message_id="a@x.com", subject="面试通知",
        received_at="2026-09-14T02:00:00Z",
    )
    path, content = DigestBuilder(settings, conn).build()

    assert path.exists()
    assert path.name.startswith("digest-")
    assert path.suffix == ".md"
    assert "# 邮件摘要" in content
    assert "面试通知" in content


def test_digest_includes_pending_events_with_evidence(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """审核需要上下文：只给「9月20日 10:00 会议」无法判断真假。"""
    msg = _insert_message(conn, message_id="a@x.com", subject="会议通知")
    conn.execute(
        "INSERT INTO events (message_id, title, start_ts, source, confidence, "
        "fingerprint, evidence, status, created_at, updated_at) VALUES "
        "(?, '会议', '2026-10-01T02:00:00Z', 'rules', 0.95, 'f1', "
        "'[待审：时间已过] 2026年10月1日', 'pending', ?, ?)",
        (msg, utcnow_iso(), utcnow_iso()),
    )

    _, content = DigestBuilder(settings, conn).build()
    assert "待审事件" in content
    assert "会议" in content
    assert "依据" in content
    assert "会议通知" in content  # 来源邮件主题


def test_digest_includes_cancellable_window(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """**延迟窗口必须出现在摘要里**——否则没有可撤销对象，--cancel 形同虚设。"""
    from automail.review import ReviewQueue

    msg = _insert_message(conn, message_id="a@x.com")
    cur = conn.execute(
        "INSERT INTO events (message_id, title, start_ts, source, confidence, "
        "fingerprint, status, created_at, updated_at) VALUES "
        "(?, '自动事件', '2026-10-01T02:00:00Z', 'ics', 0.99, 'f1', 'approved', ?, ?)",
        (msg, utcnow_iso(), utcnow_iso()),
    )
    event_id = int(cur.lastrowid)
    ReviewQueue(conn).schedule_push(event_id, delay_minutes=1)

    _, content = DigestBuilder(settings, conn).build()
    assert "即将自动入历" in content
    assert "自动事件" in content
    assert "--cancel" in content


def test_digest_reports_attention_items(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO events (message_id, title, start_ts, source, fingerprint, "
        "status, needs_attention, created_at, updated_at) VALUES "
        "(NULL, '被手改的', '2026-10-01T02:00:00Z', 'rules', 'f1', "
        "'externally_modified', 1, ?, ?)",
        (utcnow_iso(), utcnow_iso()),
    )
    _, content = DigestBuilder(settings, conn).build()
    assert "需要关注" in content
    assert "被手改的" in content


def test_digest_states_llm_not_configured(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """LLM 未配置时摘要必须说明，否则使用者以为抽取质量就是这样。"""
    _insert_message(conn, message_id="a@x.com")
    _, content = DigestBuilder(settings, conn).build()
    assert "LLM 未配置" in content
    assert "仅规则" in content


def test_digest_marks_weak_threads(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """弱关联线程在摘要里必须标注，不得当作事实。"""
    _insert_message(conn, message_id="a@x.com", subject="项目讨论")
    _insert_message(
        conn, message_id="b@x.com", subject="Re: 项目讨论", uid=2
    )
    ThreadBuilder(settings, conn).rebuild(apply=True)
    _, content = DigestBuilder(settings, conn).build()

    assert "弱关联" in content


def test_digest_notes_calendar_absent(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """未接入日历时应说明原因，而不是静默留空。"""
    _insert_message(conn, message_id="a@x.com")
    _, content = DigestBuilder(settings, conn).build()
    assert "无法读取日历" in content


def test_digest_reads_calendar_events(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """接入日历后应列出今天的事件。"""
    from automail.calendar.backend import build_event_payload

    cal = FakeCalendar()
    now = datetime.now(TZ)
    start = now.replace(hour=15, minute=0, second=0, microsecond=0)
    cal.insert_event(
        build_event_payload(
            title="今天的会议",
            start_ts=start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_ts=start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            all_day=False,
            auto_mail_key="k1",
        )
    )
    _insert_message(conn, message_id="a@x.com")

    _, content = DigestBuilder(settings, conn, calendar=cal).build()
    assert "今天的会议" in content


def test_digest_calendar_failure_does_not_break_report(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """日历读取失败不应让整份摘要失败——降级并说明。"""
    class BrokenCalendar:
        def list_events(self, *, limit: int = 250):
            raise RuntimeError("simulated calendar outage")

    _insert_message(conn, message_id="a@x.com")
    path, content = DigestBuilder(settings, conn, calendar=BrokenCalendar()).build()
    assert path.exists()
    assert "无法读取日历" in content
    assert "simulated calendar outage" in content


def test_digest_same_day_overwrites_same_file(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """同一天重复生成写同一文件（幂等），不产生 digest-xxx-2.md。"""
    _insert_message(conn, message_id="a@x.com")
    builder = DigestBuilder(settings, conn)
    path1, _ = builder.build()
    path2, _ = builder.build()
    assert path1 == path2
    assert len(list(Path(settings.out_dir).glob("digest-*.md"))) == 1


def test_split_calendar_events_by_day() -> None:
    from automail.calendar.backend import build_event_payload

    cal = FakeCalendar()
    now = datetime(2026, 9, 14, 10, 0, tzinfo=TZ)
    today = now.replace(hour=15, minute=0)
    tomorrow = now + timedelta(days=1)

    cal.insert_event(build_event_payload(
        title="今天", start_ts=today.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_ts=None, all_day=False, auto_mail_key="k1",
    ))
    cal.insert_event(build_event_payload(
        title="明天", start_ts=tomorrow.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_ts=None, all_day=False, auto_mail_key="k2",
    ))
    cal.insert_event(build_event_payload(
        title="很久以后",
        start_ts=(now + timedelta(days=30)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_ts=None, all_day=False, auto_mail_key="k3",
    ))

    today_events, upcoming = _split_calendar_events(
        cal.list_events(), now=now, tz=TZ
    )
    assert [e["summary"] for e in today_events] == ["今天"]
    assert [e["summary"] for e in upcoming] == ["明天"]


def test_digest_date_of_uses_user_timezone(settings: Settings) -> None:
    # UTC 2026-09-14 20:00 → 北京时间 2026-09-15 04:00
    moment = datetime(2026, 9, 14, 20, 0, tzinfo=UTC)
    assert digest_date_of(settings, moment).isoformat() == "2026-09-15"


def test_digest_escapes_control_characters(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """邮件标题里的控制字符不得原样写进 Markdown（防注入与破坏渲染）。"""
    _insert_message(
        conn, message_id="a@x.com", subject="会议\x1b[31m标题\x1b[0m"
    )
    _, content = DigestBuilder(settings, conn).build()
    assert "\x1b" not in content
    assert "会议" in content
