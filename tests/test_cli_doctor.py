"""doctor 与 CLI 退出码测试。

退出码是计划任务判断成败的唯一依据，因此这里逐个固定行为。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from automail import cli as cli_module
from automail.doctor import compute_exit_code, run_checks
from automail.exits import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from automail.models import ReadinessItem, ReadinessStatus
from automail.settings import Settings

# ──────────────────────────────────────────────────────────────
# 退出码计算（纯逻辑）
# ──────────────────────────────────────────────────────────────

def test_exit_code_all_ok() -> None:
    items = [ReadinessItem("a", ReadinessStatus.OK, "")]
    assert compute_exit_code(items) == EXIT_OK


def test_exit_code_missing_is_partial_not_fatal() -> None:
    items = [
        ReadinessItem("a", ReadinessStatus.OK, ""),
        ReadinessItem("b", ReadinessStatus.MISSING, "缺凭据"),
    ]
    assert compute_exit_code(items) == EXIT_PARTIAL


def test_exit_code_skipped_does_not_fail() -> None:
    items = [ReadinessItem("a", ReadinessStatus.SKIPPED, "未执行")]
    assert compute_exit_code(items) == EXIT_OK


def test_exit_code_nonfatal_error_is_still_partial() -> None:
    items = [ReadinessItem("a", ReadinessStatus.ERROR, "小问题", fatal=False)]
    assert compute_exit_code(items) == EXIT_PARTIAL


def test_exit_code_fatal_error_wins() -> None:
    items = [
        ReadinessItem("a", ReadinessStatus.MISSING, "缺凭据"),
        ReadinessItem("b", ReadinessStatus.ERROR, "致命", fatal=True),
    ]
    assert compute_exit_code(items) == EXIT_FATAL


# ──────────────────────────────────────────────────────────────
# 离线检查项
# ──────────────────────────────────────────────────────────────

def test_offline_checks_run_without_credentials(settings: Settings) -> None:
    items = run_checks(settings, live=False)
    names = {item.name for item in items}
    assert "依赖 pydantic" in names
    assert "数据库" in names
    assert "时区" in names
    assert "163 邮箱凭据" in names

    # 无凭据 → 三项 MISSING，但整体只是「部分缺失」
    credential_names = {"163 邮箱凭据", "Google 凭据", "LLM 凭据"}
    credential_items = [item for item in items if item.name in credential_names]
    assert len(credential_items) == 3
    assert all(item.status is ReadinessStatus.MISSING for item in credential_items)
    assert compute_exit_code(items) == EXIT_PARTIAL


def test_live_checks_are_skipped_without_credentials(settings: Settings) -> None:
    items = run_checks(settings, live=True)
    live_names = {"IMAP 连通性", "Google 日历连通性", "LLM 连通性"}
    live_items = [item for item in items if item.name in live_names]
    assert len(live_items) == 3
    # 缺凭据时不应尝试联网，如实报 SKIPPED 而不是假装通过或假装失败
    assert all(item.status is ReadinessStatus.SKIPPED for item in live_items)


def test_directory_check_is_fatal_when_unwritable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from automail import doctor as doctor_module

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(doctor_module.tempfile, "NamedTemporaryFile", boom)
    items = doctor_module.check_directories(settings)
    assert items.status is ReadinessStatus.ERROR
    assert items.fatal is True


def test_database_check_reports_schema_version(settings: Settings) -> None:
    from automail import db as db_module
    from automail.doctor import check_database

    schema_item, runs_item = check_database(settings)
    assert schema_item.status is ReadinessStatus.OK
    # 不硬编码版本号：迁移会随项目演进增加
    expected = db_module.latest_version(settings.migrations_dir)
    assert f"schema v{expected}" in schema_item.detail
    assert runs_item.status is ReadinessStatus.OK


# ──────────────────────────────────────────────────────────────
# CLI 退出码（端到端，通过 main 的返回值）
# ──────────────────────────────────────────────────────────────

def _invoke(args: list[str]) -> int:
    """以 ``main`` 的返回值为准，而不是异常——这正是被修掉的 bug。"""
    import sys

    argv_backup = sys.argv
    sys.argv = ["automail", *args]
    try:
        return cli_module.main()
    finally:
        sys.argv = argv_backup


@pytest.fixture(autouse=True)
def _isolate_cli(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """让 CLI 在临时目录运行，避免写真实 data/。"""
    monkeypatch.chdir(workdir)


def test_cli_doctor_returns_partial_without_credentials() -> None:
    assert _invoke(["doctor"]) == EXIT_PARTIAL


def test_cli_doctor_json_is_machine_readable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _invoke(["doctor", "--json"])
    assert code == EXIT_PARTIAL
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == EXIT_PARTIAL
    assert any(item["status"] == "missing" for item in payload["items"])


def test_cli_runs_returns_ok_when_empty() -> None:
    assert _invoke(["runs"]) == EXIT_OK


def test_cli_version_returns_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert _invoke(["--version"]) == EXIT_OK
    assert "auto-mail" in capsys.readouterr().out


def test_cli_unknown_command_returns_fatal() -> None:
    assert _invoke(["nosuchcommand"]) == EXIT_FATAL


def test_cli_unknown_option_returns_fatal() -> None:
    assert _invoke(["doctor", "--nope"]) == EXIT_FATAL


def test_no_command_is_still_a_placeholder() -> None:
    """所有命令都已实现——不存在返回「未实现」的命令了。

    这条用例反过来守护：若将来新增了占位命令，它必须在这份清单之外被
    单独覆盖，而不是悄悄混进已实现命令里。
    """
    import typer

    from automail import cli as cli_module

    command = typer.main.get_command(cli_module.app)
    implemented = set(getattr(command, "commands", {}) or {})
    assert "run" in implemented
    assert "auth" in implemented
    assert "backup" in implemented


def test_live_checks_include_imap() -> None:
    """--live 必须包含 IMAP 项（P1 已实现真实连接检查）。"""
    from automail.doctor import live_checks

    items = live_checks(
        Settings(_env_file=None),
        imap_ready=False,
        google_ready=False,
        llm_ready=False,
    )
    names = {item.name for item in items}
    assert "IMAP 连通性" in names


def test_cli_invalid_config_returns_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER_TIMEZONE", "Nowhere/Invalid")
    assert _invoke(["doctor"]) == EXIT_FATAL


# ──────────────────────────────────────────────────────────────
# runs 记录：每个命令都必须留痕（含失败与占位命令）
# ──────────────────────────────────────────────────────────────

def _run_records(workdir: Path) -> list:
    from automail.db import RunRepository, open_db
    from automail.settings import Settings

    settings = Settings(
        _env_file=None,
        data_dir=workdir / "data",
        out_dir=workdir / "out",
        log_dir=workdir / "logs",
    )
    with open_db(settings) as connection:
        return RunRepository(connection).recent(limit=50)


def test_doctor_writes_run_record(workdir: Path) -> None:
    _invoke(["doctor"])
    records = _run_records(workdir)
    assert len(records) == 1
    assert records[0].command == "doctor"
    assert records[0].ok is False  # 退出码 1 视为未成功
    assert records[0].exit_code == EXIT_PARTIAL
    assert records[0].stats["exit_code"] == EXIT_PARTIAL


def test_auth_status_writes_run_record(workdir: Path) -> None:
    """auth --status 也要留痕，否则审计上会出现盲区。"""
    _invoke(["auth", "--status"])
    records = _run_records(workdir)
    assert len(records) == 1
    assert records[0].command == "auth"
    # 无 credentials.json → 未就绪 → 部分完成
    assert records[0].exit_code == EXIT_PARTIAL


def test_auth_status_exit_code_reflects_readiness(workdir: Path) -> None:
    """缺凭据时 auth --status 返回 1（部分缺失），不是致命错误。

    使用者可能只是想确认「我到底还差哪一步」，那不是错误。
    """
    assert _invoke(["auth", "--status"]) == EXIT_PARTIAL


def test_auth_revoke_without_token_is_safe(workdir: Path) -> None:
    """没有 token 时 --revoke 应是安全的 no-op。"""
    assert _invoke(["auth", "--revoke"]) == EXIT_OK


def test_extract_runs_without_messages(workdir: Path) -> None:
    """extract 已实现：无邮件时应正常返回 0，而不是报「未实现」。

    这是防止「命令实现了但忘了从占位列表移除」这类不一致的检查。
    """
    code = _invoke(["extract"])
    assert code == EXIT_OK, "空库上 extract 应成功（0 个待处理邮件）"
    records = _run_records(workdir)
    assert records[0].command == "extract"
    assert "not_implemented" not in (records[0].stats or {})


def test_sync_without_credentials_writes_run_record(workdir: Path) -> None:
    """sync 缺凭据时提前退出，但**仍须留痕**——否则审计有盲区。"""
    code = _invoke(["sync"])
    assert code == EXIT_PARTIAL
    records = _run_records(workdir)
    assert len(records) == 1
    assert records[0].command == "sync"
    assert records[0].stats.get("error") == "missing_credentials"


def test_runs_command_lists_records(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _invoke(["doctor"])
    assert _invoke(["runs"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "doctor" in output


def test_failed_command_records_error_without_body(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """异常必须被记录，且记录里不能出现正文——这里用带控制字符的消息验证清洗。"""
    from automail import cli as cli_module

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("邮件正文片段\x1b[31mSECRET\x1b[0m不应落库")

    monkeypatch.setattr(cli_module, "run_checks", explode)
    assert _invoke(["doctor"]) == EXIT_FATAL

    records = _run_records(workdir)
    assert len(records) == 1
    assert records[0].ok is False
    assert records[0].error is not None
    assert "\x1b" not in records[0].error
