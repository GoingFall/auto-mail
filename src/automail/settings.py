"""配置层：从 .env / 环境变量加载全部可调项。

设计要点
--------
* 字段名小写下划线，环境变量名即大写下划线（pydantic-settings 默认行为），
  例如 ``auto_push_delay_minutes`` ← ``AUTO_PUSH_DELAY_MINUTES``。
* **缺少密钥不是致命错误**：所有密钥/凭据默认空字符串，由 doctor 报告为
  MISSING 而不是让进程崩溃（规格 §13）。只有「配置值本身非法」（例如时区
  不存在、时间格式错误）才算致命。
* 路径类配置只保存根目录，派生路径（db、备份目录）用 property 计算，
  避免出现两处配置互相矛盾。
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict

TIME_FORMAT = "%H:%M"

#: ``.env`` 的文件名（相对基目录）。
ENV_FILE_NAME = ".env"

#: 需要按基目录锚定的路径字段（**全部** Path 字段）。
#:
#: 集中列出而不是散落各处：漏掉一个就会出现"某些路径跟着 CWD 跑"的诡异行为，
#: 而那种 bug 只在特定的启动方式下暴露，极难定位。有测试断言这份清单与
#: ``Settings`` 里实际的 Path 字段完全一致。
_PATH_FIELDS = (
    "data_dir",
    "out_dir",
    "log_dir",
    "google_credentials_file",
    "google_token_file",
)


def is_frozen() -> bool:
    """当前是否运行在打包后的可执行文件里（PyInstaller 等）。"""
    return bool(getattr(sys, "frozen", False))


def app_base_dir() -> Path:
    """应用基目录：相对路径配置以此为根。

    * **打包后**：exe 所在目录。必须如此——双击运行或计划任务启动时
      工作目录是不确定的（可能是 ``C:\\Windows\\System32``），
      数据会落到使用者找不到的地方。
    * **源码运行**：当前工作目录，与既有行为一致。

    可用环境变量 ``AUTOMAIL_HOME`` 显式覆盖，便于把数据放到别处
    （例如不受云盘同步的目录）。
    """
    override = os.environ.get("AUTOMAIL_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def default_migrations_dir() -> Path:
    """迁移脚本所在目录。

    打包后资源可能落在两个位置之一（取决于打包工具与目标布局），
    因此按优先级探测，最后退回源码目录。**只在目录真实存在时才采用**，
    避免给出一个不存在的路径让迁移静默跳过。
    """
    candidates: list[Path] = []

    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        # PyInstaller：资源根目录（onedir 为 _internal）
        candidates.append(Path(bundle) / "automail" / "migrations")
        candidates.append(Path(bundle) / "migrations")

    # 源码运行 / onedir 下 __file__ 指向包内
    candidates.append(Path(__file__).resolve().parent / "migrations")
    # 冻结时 exe 旁边的 migrations（便于使用者自行调整）
    if is_frozen():
        candidates.append(Path(sys.executable).resolve().parent / "migrations")

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def env_file_path() -> Path:
    """``.env`` 的绝对路径（基目录下）。

    与 :func:`app_base_dir` 一样锚到基目录，而不是进程工作目录——理由见
    :meth:`Settings._resolve_relative_config_paths`。
    """
    return app_base_dir() / ENV_FILE_NAME


def _resolve_env_file(value: object) -> object:
    """把 ``env_file`` 里相对路径解析为基目录下的绝对路径。

    支持 pydantic-settings 允许的全部形态：``str`` / ``Path`` / 它们的列表。
    ``None`` 表示**显式关闭**（测试用 ``_env_file=None`` 构造隔离配置），
    必须原样返回——若在这里"补"上一个路径，测试就会读到开发者本机的
    ``.env``，结果随本机配置漂移。
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return type(value)(_resolve_env_file(item) for item in value)  # type: ignore[call-arg]

    path = Path(value) if not isinstance(value, Path) else value
    if path.is_absolute():
        return path
    return app_base_dir() / path



def _secret_value(value: object) -> str:
    """取出密钥类配置的明文，容忍 ``SecretStr`` 与普通字符串两种形态。

    为什么需要容忍：配置字段声明为 ``SecretStr``（用于掩码日志），但测试与
    某些构造路径会直接赋字符串。硬调 ``get_secret_value()`` 会在那种情况下
    抛 ``AttributeError``——一个与配置内容无关的崩溃。
    """
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter()).strip()
    return str(value or "").strip()


class Settings(BaseSettings):
    """全部配置项。字段分组与 .env.example 一一对应。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        #: 空值视为「未设置」，而不是「显式的空字符串」。
        #:
        #: 这一条对加密凭据是**必需**的：迁移会把 ``.env`` 里的密码清成
        #: ``IMAP_AUTH_CODE=``，而 ``.env`` 的优先级高于加密存储——若不忽略空值，
        #: 这个空串会**盖住**加密存储里的真实值，密码读出来是空的。
        #: 实测踩到过：迁移报告成功，但新会话读到长度 0。
        #:
        #: 与本项目既有语义也一致：代码里到处是
        #: ``mark_read_folders or imap_folders``、``user_addresses or imap_user``
        #: 这类「空则回退」的写法。``.env.example`` 里的 ``KEY=`` 占位也是
        #: 「等你填」而不是「就是要空」。
        env_ignore_empty=True,
    )

    # ── 1. 163 邮箱 ───────────────────────────────────────────
    #: 账号标识。单账号场景下只是个标签；保留它是为了给多邮箱留出空间
    #: （数据库所有唯一约束都含 account 维度）。
    account: str = "163"
    imap_host: str = "imap.163.com"
    imap_port: int = Field(default=993, ge=1, le=65535)
    #: 163 用隐式 SSL（993）。仅在测试或本地明文代理场景下才置 false。
    imap_use_ssl: bool = True
    imap_user: str = ""
    #: 用户自己的全部邮箱地址（主地址 + 别名 + SMTP 发件地址），逗号分隔。
    #: 用于判断邮件方向（收信/发信）。留空时线程方向一律判为「收信」——
    #: 宁可判成收信，也不要凭空声称用户发过信（那会让「等回复」判断全错）。
    user_addresses: str = ""
    imap_auth_code: SecretStr = SecretStr("")
    imap_folders: str = "INBOX"
    # 163/Coremail 声明 SASL-IR 却拒绝 inline 形式，必须为 false。
    imap_use_sasl_ir: bool = False
    imap_client_id_name: str = "auto-mail"
    imap_client_id_version: str = "0.1.0"
    imap_client_id_vendor: str = "auto-mail"
    imap_client_id_support_email: str = ""
    imap_socket_timeout: int = Field(default=60, ge=5, le=600)
    #: 单次 FETCH 的邮件数上限。**必须分批**：实测一次性取 89 封会被 163
    #: 风控直接重置连接（WinError 10054），而 3~10 封均正常。
    imap_fetch_batch_size: int = Field(default=10, ge=1, le=200)

    # ── 已读回写 ──────────────────────────────────────────────
    #
    # 这是**本项目唯一对邮箱的写操作**。v1 全程只读（EXAMINE + BODY.PEEK），
    # 因此默认值必须是 ``off``——不能让升级后的既有用户邮箱状态被静默改变。
    #
    # 取值：
    #   off        不写（默认）
    #   resolved   仅在「该邮件已无需你再做什么」时标记
    #   processed  所有抽取完成的邮件都标记
    #
    # ``resolved`` 比 ``processed`` 保守：仍有待审/失败事件时保持未读，
    # 让「未读」继续表示「需要你处理」。
    mark_read_policy: Literal["off", "resolved", "processed"] = "off"

    #: 只回写这些文件夹（逗号分隔）。空表示用 ``imap_folders``。
    #:
    #: 单独配置的原因：通常只需要整理 INBOX——把「已发送」「垃圾箱」里的信件
    #: 标为已读没有意义，而且那些文件夹的邮件可能根本没被抽取过。
    mark_read_folders: str = ""

    #: 单次 STORE 的 UID 数上限。与 FETCH 同理，避免一次命令过大触发风控。
    mark_read_batch_size: int = Field(default=50, ge=1, le=500)
    #: 批次失败后的重连重试次数。163 会在任意时刻使会话失效（闲置超时约 2~4
    #: 分钟、另一客户端登录、限流），此时应重连续传而不是放弃整轮同步。
    imap_reconnect_attempts: int = Field(default=3, ge=0, le=10)
    #: 重连前的等待秒数（线性退避：1×、2×、3×…）。
    imap_reconnect_backoff_seconds: float = Field(default=3.0, ge=0.0, le=60.0)
    # 163 不支持 IDLE，只能轮询。低于 15 分钟易触发风控。
    poll_interval_minutes: int = Field(default=15, ge=1)

    # ── 2. Google Calendar ────────────────────────────────────
    #: 相对路径会由 :meth:`_resolve_relative_config_paths` 解析为
    #: **基目录下的绝对路径**（见该方法的说明）。
    google_credentials_file: Path = Path("credentials.json")
    google_token_file: Path = Path("token.json")
    google_calendar_id: str = "primary"
    #: 日历后端选择：
    #: * ``auto``（默认）—— 有 credentials.json + token.json 则用真实 Google，
    #:   否则退回内存实现并提示。避免「以为在写真实日历，其实只写了内存」。
    #: * ``google`` —— 强制真实后端，缺凭据时报错。
    #: * ``fake`` —— 强制内存后端（演练用，不触碰真实日历）。
    calendar_backend: Literal["auto", "google", "fake"] = "auto"
    # 代理（Google 不可直连时用；库自身也读这些环境变量）
    https_proxy: str = ""
    http_proxy: str = ""
    no_proxy: str = "imap.163.com,imap.126.com,localhost,127.0.0.1"

    # ── 3. LLM ────────────────────────────────────────────────
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "deepseek-chat"
    llm_timeout: int = Field(default=30, ge=5, le=300)
    llm_max_calls_per_run: int = Field(default=50, ge=0)
    # 逗号分隔的白名单；可选 excerpt, subject, received_at, timezone, sender_name
    llm_payload_fields: str = "excerpt,subject,received_at,timezone"
    llm_excerpt_max_chars: int = Field(default=1500, ge=100)
    llm_max_input_tokens: int = Field(default=2000, ge=100)

    # ── 4. 抽取与入历策略 ────────────────────────────────────
    excerpt_max_chars: int = Field(default=4000, ge=200)
    extract_max_attempts: int = Field(default=3, ge=1)
    #: 超过此分钟数仍停在 extract_status='running' 的记录会被回收为 pending。
    #: 崩溃（或进程被杀）会留下这种中间态；不回收就永远不会再被领取。
    extract_zombie_minutes: int = Field(default=30, ge=1)

    #: 单实例锁的存活时间（秒）。崩溃留下的锁会在此时间后自动失效，
    #: 不需要人工清理——这是用数据库锁而非文件锁的原因。
    #: 应大于一次完整运行的最长耗时（首次同步大邮箱可能几分钟）。
    run_lock_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    # 严格大于：仅 0.95 档（绝对日期+显式时刻）可自动入历
    confidence_auto_push_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    auto_push_limit_per_run: int = Field(default=10, ge=0)
    auto_push_delay_minutes: int = Field(default=5, ge=0)
    ics_auto_push_non_contact: bool = False
    push_max_attempts: int = Field(default=5, ge=1)
    not_found_policy: Literal["pending", "recreate", "fail"] = "pending"
    ambiguous_date_policy: Literal["pending", "earliest"] = "pending"
    low_confidence_event_policy: Literal["pending"] = "pending"
    default_event_duration_minutes: int = Field(default=30, ge=1, le=1440)
    default_reminder_time: str = "09:00"

    # ── 5. 同步一致性与存储 ──────────────────────────────────
    compensate_scans: int = Field(default=20, ge=1)
    compensate_days: int = Field(default=14, ge=1)
    db_backup_keep: int = Field(default=3, ge=1)
    db_backup_max_age_days: int = Field(default=14, ge=1)

    # ── 6. 通用 ───────────────────────────────────────────────
    user_timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    # 路径默认相对于「应用基目录」，见 app_base_dir()：
    # 源码运行时是当前工作目录，打包成 exe 后是 exe 所在目录。
    # 这一点对可执行文件是必要的——否则双击运行或由计划任务启动时，
    # 数据会落到不确定的工作目录里。
    #
    # 注意：这些**同时也会被 .env 覆盖**（模板里就写着 ``DATA_DIR=data``）。
    # 因此相对路径的锚定必须发生在校验阶段，不能只靠 default_factory——
    # 见 _resolve_relative_config_paths。实测踩到过：frozen exe 从别的目录
    # 启动时，``data`` 被解析到当前工作目录（甚至 ``C:\Windows\data``），
    # 报「拒绝访问」——而使用者的数据其实在 exe 旁边。
    data_dir: Path = Field(default_factory=lambda: app_base_dir() / "data")
    out_dir: Path = Field(default_factory=lambda: app_base_dir() / "out")
    log_dir: Path = Field(default_factory=lambda: app_base_dir() / "logs")
    # 迁移脚本目录。优先找打包后的资源位置，再退回源码目录。
    migrations_dir: Path = Field(default_factory=default_migrations_dir)

    @model_validator(mode="after")
    def _resolve_relative_config_paths(self) -> Settings:
        """把相对的配置路径解析为**基目录下的绝对路径**。

        为什么必须在**校验阶段**做，而不能只靠 ``default_factory``：
        ``.env``（与环境变量）的优先级高于默认值，而模板里就写着
        ``DATA_DIR=data``、``GOOGLE_CREDENTIALS_FILE=credentials.json``
        这些**相对路径**。它们一旦被读进来，就会按进程工作目录解析。

        命令行工具一直没暴露这个问题，只因为启动器先切了目录
        （``auto-mail-run.cmd`` 里的 ``cd /d "%~dp0"``、``run.ps1`` 里的
        ``Set-Location``）。图形界面从桌面快捷方式启动时工作目录是别处，
        于是：读不到 ``.env`` 与凭据、数据库指向错误的目录——实测在
        ``C:\\Windows`` 下启动直接报「拒绝访问: 'data'」。

        锚定到 :func:`app_base_dir` 之后，从哪儿启动都指向同一批文件。
        对既有的两条路径行为等价：源码运行时 ``app_base_dir()`` 就是当前
        工作目录；打包后经包装器启动时它是 exe 目录，与工作目录一致。

        副作用（有意为之）：``AUTOMAIL_HOME`` 现在会一并移动配置与凭据文件，
        此前只影响 ``data/out/logs``。
        """
        base = app_base_dir()
        for name in _PATH_FIELDS:
            value = getattr(self, name, None)
            if value is None:
                continue
            path = Path(value)
            if not path.is_absolute():
                setattr(self, name, base / path)
        return self

    # ── 校验 ──────────────────────────────────────────────────

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: object,
        env_settings: object,
        dotenv_settings: object,
        file_secret_settings: object,
    ) -> tuple[object, ...]:
        """把 ``.env`` 的查找位置锚到基目录，并把加密凭据接在 ``.env`` 之后。

        默认情况下 pydantic-settings 会按**进程工作目录**解析相对 ``env_file``。
        命令行工具没暴露这个问题，只因为启动器先 ``cd`` 到了正确目录；图形界面
        从快捷方式启动时工作目录是别处，会读不到 ``.env``（表现为「明明配好了
        却报缺少凭据」）。这里重建 dotenv 源并换成绝对路径。

        顺序是 ``init → 环境变量 → .env → 加密凭据 → secrets 目录``。把加密凭据
        放在 ``.env`` **之后**是有意的：手工在 ``.env`` 里填的值始终优先，
        这是一条随时可用的逃生通道（例如加密文件损坏、或换机器后解不开）。
        """
        sources: list[object] = [init_settings, env_settings]

        env_file = getattr(dotenv_settings, "env_file", None)
        if env_file is None:
            # 显式关闭（测试用 _env_file=None）→ 保持关闭，不读任何 .env
            sources.append(dotenv_settings)
        else:
            resolved = _resolve_env_file(env_file)
            if resolved == env_file:
                sources.append(dotenv_settings)
            else:
                # env_vars 在构造时就已读盘，改属性无效，必须重建。
                sources.append(
                    DotEnvSettingsSource(
                        settings_cls,
                        env_file=resolved,  # type: ignore[arg-type]
                        env_file_encoding=getattr(
                            dotenv_settings, "env_file_encoding", None
                        ),
                        case_sensitive=getattr(dotenv_settings, "case_sensitive", None),
                        env_prefix=getattr(dotenv_settings, "env_prefix", None),
                        env_nested_delimiter=getattr(
                            dotenv_settings, "env_nested_delimiter", None
                        ),
                        env_ignore_empty=getattr(
                            dotenv_settings, "env_ignore_empty", None
                        ),
                        env_parse_none_str=getattr(
                            dotenv_settings, "env_parse_none_str", None
                        ),
                    )
                )

        # 加密凭据（DPAPI）。放在 .env 之后 → 手工配置优先。
        from .secrets_store import build_settings_source

        encrypted = build_settings_source(settings_cls)
        if encrypted is not None:
            sources.append(encrypted)

        sources.append(file_secret_settings)
        return tuple(sources)

    # ── v2 预留 ───────────────────────────────────────────────
    reply_threshold_hours: int = Field(default=24, ge=1)

    @field_validator("default_reminder_time")
    @classmethod
    def _validate_reminder_time(cls, value: str) -> str:
        from datetime import datetime

        try:
            datetime.strptime(value, TIME_FORMAT)
        except ValueError as exc:
            raise ValueError(
                f"default_reminder_time 必须是 HH:MM 格式，收到 {value!r}"
            ) from exc
        return value

    @field_validator("user_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"user_timezone 不是合法 IANA 时区名：{value!r}") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level 必须是 {sorted(allowed)} 之一，收到 {value!r}")
        return upper

    # ── 派生值 ────────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        return self.data_dir / "automail.db"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def imap_folder_list(self) -> list[str]:
        """``IMAP_FOLDERS`` 解析结果，去重且保序。"""
        seen: dict[str, None] = {}
        for raw in self.imap_folders.split(","):
            name = raw.strip()
            if name:
                seen.setdefault(name, None)
        return list(seen)

    @property
    def mark_read_folder_list(self) -> list[str]:
        """已读回写作用的文件夹。

        留空则回退到 ``IMAP_FOLDERS``（即只处理同步过的文件夹）——对未同步的
        文件夹做回写没有依据，因为那里没有抽取状态可判断。
        """
        source = self.mark_read_folders or self.imap_folders
        seen: dict[str, None] = {}
        for raw in source.split(","):
            name = raw.strip()
            if name:
                seen.setdefault(name, None)
        return list(seen)

    @property
    def mark_read_enabled(self) -> bool:
        return self.mark_read_policy != "off"

    @property
    def user_address_list(self) -> list[str]:
        """用户地址列表，去重且保序。

        未显式配置时回退到 ``IMAP_USER``——单账号场景下它通常就是用户地址。
        """
        seen: dict[str, None] = {}
        for raw in (self.user_addresses or self.imap_user).split(","):
            name = raw.strip().lower()
            if name:
                seen.setdefault(name, None)
        return list(seen)

    @property
    def llm_payload_field_list(self) -> list[str]:
        """LLM 请求字段白名单，去重且保序。"""
        seen: dict[str, None] = {}
        for raw in self.llm_payload_fields.split(","):
            name = raw.strip().lower()
            if name:
                seen.setdefault(name, None)
        return list(seen)

    @property
    def imap_auth_code_value(self) -> str:
        """取出授权码明文（仅在实际登录时调用）。

        容忍 ``SecretStr`` 与普通字符串两种形态：测试与某些构造路径会直接
        赋字符串，若这里硬调 ``get_secret_value()`` 会抛 ``AttributeError``，
        而那是个与配置无关的崩溃。
        """
        return _secret_value(self.imap_auth_code)

    @property
    def llm_api_key_value(self) -> str:
        return _secret_value(self.llm_api_key)

    # ── 副作用 ────────────────────────────────────────────────

    def ensure_dirs(self) -> list[Path]:
        """创建 data/out/logs 及备份目录，返回本次实际新建的目录。"""
        created: list[Path] = []
        for directory in (self.data_dir, self.out_dir, self.log_dir, self.backup_dir):
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                created.append(directory)
        return created

    def apply_proxy_env(self, env: MutableMapping[str, str] | None = None) -> None:
        """把配置中的代理写回环境变量，供 google-api-python-client 等使用。

        只在目标变量尚未存在时写入（``setdefault`` 语义），避免覆盖用户 shell
        里已有的代理设置。

        Args:
            env: 目标映射，默认 ``os.environ``。测试可传入普通 dict，
                从而完全不触碰进程环境。
        """
        target = os.environ if env is None else env
        if self.https_proxy:
            target.setdefault("HTTPS_PROXY", self.https_proxy)
            target.setdefault("https_proxy", self.https_proxy)
        if self.http_proxy:
            target.setdefault("HTTP_PROXY", self.http_proxy)
            target.setdefault("http_proxy", self.http_proxy)
        if self.no_proxy:
            target.setdefault("NO_PROXY", self.no_proxy)
            target.setdefault("no_proxy", self.no_proxy)

    def describe_proxies(self) -> str:
        """doctor 展示用的代理摘要。

        NO_PROXY 只在确实配置了代理时才有意义，因此单独配置它不算「已配置代理」。
        """
        https = self.https_proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        http = self.http_proxy or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
        no_proxy = self.no_proxy or os.environ.get("NO_PROXY") or os.environ.get("no_proxy")

        parts: list[str] = []
        if https:
            parts.append(f"HTTPS_PROXY={https}")
        if http:
            parts.append(f"HTTP_PROXY={http}")
        if not parts:
            return "未配置（直连）"
        if no_proxy:
            parts.append(f"NO_PROXY={no_proxy}")
        return "，".join(parts)


class SettingsError(Exception):
    """配置加载失败（致命）。"""


def load_settings(**overrides: object) -> Settings:
    """加载配置。

    ``overrides`` 便于测试直接注入字段值而不依赖 .env。

    Raises:
        SettingsError: 配置值非法（例如时区不存在）。调用方应视为致命错误
            （退出码 2），而不是当作「缺少密钥」。
    """
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except Exception as exc:  # pydantic.ValidationError 及其子类
        raise SettingsError(str(exc)) from exc
