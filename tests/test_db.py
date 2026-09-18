"""数据库层测试：迁移、幂等约束、锁、备份、错误清洗。"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from automail import db as db_module
from automail.db import (
    DatabaseError,
    LockRepository,
    MetaRepository,
    RunRepository,
    iso,
    parse_iso,
    sanitize_error,
    utcnow,
)
from automail.settings import Settings
from tests.conftest import table_names

EXPECTED_TABLES = {
    "threads",
    "messages",
    "events",
    "scheduled_pushes",
    "processed_mail",
    "sync_state",
    "senders",
    "runs",
    "locks",
    "unsubscribe_log",
    "todos",
    "todo_reminders",
    "app_meta",
}


# ──────────────────────────────────────────────────────────────
# 迁移
# ──────────────────────────────────────────────────────────────

def test_migration_creates_all_tables(conn: sqlite3.Connection) -> None:
    assert table_names(conn) == EXPECTED_TABLES


def test_user_version_advances_to_latest(settings: Settings) -> None:
    with db_module.open_db(settings) as connection:
        assert db_module.current_version(connection) == db_module.latest_version(
            settings.migrations_dir
        )


def test_migration_is_idempotent(settings: Settings) -> None:
    """重复打开不应重复执行迁移，也不应报错。"""
    with db_module.open_db(settings) as connection:
        assert db_module.apply_migrations(connection, settings.migrations_dir) == []
    with db_module.open_db(settings) as connection:
        assert db_module.apply_migrations(connection, settings.migrations_dir) == []


def test_backup_created_when_upgrading_existing_db(
    settings: Settings, tmp_path: Path
) -> None:
    """已有数据且出现**更新的**迁移时，升级前必须先备份。

    贴近真实：先建到「当前最新版本」，再追加一个更高版本的迁移触发升级。
    不能硬编码版本号——迁移会随项目演进增加（001 → 002 → …），
    硬编码会让这条用例在每次加迁移时误报。
    """
    import shutil

    current = db_module.latest_version(settings.migrations_dir)
    next_version = current + 1

    older_dir = tmp_path / "migrations_older"
    newer_dir = tmp_path / "migrations_newer"
    shutil.copytree(settings.migrations_dir, older_dir)
    shutil.copytree(settings.migrations_dir, newer_dir)
    (newer_dir / f"{next_version:03d}_add_note.sql").write_text(
        "ALTER TABLE app_meta ADD COLUMN note TEXT;\n", encoding="utf-8"
    )

    settings.migrations_dir = older_dir
    with db_module.open_db(settings) as connection:
        RunRepository(connection).start("r-1", "doctor")
        assert db_module.current_version(connection) == current

    settings.migrations_dir = newer_dir
    with db_module.open_db(settings) as connection:
        assert db_module.current_version(connection) == next_version

    backups = list(settings.backup_dir.glob("automail-*.db"))
    assert len(backups) == 1


def test_no_backup_on_first_initialization(settings: Settings) -> None:
    """首次建库不应产生空备份。"""
    with db_module.open_db(settings):
        pass
    assert list(settings.backup_dir.glob("automail-*.db")) == []


def test_backup_prune_keeps_configured_count(settings: Settings) -> None:
    settings.db_backup_keep = 2
    settings.db_backup_max_age_days = 3650
    settings.backup_dir.mkdir(parents=True, exist_ok=True)

    import sqlite3 as _sqlite3

    # 直接摆 4 个备份文件，名字决定新旧顺序
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z", "20260104T000000Z"):
        path = settings.backup_dir / f"automail-{stamp}.db"
        _sqlite3.connect(str(path)).close()

    removed = db_module.prune_backups(settings)
    remaining = sorted(p.name for p in settings.backup_dir.glob("automail-*.db"))

    assert len(removed) == 2
    assert len(remaining) == 2
    # 保留最新的两个
    assert remaining == ["automail-20260103T000000Z.db", "automail-20260104T000000Z.db"]


# ──────────────────────────────────────────────────────────────
# 时间工具
# ──────────────────────────────────────────────────────────────

def test_iso_roundtrip_is_utc() -> None:
    now = utcnow()
    text = iso(now)
    assert text is not None and text.endswith("Z")
    parsed = parse_iso(text)
    assert parsed is not None
    assert abs((parsed - now).total_seconds()) < 1


def test_parse_iso_tolerates_garbage() -> None:
    assert parse_iso(None) is None
    assert parse_iso("") is None
    assert parse_iso("not-a-date") is None


def test_parse_iso_treats_naive_as_utc() -> None:
    parsed = parse_iso("2026-09-14T10:00:00")
    assert parsed is not None
    assert parsed.tzinfo is not None


# ──────────────────────────────────────────────────────────────
# 错误清洗（防止正文被写进 runs.error）
# ──────────────────────────────────────────────────────────────

def test_sanitize_error_flattens_and_truncates() -> None:
    noisy = "line1\nline2\r\n" + "x" * 2000
    cleaned = sanitize_error(Exception(noisy))
    assert "\n" not in cleaned and "\r" not in cleaned
    assert len(cleaned) <= db_module.ERROR_MAX_CHARS + 1


def test_sanitize_error_strips_control_chars() -> None:
    cleaned = sanitize_error(Exception("标题\x1b[31m红色\x1b[0m结束"))
    assert "\x1b" not in cleaned
    assert "标题" in cleaned


# ──────────────────────────────────────────────────────────────
# 幂等约束
# ──────────────────────────────────────────────────────────────

def _insert_message(conn: sqlite3.Connection, **overrides: object) -> int:
    values = {
        "account": "163",
        "folder": "INBOX",
        "uid_validity": 1,
        "uid": 10,
        "subject": "s",
        "fetched_at": db_module.utcnow_iso(),
    }
    values.update(overrides)
    cur = conn.execute(
        "INSERT INTO messages (account, folder, uid_validity, uid, subject, fetched_at) "
        "VALUES (:account, :folder, :uid_validity, :uid, :subject, :fetched_at)",
        values,
    )
    return int(cur.lastrowid)


def test_messages_unique_on_uid_tuple(conn: sqlite3.Connection) -> None:
    _insert_message(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_message(conn)


def test_duplicate_message_id_across_folders_is_allowed(conn: sqlite3.Connection) -> None:
    """同一封邮件出现在两个文件夹（相同 Message-ID）必须能同时入库。

    这是规格明确要求修复的场景：normalized_message_id 只建非唯一索引。
    """
    first = _insert_message(conn, folder="INBOX", uid=1, normalized_message_id="<a@b>")
    second = _insert_message(conn, folder="订阅邮件", uid=2, normalized_message_id="<a@b>")
    assert first != second

    # 第二份标记为非规范副本
    conn.execute(
        "UPDATE messages SET is_canonical = 0, duplicate_of = ? WHERE id = ?",
        (first, second),
    )
    row = conn.execute("SELECT is_canonical, duplicate_of FROM messages WHERE id = ?", (second,)).fetchone()
    assert row["is_canonical"] == 0
    assert row["duplicate_of"] == first


def test_events_unique_on_message_and_fingerprint(conn: sqlite3.Connection) -> None:
    message_id = _insert_message(conn)
    now = db_module.utcnow_iso()
    params = (message_id, "Event", "rules", "fp-1", "pending", now, now)
    conn.execute(
        "INSERT INTO events (message_id, title, source, fingerprint, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        params,
    )
    # 同邮件同指纹重复抽取必须被拦下（幂等）
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (message_id, title, source, fingerprint, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            params,
        )


def test_event_status_check_constraint(conn: sqlite3.Connection) -> None:
    message_id = _insert_message(conn)
    now = db_module.utcnow_iso()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (message_id, source, fingerprint, status, created_at, updated_at) "
            "VALUES (?, 'rules', 'fp', 'bogus-status', ?, ?)",
            (message_id, now, now),
        )


def test_extract_status_check_constraint(conn: sqlite3.Connection) -> None:
    message_id = _insert_message(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE messages SET extract_status = 'nonsense' WHERE id = ?", (message_id,)
        )


def test_source_check_constraint(conn: sqlite3.Connection) -> None:
    message_id = _insert_message(conn)
    now = db_module.utcnow_iso()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO events (message_id, source, fingerprint, created_at, updated_at) "
            "VALUES (?, 'magic', 'fp', ?, ?)",
            (message_id, now, now),
        )


# ──────────────────────────────────────────────────────────────
# 锁
# ──────────────────────────────────────────────────────────────

def test_lock_exclusive(conn: sqlite3.Connection) -> None:
    locks = LockRepository(conn)
    assert locks.acquire("run", "owner-a", ttl_seconds=300) is True
    assert locks.acquire("run", "owner-b", ttl_seconds=300) is False


def test_lock_owner_can_reenter(conn: sqlite3.Connection) -> None:
    locks = LockRepository(conn)
    assert locks.acquire("run", "owner-a", ttl_seconds=300) is True
    assert locks.acquire("run", "owner-a", ttl_seconds=300) is True


def test_lock_expired_is_taken_over(conn: sqlite3.Connection) -> None:
    locks = LockRepository(conn)
    assert locks.acquire("run", "owner-a", ttl_seconds=300) is True
    # 手动把过期时间推到过去，模拟上次运行崩溃
    conn.execute(
        "UPDATE locks SET expires_at = ? WHERE name = 'run'",
        (iso(utcnow() - timedelta(seconds=10)),),
    )
    assert locks.acquire("run", "owner-b", ttl_seconds=300) is True
    assert locks.peek("run")["owner_run_id"] == "owner-b"


def test_lock_release_only_by_owner(conn: sqlite3.Connection) -> None:
    locks = LockRepository(conn)
    locks.acquire("run", "owner-a", ttl_seconds=300)
    locks.release("run", "owner-b")
    assert locks.peek("run") is not None
    locks.release("run", "owner-a")
    assert locks.peek("run") is None


def test_lock_purge_expired(conn: sqlite3.Connection) -> None:
    locks = LockRepository(conn)
    locks.acquire("run", "owner-a", ttl_seconds=300)
    conn.execute(
        "UPDATE locks SET expires_at = ? WHERE name = 'run'",
        (iso(utcnow() - timedelta(seconds=1)),),
    )
    assert locks.purge_expired() == 1


# ──────────────────────────────────────────────────────────────
# runs / app_meta
# ──────────────────────────────────────────────────────────────

def test_run_repository_lifecycle(conn: sqlite3.Connection) -> None:
    runs = RunRepository(conn)
    row_id = runs.start("run-1", "sync")
    runs.finish(row_id, ok=True, exit_code=0, stats={"synced": 3})

    record = runs.recent(limit=1)[0]
    assert record.run_id == "run-1"
    assert record.command == "sync"
    assert record.ok is True
    assert record.stats == {"synced": 3}
    assert record.ended_at is not None


def test_run_repository_records_failure(conn: sqlite3.Connection) -> None:
    runs = RunRepository(conn)
    row_id = runs.start("run-2", "extract")
    runs.finish(row_id, ok=False, exit_code=2, error="boom")
    record = runs.recent(limit=1)[0]
    assert record.ok is False
    assert record.error == "boom"


def test_meta_repository_roundtrip(conn: sqlite3.Connection) -> None:
    meta = MetaRepository(conn)
    assert meta.get("missing", "fallback") == "fallback"
    assert meta.get_bool(MetaRepository.NEEDS_REAUTH) is False

    meta.set(MetaRepository.NEEDS_REAUTH, "1")
    assert meta.get_bool(MetaRepository.NEEDS_REAUTH) is True

    meta.set_bool(MetaRepository.NEEDS_REAUTH, False)
    assert meta.get_bool(MetaRepository.NEEDS_REAUTH) is False

    meta.delete(MetaRepository.NEEDS_REAUTH)
    assert meta.get(MetaRepository.NEEDS_REAUTH) is None


# ──────────────────────────────────────────────────────────────
# 打开失败路径
# ──────────────────────────────────────────────────────────────

def test_open_db_raises_on_unwritable_path(settings: Settings, tmp_path: Path) -> None:
    """数据目录无法创建时应抛出 DatabaseError，而不是裸 OSError/ValueError。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    # 把「文件」当目录用 → mkdir 必然失败
    settings.data_dir = blocker / "data"
    with pytest.raises(DatabaseError):
        with db_module.open_db(settings):
            pass


def test_pragmas_are_applied(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
