"""Windows DPAPI 加密的凭据存储。

**为什么需要它**：v1 把 163 授权码与 LLM Key 明文放在 ``.env`` 里。明文的问题
不是"看不见"，而是**任何能读该文件的程序都能拿到**：一次误分享、一次备份到
云盘、一次打包进压缩包，凭据就外泄了。

DPAPI（``CryptProtectData``）把密钥绑定到**当前 Windows 用户**：同一用户的进程
可以解密，换个用户或把文件拷到别的机器就解不开。无需用户另记一个主密码，
也不会像自研加密那样把密钥和密文放在一起。

**威胁模型的边界（务必如实告知）**：这**不是**"文件本身加密所以万无一失"。
同一用户态下运行的任意进程都能解密 ``secrets.dat``——DPAPI 的固有边界就在
这里。它防的是文件被拷走／被同步／被误分享，不防本机已被同一用户执行的恶意
代码。文档里必须写清，避免使用者产生错误的安全感。

**零新依赖**：直接用 ctypes 调 ``crypt32``，不引入 ``pywin32`` / ``keyring``
——与项目一贯的极简依赖取向一致。

**跨平台**：非 Windows 上所有操作优雅降级（读取返回空、写入报明确错误），
这样测试可以在别的平台上运行而不必到处 skip。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("automail.secrets_store")

#: 加密凭据文件名（位于 ``data/`` 下，已被 .gitignore 覆盖）。
SECRETS_FILE_NAME = "secrets.dat"

#: 允许存入的键。**白名单**而非黑名单：新增字段必须显式登记，
#: 避免不小心把不该落盘的东西写进去。
ALLOWED_KEYS = frozenset(
    {
        "IMAP_AUTH_CODE",
        "LLM_API_KEY",
    }
)

#: DPAPI 标志：不弹任何 UI。
#:
#: 必须设：计划任务与图形界面都可能在没有交互桌面的会话里运行，
#: 若不设，``CryptProtectData`` 有可能尝试弹出确认框，把调用**永久挂住**。
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    """DPAPI 使用的 ``DATA_BLOB``。"""

    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


class SecretsStoreError(Exception):
    """凭据存储的致命错误（调用方应报告给使用者）。"""


@dataclass(slots=True)
class SecretsLoad:
    """一次读取的结果。

    ``values`` 与 ``error`` 分开返回，而不是只给一个 dict——**使用者需要区分
    「从未配置」与「配置过但解不开」**。后者（例如换了 Windows 账号、或从别的
    机器拷来了 ``data/``）如果只表现为"空值"，界面上就会显示成"未配置"，
    使用者会反复重填却始终不生效，且无从知道真实原因。
    """

    values: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def default_secrets_path(data_dir: Path) -> Path:
    return data_dir / SECRETS_FILE_NAME


def is_available() -> bool:
    """当前平台是否支持 DPAPI。"""
    if not sys.platform.startswith("win"):
        return False
    try:
        return bool(ctypes.windll.crypt32)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


# ──────────────────────────────────────────────────────────────
# DPAPI 原语
# ──────────────────────────────────────────────────────────────

def _blob_from_bytes(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _bytes_from_blob(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free_blob(blob: _DataBlob) -> None:
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)  # type: ignore[attr-defined]


def protect(plaintext: bytes) -> bytes:
    """用 DPAPI 加密。

    Raises:
        SecretsStoreError: 非 Windows，或 API 调用失败。
    """
    if not is_available():
        raise SecretsStoreError(
            "当前平台不支持 Windows DPAPI；请继续在 .env 中手工填写凭据"
        )

    blob_in = _blob_from_bytes(plaintext)
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(  # type: ignore[attr-defined]
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretsStoreError(
            f"DPAPI 加密失败（GetLastError={ctypes.GetLastError()}）"
        )
    try:
        return _bytes_from_blob(blob_out)
    finally:
        _free_blob(blob_out)


def unprotect(ciphertext: bytes) -> bytes:
    """用 DPAPI 解密。

    Raises:
        SecretsStoreError: 非 Windows，或解密失败（最常见的原因是换了
            Windows 账号／换了机器——DPAPI 的密钥与用户绑定）。
    """
    if not is_available():
        raise SecretsStoreError("当前平台不支持 Windows DPAPI")

    blob_in = _blob_from_bytes(ciphertext)
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(  # type: ignore[attr-defined]
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretsStoreError(
            "无法解密凭据文件：它由另一个 Windows 账号或另一台机器加密。"
            "请在设置里重新填写授权码（或把 .env 中的值填回去）。"
        )
    try:
        return _bytes_from_blob(blob_out)
    finally:
        _free_blob(blob_out)


# ──────────────────────────────────────────────────────────────
# 文件读写
# ──────────────────────────────────────────────────────────────

def load(path: Path) -> SecretsLoad:
    """读取凭据文件。

    **永不抛异常**：GUI 在启动路径上调用它，任何异常都会让窗口起不来。
    失败原因放进 ``SecretsLoad.error``，由界面决定怎么呈现。
    """
    if not path.is_file():
        return SecretsLoad()  # 从未配置过，不是错误
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return SecretsLoad(error=f"读取凭据文件失败：{exc}")

    if not raw:
        return SecretsLoad(error="凭据文件为空，可能上次写入被中断")

    try:
        plaintext = unprotect(raw)
    except SecretsStoreError as exc:
        return SecretsLoad(error=str(exc))

    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return SecretsLoad(error=f"凭据文件内容损坏：{exc}")

    if not isinstance(payload, dict):
        return SecretsLoad(error="凭据文件内容格式不正确（期望 JSON 对象）")

    values = {
        str(key).upper(): str(value)
        for key, value in payload.items()
        if str(key).upper() in ALLOWED_KEYS and isinstance(value, (str, int, float))
    }
    return SecretsLoad(values=values)


def save(path: Path, values: dict[str, str]) -> None:
    """加密写入凭据文件（原子替换）。

    只接受白名单内的键；未知键被忽略而非报错（调用方可能传入整份配置）。

    Raises:
        SecretsStoreError: 加密失败或写盘失败。
    """
    filtered = {
        key.upper(): str(value)
        for key, value in values.items()
        if key.upper() in ALLOWED_KEYS and str(value).strip()
    }

    payload = json.dumps(filtered, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ciphertext = protect(payload)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(ciphertext)
        os.replace(tmp, path)  # 原子：读者要么看到旧文件，要么看到新文件
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SecretsStoreError(f"写入凭据文件失败：{exc}") from exc


def clear(path: Path) -> bool:
    """删除凭据文件，返回是否真的删了。"""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SecretsStoreError(f"删除凭据文件失败：{exc}") from exc


# ──────────────────────────────────────────────────────────────
# 与 Settings 的接线
# ──────────────────────────────────────────────────────────────

#: 最近一次读取的失败原因。GUI 读它来区分「未配置」与「解不开」。
#:
#: 为什么用模块级状态而不是让 source 抛异常：pydantic 的
#: ``settings_customise_sources`` 只能返回 dict，**带不出错误信息**。
#: 所以由这里单独持有，界面直接查。
_last_error: str | None = None


def last_error() -> str | None:
    """最近一次加载凭据文件失败的原因（成功或文件不存在时为 ``None``）。"""
    return _last_error


def _reset_last_error() -> None:
    global _last_error
    _last_error = None


def build_settings_source(settings_cls: type) -> Any | None:
    """构造一个供 pydantic-settings 使用的加密凭据源。

    返回 ``None`` 表示**不接入**（文件不存在，或平台不支持）——此时
    ``Settings`` 的行为与引入本模块之前完全一致，不需要任何特殊处理。

    读到的键**原样放入返回的 dict**，交由 pydantic 做类型转换；这样
    ``imap_auth_code`` 等 ``SecretStr`` 字段的既有语义不变。
    """
    from .settings import app_base_dir

    _reset_last_error()

    path = default_secrets_path(app_base_dir() / "data")
    if not path.is_file():
        return None

    result = load(path)
    if not result.ok:
        global _last_error
        _last_error = result.error
        logger.warning("凭据文件加载失败：%s", result.error)
        # 返回 None：退回到 .env／环境变量，而不是让构造失败。
        # 「解不开」不该导致程序无法启动——使用者可能正要去设置里重填。
        return None

    # 把「环境变量名」映射成「字段名」。
    #
    # 必须做这一步：其他源（env / .env）由 pydantic 自己处理大小写不敏感，
    # 但**普通可调用源返回的 dict 会被当作字段值直接合并**，键必须精确等于
    # 字段名。实测过：直接返回 ``IMAP_AUTH_CODE`` 会被静默忽略，凭据读出来
    # 是空的——而且不报错，最难查的那种。
    fields = {name.upper(): name for name in settings_cls.model_fields}
    values = {
        fields[key.upper()]: value
        for key, value in result.values.items()
        if key.upper() in fields
    }
    if not values:
        return None

    class _EncryptedSettingsSource:
        """最小实现：只需提供 ``__call__`` 返回字段字典。"""

        def __init__(self, data: dict[str, str]) -> None:
            self._values = data

        def __call__(self) -> dict[str, Any]:
            return dict(self._values)

    return _EncryptedSettingsSource(values)


def describe_status(data_dir: Path) -> str:
    """给界面用的一句话状态说明。"""
    path = default_secrets_path(data_dir)
    if not path.is_file():
        return "未使用加密存储（凭据在 .env 中）"
    if not is_available():
        return "凭据文件存在，但当前平台无法读取 DPAPI"

    result = load(path)
    if result.ok:
        keys = "、".join(sorted(result.values)) or "（无）"
        return f"已加密保存：{keys}"
    return f"无法读取：{result.error}"


# ──────────────────────────────────────────────────────────────
# 明文凭据迁移
# ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class MigrationResult:
    """一次迁移尝试的结果。"""

    migrated: list[str] = field(default_factory=list)
    """成功迁入并已从 .env 清除的键。"""

    skipped: str | None = None
    """未执行的原因（已迁移过、没有明文可迁、平台不支持等）。"""

    failed: str | None = None
    """失败原因。**失败时 .env 原值必须完好**——这是本函数的硬约束。"""

    backup: Path | None = None

    @property
    def changed(self) -> bool:
        return bool(self.migrated)


def migrate_plaintext_credentials(
    *,
    data_dir: Path,
    env_path: Path,
    backup_dir: Path | None = None,
    backup_keep: int = 3,
) -> MigrationResult:
    """把 ``.env`` 里的明文凭据迁到加密存储。

    对**已有用户**是必需的：他们的授权码就明文放在 ``.env`` 里。若只引入加密
    存储而不迁移，程序会认为"没有凭据"，把人送回设置面板重填一遍——这是回归。

    顺序是刻意设计并加测试钉住的 —— **先验证，后擦除**：

    1. 读出 ``.env`` 中的明文（只读，不动磁盘）
    2. 加密写入 ``secrets.dat``
    3. **立即解密回读并逐键比对**（这一步才是关键：写成功不等于能读回来，
       例如 DPAPI 在某些配置下可能只写不读）
    4. 只有第 3 步完全通过，才备份 ``.env`` 并清空那两个键

    任何一步失败都**保留 ``.env`` 原值**并返回可读原因。宁可继续用明文，
    也不能出现"加密文件写坏了、明文又被抹了"导致凭据彻底丢失。

    幂等：``secrets.dat`` 已存在则直接跳过，不做任何事。
    """
    from . import envfile

    secrets_path = default_secrets_path(data_dir)

    if secrets_path.is_file():
        return MigrationResult(skipped="已存在加密凭据文件，无需迁移")
    if not is_available():
        return MigrationResult(skipped="当前平台不支持 DPAPI，继续使用 .env 明文")
    if not env_path.is_file():
        return MigrationResult(skipped="没有 .env 文件可迁移")

    try:
        entries = envfile.read_entries(env_path)
    except Exception as exc:  # noqa: BLE001 - 迁移失败绝不能影响启动
        return MigrationResult(failed=f"读取 .env 失败：{exc}")

    plaintext = {
        key: entries[key].value
        for key in ALLOWED_KEYS
        if key in entries and entries[key].value.strip()
    }
    if not plaintext:
        return MigrationResult(skipped=".env 中没有可迁移的明文凭据")

    # ① 加密写入
    try:
        save(secrets_path, plaintext)
    except SecretsStoreError as exc:
        return MigrationResult(failed=f"写入加密凭据失败（.env 保持原样）：{exc}")

    # ② 回读校验 —— 写成功不等于读得回来
    check = load(secrets_path)
    if not check.ok:
        _rollback(secrets_path)
        return MigrationResult(
            failed=f"加密凭据写入后无法读回（.env 保持原样）：{check.error}"
        )
    mismatched = [key for key, value in plaintext.items() if check.values.get(key) != value]
    if mismatched:
        _rollback(secrets_path)
        return MigrationResult(
            failed=(
                f"加密凭据回读不一致（.env 保持原样）：{', '.join(sorted(mismatched))}"
            )
        )

    # ③ 校验通过，才清空明文
    try:
        result = envfile.blank_out(
            env_path,
            sorted(plaintext),
            note="已改用 Windows 加密存储，可在图形界面中修改；此处留空即可",
            backup_dir=backup_dir,
            backup_keep=backup_keep,
        )
    except Exception as exc:  # noqa: BLE001
        # 加密文件已就绪、明文还在 —— 两者内容一致，属安全状态（下次再清）
        return MigrationResult(
            migrated=sorted(plaintext),
            failed=f"加密成功，但清空 .env 明文失败（凭据仍可用）：{exc}",
        )

    logger.info("已迁移 %d 项凭据到加密存储", len(plaintext))
    return MigrationResult(
        migrated=sorted(plaintext), backup=result.backup
    )


def _rollback(secrets_path: Path) -> None:
    """删除校验失败的加密文件，避免下次启动读到坏数据。"""
    try:
        secrets_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("清理无效凭据文件失败：%s", exc)
