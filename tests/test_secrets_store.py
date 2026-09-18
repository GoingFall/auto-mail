"""DPAPI 凭据存储与明文迁移的测试。

重点不在"能加密"，而在**失败时的行为**：

* 解不开（换账号/换机器）不能崩，且要能区分「未配置」与「解不开」
* 迁移必须**先验证后擦除**——加密文件写坏了、明文又被抹掉，凭据就彻底丢了
* 加密存储与 ``.env`` 的优先级必须明确（``.env`` 优先，是逃生通道）
"""

from __future__ import annotations

import pytest

from automail import secrets_store as ss
from automail.secrets_store import (
    ALLOWED_KEYS,
    SECRETS_FILE_NAME,
    SecretsStoreError,
    load,
    migrate_plaintext_credentials,
    save,
)

# 本机不支持 DPAPI 时跳过「真实加解密」类用例，但**纯逻辑**用例仍要跑
requires_dpapi = pytest.mark.skipif(
    not ss.is_available(), reason="本机不支持 Windows DPAPI"
)


# ══════════════════════════════════════════════════════════════
# 加解密往返
# ══════════════════════════════════════════════════════════════


@requires_dpapi
def test_roundtrip(tmp_path) -> None:
    path = tmp_path / SECRETS_FILE_NAME
    save(path, {"IMAP_AUTH_CODE": "s3cret-value", "LLM_API_KEY": "sk-abc"})

    result = load(path)
    assert result.ok, result.error
    assert result.values["IMAP_AUTH_CODE"] == "s3cret-value"
    assert result.values["LLM_API_KEY"] == "sk-abc"


@requires_dpapi
def test_plaintext_is_not_recoverable_from_file(tmp_path) -> None:
    """**核心目的**：磁盘上不得出现明文。

    这正是引入本模块的理由——明文放在 ``.env`` 里，一次误分享/误备份就外泄。
    """
    path = tmp_path / SECRETS_FILE_NAME
    save(path, {"IMAP_AUTH_CODE": "PLAINTEXT-MARKER-9x8y7z"})

    raw = path.read_bytes()
    assert b"PLAINTEXT-MARKER-9x8y7z" not in raw


def test_whitelist_drops_unknown_keys(tmp_path) -> None:
    """只允许白名单内的键落盘（新增字段必须显式登记）。"""
    path = tmp_path / SECRETS_FILE_NAME
    if not ss.is_available():
        pytest.skip("no DPAPI")
    save(path, {"IMAP_AUTH_CODE": "a", "SOMETHING_ELSE": "b", "LLM_API_KEY": ""})

    assert set(load(path).values) == {"IMAP_AUTH_CODE"}, "未知键与空值都应被丢弃"
    assert "SOMETHING_ELSE" not in ALLOWED_KEYS


@requires_dpapi
def test_write_is_atomic_and_leaves_no_temp(tmp_path) -> None:
    path = tmp_path / SECRETS_FILE_NAME
    save(path, {"IMAP_AUTH_CODE": "a"})
    assert list(tmp_path.glob("*.tmp")) == []


@requires_dpapi
def test_overwrite_replaces_value(tmp_path) -> None:
    path = tmp_path / SECRETS_FILE_NAME
    save(path, {"IMAP_AUTH_CODE": "first"})
    save(path, {"IMAP_AUTH_CODE": "second"})
    assert load(path).values["IMAP_AUTH_CODE"] == "second"


# ══════════════════════════════════════════════════════════════
# 失败路径（必须优雅）
# ══════════════════════════════════════════════════════════════


def test_missing_file_is_not_an_error(tmp_path) -> None:
    """从未配置过不是错误——GUI 启动路径上调用它，不能报错。"""
    result = load(tmp_path / "absent.dat")
    assert result.ok
    assert result.values == {}
    assert result.error is None


def test_corrupt_file_reports_error_without_raising(tmp_path) -> None:
    """损坏的密文不能抛异常，且要给出可读原因。

    典型场景：把 ``data/`` 从旧机器拷过来，DPAPI 解不开。若这里抛异常，
    图形界面根本起不来，使用者连"去设置里重填"的机会都没有。
    """
    path = tmp_path / SECRETS_FILE_NAME
    path.write_bytes(b"this is not a DPAPI blob")
    result = load(path)
    assert not result.ok
    assert result.error
    assert result.values == {}


def test_empty_file_reports_error(tmp_path) -> None:
    path = tmp_path / SECRETS_FILE_NAME
    path.write_bytes(b"")
    result = load(path)
    assert not result.ok
    assert "空" in (result.error or "")


@requires_dpapi
def test_non_json_payload_reports_error(tmp_path) -> None:
    """合法 DPAPI 密文但内容不是 JSON → 报"损坏"，不是"解不开"。"""
    path = tmp_path / SECRETS_FILE_NAME
    path.write_bytes(ss.protect(b"definitely not json"))
    result = load(path)
    assert not result.ok
    assert "损坏" in (result.error or "")


def test_describe_status_distinguishes_states(tmp_path) -> None:
    """状态说明要能区分「没用加密」与「用了但读不了」。"""
    assert "未使用" in ss.describe_status(tmp_path)

    path = tmp_path / SECRETS_FILE_NAME
    path.write_bytes(b"broken")
    described = ss.describe_status(tmp_path)
    assert "无法读取" in described or "无法" in described


# ══════════════════════════════════════════════════════════════
# 与 Settings 的接线
# ══════════════════════════════════════════════════════════════


@requires_dpapi
def test_settings_reads_encrypted_credentials(tmp_path, monkeypatch) -> None:
    """加密凭据要能被 ``Settings`` 读到（键名大小写映射正确）。

    这是最容易静默出错的一环：普通可调用源返回的 dict 键必须**精确等于字段名**。
    实测过直接返回 ``IMAP_AUTH_CODE`` 会被静默忽略——凭据读出来是空的，
    而且不报错。
    """
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    save(tmp_path / "data" / SECRETS_FILE_NAME, {"IMAP_AUTH_CODE": "from-dpapi"})

    from automail.settings import Settings

    settings = Settings(_env_file=None)
    assert settings.imap_auth_code_value == "from-dpapi"
    # SecretStr 语义必须保留（repr 不泄露明文）
    assert "from-dpapi" not in repr(settings.imap_auth_code)


@requires_dpapi
def test_env_file_wins_over_encrypted(tmp_path, monkeypatch) -> None:
    """``.env`` 优先于加密存储 —— 这是刻意的逃生通道。

    加密文件解不开或损坏时，使用者应当能直接改 ``.env`` 把程序救回来。
    """
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    save(tmp_path / "data" / SECRETS_FILE_NAME, {"IMAP_AUTH_CODE": "from-dpapi"})
    (tmp_path / ".env").write_text("IMAP_AUTH_CODE=from-env\n", encoding="utf-8")

    from automail.settings import Settings

    settings = Settings()
    assert settings.imap_auth_code_value == "from-env"


@requires_dpapi
def test_broken_encrypted_file_does_not_break_settings(tmp_path, monkeypatch) -> None:
    """加密文件损坏时 ``Settings`` 仍能构造（退回到 .env／默认值）。"""
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / SECRETS_FILE_NAME).write_bytes(b"broken")

    from automail.settings import Settings

    ss._reset_last_error()
    settings = Settings(_env_file=None)
    assert settings.imap_auth_code_value == ""
    # 失败原因要能被界面读到，用于区分「未配置」与「解不开」
    assert ss.last_error()


# ══════════════════════════════════════════════════════════════
# 明文迁移
# ══════════════════════════════════════════════════════════════


@requires_dpapi
def test_migration_moves_plaintext_to_encrypted(tmp_path) -> None:
    """已有用户的明文凭据要迁进加密存储并从 ``.env`` 清除。"""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# 注释必须保留\nIMAP_USER=a@b.com\nIMAP_AUTH_CODE=plain-secret\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "data"

    result = migrate_plaintext_credentials(
        data_dir=data_dir,
        env_path=env_path,
        backup_dir=data_dir / "backups",
    )

    assert result.migrated == ["IMAP_AUTH_CODE"]
    assert result.failed is None
    # 加密文件里能读回
    assert load(data_dir / SECRETS_FILE_NAME).values["IMAP_AUTH_CODE"] == "plain-secret"
    # .env 里明文已消失，但键行与注释都还在
    text = env_path.read_text(encoding="utf-8")
    assert "plain-secret" not in text
    assert "IMAP_AUTH_CODE=" in text
    assert "# 注释必须保留" in text
    assert "IMAP_USER=a@b.com" in text
    assert result.backup is not None and result.backup.is_file()


@requires_dpapi
def test_migration_is_idempotent(tmp_path) -> None:
    """已迁移过就不该重复动作（否则每次启动都要动一次 .env）。"""
    env_path = tmp_path / ".env"
    env_path.write_text("IMAP_AUTH_CODE=x\n", encoding="utf-8")
    data_dir = tmp_path / "data"

    first = migrate_plaintext_credentials(data_dir=data_dir, env_path=env_path)
    assert first.changed

    second = migrate_plaintext_credentials(data_dir=data_dir, env_path=env_path)
    assert not second.changed
    assert second.skipped


@requires_dpapi
def test_migrated_credentials_are_usable_afterwards(tmp_path, monkeypatch) -> None:
    """**迁移的验收标准**：迁完之后新会话要能真的读到凭据。

    这是最容易出错的一环。迁移会把 ``.env`` 清成 ``IMAP_AUTH_CODE=``，而
    ``.env`` 的优先级**高于**加密存储——若不把空值当作「未设置」，这个空串会
    盖住加密存储里的真值，密码读出来是空的。

    实测踩到过：迁移报告成功、``.env`` 也清干净了，但 ``Settings`` 读到长度 0。
    因此这条断言必须存在：否则"迁移成功"是个假象，实际使用时会报缺凭据。
    """
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    env_path = tmp_path / ".env"
    env_path.write_text(
        "IMAP_USER=a@b.com\nIMAP_AUTH_CODE=plain-secret\nLLM_API_KEY=sk-plain\n",
        encoding="utf-8",
    )

    result = migrate_plaintext_credentials(
        data_dir=tmp_path / "data", env_path=env_path
    )
    assert result.changed

    from automail.settings import Settings

    settings = Settings()
    assert settings.imap_auth_code_value == "plain-secret"
    assert settings.llm_api_key_value == "sk-plain"
    # 非凭据字段不受影响
    assert settings.imap_user == "a@b.com"


@requires_dpapi
def test_empty_env_value_does_not_shadow_encrypted(tmp_path, monkeypatch) -> None:
    """**回归**：``.env`` 里的空值不得盖住加密存储的值。

    ``.env`` 优先于加密存储是刻意的逃生通道；但"空值"不是"我就要空"，
    而是"这里没填"。两者必须区分，否则逃生通道会变成陷阱。
    """
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    save(tmp_path / "data" / SECRETS_FILE_NAME, {"IMAP_AUTH_CODE": "from-dpapi"})
    (tmp_path / ".env").write_text("IMAP_AUTH_CODE=\n", encoding="utf-8")

    from automail.settings import Settings

    assert Settings().imap_auth_code_value == "from-dpapi"


@requires_dpapi
def test_nonempty_env_value_still_overrides_encrypted(tmp_path, monkeypatch) -> None:
    """非空的 ``.env`` 值仍然优先——逃生通道不能被上面的修正破坏。"""
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    save(tmp_path / "data" / SECRETS_FILE_NAME, {"IMAP_AUTH_CODE": "from-dpapi"})
    (tmp_path / ".env").write_text("IMAP_AUTH_CODE=manual-override\n", encoding="utf-8")

    from automail.settings import Settings

    assert Settings().imap_auth_code_value == "manual-override"


@requires_dpapi
def test_migration_keeps_plaintext_when_verification_fails(
    tmp_path, monkeypatch
) -> None:
    """**最关键的一条**：回读校验失败时，``.env`` 明文必须完好无损。

    构造「写成功但读不回来」的场景（模拟 DPAPI 异常），断言：
    ① 加密文件被清理掉（不留坏数据）② ``.env`` 原值一字未动
    ③ 返回可读的失败原因

    若这条不成立，就会出现「加密文件是坏的、明文又被抹了」→ 凭据彻底丢失。
    """
    env_path = tmp_path / ".env"
    env_path.write_text("IMAP_AUTH_CODE=must-survive\n", encoding="utf-8")
    original = env_path.read_text(encoding="utf-8")
    data_dir = tmp_path / "data"

    # 让回读返回不匹配的值
    monkeypatch.setattr(
        ss, "load", lambda path: ss.SecretsLoad(values={"IMAP_AUTH_CODE": "WRONG"})
    )

    result = migrate_plaintext_credentials(data_dir=data_dir, env_path=env_path)

    assert result.failed
    assert not result.changed
    assert env_path.read_text(encoding="utf-8") == original, "明文必须原样保留"
    assert not (data_dir / SECRETS_FILE_NAME).exists(), "坏掉的加密文件应被清理"


@requires_dpapi
def test_migration_keeps_plaintext_when_encryption_fails(tmp_path, monkeypatch) -> None:
    """加密本身失败时同样不能动 ``.env``。"""
    env_path = tmp_path / ".env"
    env_path.write_text("IMAP_AUTH_CODE=keep-me\n", encoding="utf-8")
    original = env_path.read_text(encoding="utf-8")

    def _boom(*args, **kwargs):
        raise SecretsStoreError("simulated failure")

    monkeypatch.setattr(ss, "save", _boom)
    result = migrate_plaintext_credentials(
        data_dir=tmp_path / "data", env_path=env_path
    )

    assert result.failed
    assert env_path.read_text(encoding="utf-8") == original


def test_migration_skips_when_no_plaintext(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("IMAP_USER=a@b.com\n", encoding="utf-8")
    result = migrate_plaintext_credentials(
        data_dir=tmp_path / "data", env_path=env_path
    )
    assert not result.changed
    assert result.skipped


def test_migration_skips_when_no_env_file(tmp_path) -> None:
    result = migrate_plaintext_credentials(
        data_dir=tmp_path / "data", env_path=tmp_path / ".env"
    )
    assert not result.changed
    assert result.skipped


@requires_dpapi
def test_migration_ignores_empty_values(tmp_path) -> None:
    """``.env`` 里被清空的键不算"有明文可迁"。"""
    env_path = tmp_path / ".env"
    env_path.write_text("IMAP_AUTH_CODE=\nLLM_API_KEY=\n", encoding="utf-8")
    result = migrate_plaintext_credentials(
        data_dir=tmp_path / "data", env_path=env_path
    )
    assert not result.changed
    assert result.skipped


@requires_dpapi
def test_migration_handles_both_credentials(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "IMAP_AUTH_CODE=auth-code\nLLM_API_KEY=sk-key\n", encoding="utf-8"
    )
    data_dir = tmp_path / "data"
    result = migrate_plaintext_credentials(data_dir=data_dir, env_path=env_path)

    assert result.migrated == ["IMAP_AUTH_CODE", "LLM_API_KEY"]
    values = load(data_dir / SECRETS_FILE_NAME).values
    assert values["IMAP_AUTH_CODE"] == "auth-code"
    assert values["LLM_API_KEY"] == "sk-key"
    text = env_path.read_text(encoding="utf-8")
    assert "auth-code" not in text and "sk-key" not in text


def test_protect_raises_on_unsupported_platform(monkeypatch) -> None:
    """非 Windows 上加密要明确报错，而不是静默产生无效数据。"""
    monkeypatch.setattr(ss, "is_available", lambda: False)
    with pytest.raises(SecretsStoreError):
        ss.protect(b"data")
    with pytest.raises(SecretsStoreError):
        ss.unprotect(b"data")
