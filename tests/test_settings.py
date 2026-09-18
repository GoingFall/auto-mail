"""配置层测试：默认值、校验、派生值、代理。"""

from __future__ import annotations

from pathlib import Path

import pytest

from automail.settings import Settings, SettingsError, load_settings


def test_defaults_are_offline_safe() -> None:
    """默认配置必须能在没有任何凭据时构造成功（doctor 才能报告 MISSING）。"""
    settings = Settings(_env_file=None)
    assert settings.imap_auth_code_value == ""
    assert settings.llm_api_key_value == ""
    assert settings.google_calendar_id == "primary"


def test_imap_folder_list_dedupes_and_preserves_order() -> None:
    settings = Settings(_env_file=None, imap_folders="INBOX, 订阅邮件 ,INBOX,")
    assert settings.imap_folder_list == ["INBOX", "订阅邮件"]


def test_llm_payload_fields_normalized() -> None:
    settings = Settings(_env_file=None, llm_payload_fields="Excerpt, SUBJECT ,excerpt")
    assert settings.llm_payload_field_list == ["excerpt", "subject"]


def test_derived_paths() -> None:
    """派生路径必须跟随 ``data_dir``。

    注意传入的是**相对**路径：它会被锚定为基目录下的绝对路径（见
    ``_resolve_relative_config_paths``）。这里断言的是"跟随"关系，
    而不是"保持相对"——后者正是导致 frozen exe 从别的目录启动时报
    ``拒绝访问: 'data'`` 的原因。
    """
    settings = Settings(_env_file=None, data_dir=Path("d"))
    assert settings.db_path == settings.data_dir / "automail.db"
    assert settings.backup_dir == settings.data_dir / "backups"
    assert settings.data_dir.is_absolute(), "相对路径应被锚定为绝对路径"


def test_secrets_are_masked_in_repr() -> None:
    settings = Settings(_env_file=None, llm_api_key="sk-super-secret")
    assert "sk-super-secret" not in repr(settings)
    assert settings.llm_api_key_value == "sk-super-secret"


# ── 校验失败必须抛 SettingsError（调用方据此返回退出码 2）────────

def test_invalid_timezone_is_fatal() -> None:
    with pytest.raises(SettingsError):
        load_settings(_env_file=None, user_timezone="Mars/Olympus")


def test_invalid_reminder_time_is_fatal() -> None:
    with pytest.raises(SettingsError):
        load_settings(_env_file=None, default_reminder_time="9am")


def test_invalid_log_level_is_fatal() -> None:
    with pytest.raises(SettingsError):
        load_settings(_env_file=None, log_level="CHATTY")


def test_confidence_threshold_range_enforced() -> None:
    with pytest.raises(SettingsError):
        load_settings(_env_file=None, confidence_auto_push_threshold=1.5)


def test_negative_delay_rejected() -> None:
    with pytest.raises(SettingsError):
        load_settings(_env_file=None, auto_push_delay_minutes=-1)


def test_log_level_is_uppercased() -> None:
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"


def test_unknown_not_found_policy_rejected() -> None:
    with pytest.raises(SettingsError):
        load_settings(_env_file=None, not_found_policy="explode")


# ── 侧效应 ────────────────────────────────────────────────────

def test_ensure_dirs_creates_and_reports(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "logs",
    )
    created = settings.ensure_dirs()
    assert len(created) == 4  # data, out, logs, backups
    for directory in (settings.data_dir, settings.out_dir, settings.log_dir, settings.backup_dir):
        assert directory.is_dir()

    # 第二次调用不应重复报告
    assert settings.ensure_dirs() == []


def test_apply_proxy_env_does_not_clobber_existing() -> None:
    env: dict[str, str] = {"HTTPS_PROXY": "http://existing:8080"}
    settings = Settings(_env_file=None, https_proxy="http://from-config:8080")
    settings.apply_proxy_env(env)
    # 已有环境变量优先，配置不覆盖它
    assert env["HTTPS_PROXY"] == "http://existing:8080"


def test_apply_proxy_env_sets_when_absent() -> None:
    env: dict[str, str] = {}
    settings = Settings(_env_file=None, https_proxy="http://proxy:8080")
    settings.apply_proxy_env(env)
    assert env["HTTPS_PROXY"] == "http://proxy:8080"
    assert env["https_proxy"] == "http://proxy:8080"


def test_apply_proxy_env_sets_no_proxy() -> None:
    env: dict[str, str] = {}
    settings = Settings(_env_file=None, https_proxy="http://p:1", no_proxy="imap.163.com")
    settings.apply_proxy_env(env)
    assert env["NO_PROXY"] == "imap.163.com"


def test_describe_proxies_default_is_direct() -> None:
    """仅配置 NO_PROXY 不算「已配置代理」——它只在有代理时才有意义。"""
    settings = Settings(_env_file=None, https_proxy="", http_proxy="", no_proxy="")
    assert "未配置" in settings.describe_proxies()


def test_describe_proxies_reports_explicit_proxy() -> None:
    settings = Settings(_env_file=None, https_proxy="http://p:8080", no_proxy="x.local")
    described = settings.describe_proxies()
    assert "http://p:8080" in described
    assert "x.local" in described


# ──────────────────────────────────────────────────────────────
# .env.example 与 Settings 的一致性
# ──────────────────────────────────────────────────────────────


def test_env_example_covers_all_settings() -> None:
    """``.env.example`` 必须覆盖全部配置项。

    漂移是真实发生过的：新加的项忘了写进模板，使用者就无从知道它存在
    （``MARK_READ_POLICY`` 这类默认关闭的开关尤其如此——不写就永远不会被开启）。

    注释掉的赋值（``# HTTPS_PROXY=``）也算已文档化：可选的高级项用这种写法
    避免误填，是刻意的方式。
    """
    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")

    documented = set()
    for line in example.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if not stripped or "=" not in stripped:
            continue
        documented.add(stripped.split("=", 1)[0].strip().upper())

    #: 不是环境变量、或刻意不写进模板的项。
    internal = {
        # 项目自身标识，不是使用者可调项
        "ACCOUNT",
        # 派生路径与内部维护项：改了会破坏数据位置或锁语义，不鼓励手工配置
        "MIGRATIONS_DIR",
        "EXTRACT_ZOMBIE_MINUTES",
        "RUN_LOCK_TTL_SECONDS",
        # 仅测试/演练用
        "CALENDAR_BACKEND",
    }
    missing = sorted(
        name.upper()
        for name in Settings.model_fields
        if name.upper() not in documented and name.upper() not in internal
    )
    assert not missing, (
        f".env.example 缺少这些配置项：{missing}。"
        "新增设置时请同时补文档——尤其是默认关闭的开关。"
    )


def test_mark_read_defaults_are_off() -> None:
    """**已读回写是唯一会改变邮箱状态的操作，默认必须关闭。**

    升级既有安装时不得静默改变使用者邮箱的状态。
    """
    settings = Settings(_env_file=None)
    assert settings.mark_read_policy == "off"
    assert settings.mark_read_enabled is False


def test_mark_read_policy_rejects_unknown_value() -> None:
    """非法策略名必须被拒绝，而不是静默退化成某个默认行为。

    静默退化在这里尤其危险：写错 ``resolved`` 可能被当成 ``off``（功能不生效）
    或被当成 ``processed``（标记范围超出预期，而这是**会改变邮箱状态**的操作）。
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, mark_read_policy="everything")  # type: ignore[arg-type]


def test_mark_read_folders_fall_back_to_synced_folders() -> None:
    """未单独配置回写范围时，跟随同步范围。"""
    settings = Settings(_env_file=None, imap_folders="INBOX,Archive")
    assert settings.mark_read_folder_list == ["INBOX", "Archive"]

    scoped = Settings(
        _env_file=None, imap_folders="INBOX,Archive", mark_read_folders="INBOX"
    )
    assert scoped.mark_read_folder_list == ["INBOX"]
