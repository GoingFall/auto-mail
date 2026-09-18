"""SQLite 基础设施：连接、迁移、备份、锁与运行记录。

迁移策略
--------
用 ``PRAGMA user_version`` 作为单调递增的版本号，配合 ``migrations/NNN_*.sql``
顺序执行。前向迁移（forward-only）：不做回滚，迁移文件一旦发布不再修改。
升级前自动备份，避免 schema 变更把数据搞坏后无法恢复。

隐私约束
--------
``runs.error`` 只允许写入 message id 与哈希，**绝不写邮件正文**；本模块提供
的 :func:`sanitize_error` 会截断并把换行压平，但调用方仍有责任不传正文。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import RunRecord
from .sanitize import ERROR_MAX_CHARS, sanitize_error  # noqa: F401 - 对外再导出
from .settings import Settings

MIGRATION_PATTERN = re.compile(r"^(\d+)_.+\.sql$")


class DatabaseError(Exception):
    """数据库无法打开、迁移或写入。"""


# ──────────────────────────────────────────────────────────────
# 时间工具：全库统一 ISO8601 UTC，字典序即时间序
# ──────────────────────────────────────────────────────────────

def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    """转成 ``YYYY-MM-DDTHH:MM:SSZ`` 形式的 UTC 字符串。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_iso() -> str:
    return iso(utcnow())  # type: ignore[return-value]


def parse_iso(value: str | None) -> datetime | None:
    """解析本模块写出的 ISO8601 字符串；容忍末尾的 ``Z``。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# ──────────────────────────────────────────────────────────────
# 连接与迁移
# ──────────────────────────────────────────────────────────────


def connect(db_path: Path) -> sqlite3.Connection:
    """打开连接并设置本项目的固定 PRAGMA。

    * ``foreign_keys=ON`` —— 让 REFERENCES 真正生效。
    * ``journal_mode=WAL`` —— 读写并发，避免调度重叠时互锁。
    * ``busy_timeout=5000`` —— 两个进程抢锁时等待而非立刻报错。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def discover_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """列出可用迁移，按版本号升序。文件名不符规范则忽略。"""
    if not migrations_dir.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        match = MIGRATION_PATTERN.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return found


def latest_version(migrations_dir: Path) -> int:
    migrations = discover_migrations(migrations_dir)
    return migrations[-1][0] if migrations else 0


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[int]:
    """应用所有尚未执行的迁移，返回本次应用的版本号列表。

    以 ``user_version`` 为水位线，只执行编号更大的迁移。每个迁移执行完毕
    立即推进 ``user_version``，因此中途失败不会重复执行已成功的部分。
    """
    applied: list[int] = []
    version = current_version(conn)
    for number, path in discover_migrations(migrations_dir):
        if number <= version:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            # PRAGMA 不支持占位符，number 来自文件名正则且已转为 int，无注入风险
            conn.execute(f"PRAGMA user_version = {int(number)}")
        except sqlite3.Error as exc:
            raise DatabaseError(f"迁移 {path.name} 执行失败：{exc}") from exc
        version = number
        applied.append(number)
    return applied


def has_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return bool(row and row[0])


@contextmanager
def open_db(settings: Settings) -> Iterator[sqlite3.Connection]:
    """打开数据库、必要时先备份再迁移，最后关闭。

    这是其余模块获取连接的唯一入口，保证 PRAGMA 与 schema 版本一致。
    """
    try:
        settings.ensure_dirs()
    except (OSError, ValueError) as exc:
        raise DatabaseError(f"无法创建数据目录 {settings.data_dir}：{exc}") from exc

    db_path = settings.db_path
    pre_existing = db_path.exists()
    try:
        conn = connect(db_path)
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise DatabaseError(f"无法打开数据库 {db_path}：{exc}") from exc

    try:
        pending = [
            number
            for number, _ in discover_migrations(settings.migrations_dir)
            if number > current_version(conn)
        ]
        # 升级前备份：只在已有数据时做，避免首次运行产生无意义的空备份
        if pending and pre_existing and has_tables(conn):
            backup_db(settings, conn)
        try:
            apply_migrations(conn, settings.migrations_dir)
        except DatabaseError:
            raise
        except sqlite3.Error as exc:
            raise DatabaseError(f"迁移失败：{exc}") from exc
        yield conn
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────
# 备份与清理
# ──────────────────────────────────────────────────────────────

def backup_db(settings: Settings, conn: sqlite3.Connection) -> Path:
    """在线备份数据库（不阻塞读写），返回备份文件路径。

    由于 ``body_excerpt`` 只存脱敏片段而非完整正文，备份不会复制完整邮件内容。
    """
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = settings.backup_dir / f"automail-{stamp}.db"
    try:
        dest = sqlite3.connect(str(target))
        try:
            conn.backup(dest)
        finally:
            dest.close()
    except sqlite3.Error as exc:
        raise DatabaseError(f"备份失败：{exc}") from exc
    prune_backups(settings)
    return target


def prune_backups(settings: Settings) -> list[Path]:
    """按保留数量与最大年龄清理备份，返回被删除的文件。"""
    backups = sorted(
        settings.backup_dir.glob("automail-*.db"),
        key=lambda p: p.name,
        reverse=True,
    )
    cutoff = utcnow() - timedelta(days=settings.db_backup_max_age_days)
    removed: list[Path] = []
    for index, path in enumerate(backups):
        too_many = index >= settings.db_backup_keep
        stat = path.stat()
        too_old = datetime.fromtimestamp(stat.st_mtime, tz=UTC) < cutoff
        if too_many or too_old:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


# ──────────────────────────────────────────────────────────────
# 仓库：P0 只涉及 runs / locks / app_meta
# ──────────────────────────────────────────────────────────────

class RunRepository:
    """``runs`` 表的读写。每次 CLI 运行都应留一条。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self, run_id: str, command: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs (run_id, command, started_at) VALUES (?, ?, ?)",
            (run_id, command, utcnow_iso()),
        )
        return int(cur.lastrowid)

    def finish(
        self,
        row_id: int,
        *,
        ok: bool,
        exit_code: int,
        stats: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        import json

        self._conn.execute(
            "UPDATE runs SET ended_at = ?, ok = ?, exit_code = ?, "
            "stats_json = ?, error = ? WHERE id = ?",
            (
                utcnow_iso(),
                1 if ok else 0,
                exit_code,
                json.dumps(stats or {}, ensure_ascii=False),
                error,
                row_id,
            ),
        )

    def record_cycle(
        self,
        run_id: str,
        command: str,
        *,
        exit_code: int,
        error: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> int:
        """一步写下一条完整的运行记录（开始与结束同刻）。

        适用于「运行已经结束、现在补记」的场景，例如占位命令与失败路径。
        ``runs.error`` 必须已经过 :func:`sanitize_error`，绝不可传邮件正文。
        """
        row_id = self.start(run_id, command)
        self.finish(
            row_id,
            ok=exit_code == 0,
            exit_code=exit_code,
            stats=stats,
            error=sanitize_error(error) if error else None,
        )
        return row_id

    def recent(self, limit: int = 10) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        import json

        records: list[RunRecord] = []
        for row in rows:
            raw_stats = row["stats_json"]
            records.append(
                RunRecord(
                    run_id=row["run_id"],
                    command=row["command"],
                    started_at=row["started_at"],
                    ended_at=row["ended_at"],
                    stats=json.loads(raw_stats) if raw_stats else {},
                    ok=None if row["ok"] is None else bool(row["ok"]),
                    error=row["error"],
                    exit_code=row["exit_code"],
                )
            )
        return records


class LockRepository:
    """``locks`` 表的 TTL 抢占式单实例锁。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def acquire(self, name: str, owner_run_id: str, ttl_seconds: int) -> bool:
        """尝试获取锁。

        同一 owner 重入或旧锁已过期时接管；否则返回 False。
        依赖 ``ON CONFLICT ... WHERE`` 的条件更新：条件不满足时语句为 no-op，
        ``rowcount`` 为 0，正好等价于「未获得锁」。
        """
        now = utcnow_iso()
        expires = iso(utcnow() + timedelta(seconds=ttl_seconds))
        cur = self._conn.execute(
            """
            INSERT INTO locks (name, owner_run_id, acquired_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                owner_run_id = excluded.owner_run_id,
                acquired_at  = excluded.acquired_at,
                expires_at   = excluded.expires_at
            WHERE locks.expires_at <= ? OR locks.owner_run_id = ?
            """,
            (name, owner_run_id, now, expires, now, owner_run_id),
        )
        return cur.rowcount > 0

    def release(self, name: str, owner_run_id: str) -> None:
        self._conn.execute(
            "DELETE FROM locks WHERE name = ? AND owner_run_id = ?",
            (name, owner_run_id),
        )

    def peek(self, name: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM locks WHERE name = ?", (name,)
        ).fetchone()

    def purge_expired(self) -> int:
        cur = self._conn.execute("DELETE FROM locks WHERE expires_at <= ?", (utcnow_iso(),))
        return cur.rowcount


class MetaRepository:
    """``app_meta`` 键值表：少量全局状态，例如 OAuth 的 needs_reauth。"""

    NEEDS_REAUTH = "google.needs_reauth"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM app_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO app_meta (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (key, value, utcnow_iso()),
        )

    def delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM app_meta WHERE key = ?", (key,))

    def get_bool(self, key: str) -> bool:
        return (self.get(key) or "").strip().lower() in {"1", "true", "yes"}

    def set_bool(self, key: str, value: bool) -> None:
        self.set(key, "1" if value else "0")
