"""共享测试夹具。

原则：**单测全程不联网、不碰真实凭据**。所有外部系统都用夹具替换。
每个测试使用独立的临时目录，避免污染工作区的 data/ logs/。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from automail import db as db_module
from automail.settings import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "src" / "automail" / "migrations"


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """切到临时目录，并把随包迁移目录暴露给 Settings。

    ``Settings.migrations_dir`` 指向源码位置，测试中无需改动；这里只保证
    相对路径配置（data/out/logs）落在 tmp_path 下。
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def settings(workdir: Path) -> Settings:
    """一套完全离线、无凭据的默认配置。"""
    return Settings(
        _env_file=None,
        data_dir=workdir / "data",
        out_dir=workdir / "out",
        log_dir=workdir / "logs",
    )


@pytest.fixture
def conn(settings: Settings):
    """已迁移的空数据库连接。"""
    settings.ensure_dirs()
    connection = db_module.connect(settings.db_path)
    db_module.apply_migrations(connection, settings.migrations_dir)
    yield connection
    connection.close()


@pytest.fixture
def fixture_dir() -> Path:
    """邮件样本目录（*.eml 被 gitignore，测试内联生成更稳妥）。"""
    return Path(__file__).resolve().parent / "fixtures"


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}
