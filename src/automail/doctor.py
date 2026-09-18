"""``automail doctor``：自检。

两种模式
--------
* 默认**离线**：只检查配置完整性、依赖、目录可写、数据库 schema、时区。
  缺密钥报告为 MISSING，**不判整体失败**（退出码 1 而非 2），
  因此没有凭据时也能验证项目是否能跑。
* ``--live``：额外连 IMAP / Google / LLM。P0 阶段这些项目报 SKIPPED，
  待 P4 接入真实后端后实现。
"""

from __future__ import annotations

import importlib
import importlib.metadata
import tempfile

from .db import DatabaseError, current_version, has_tables, open_db
from .exits import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from .models import ReadinessItem, ReadinessStatus
from .settings import Settings

#: (import 名, 发行包名) —— 用于同时校验「能 import」与「版本可查」
REQUIRED_DISTRIBUTIONS: tuple[tuple[str, str], ...] = (
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("typer", "typer"),
    ("rich", "rich"),
    ("imapclient", "imapclient"),
    ("bs4", "beautifulsoup4"),
    ("charset_normalizer", "charset-normalizer"),
    ("dateparser", "dateparser"),
    ("dateutil", "python-dateutil"),
    ("icalendar", "icalendar"),
    ("googleapiclient", "google-api-python-client"),
    ("google_auth_oauthlib", "google-auth-oauthlib"),
    ("openai", "openai"),
)

#: 单实例锁名，doctor 用来报告是否有运行在进行中
RUN_LOCK = "automail.run"


def _item(
    name: str,
    status: ReadinessStatus,
    detail: str = "",
    *,
    fatal: bool = False,
) -> ReadinessItem:
    return ReadinessItem(name=name, status=status, detail=detail, fatal=fatal)


def compute_exit_code(items: list[ReadinessItem]) -> int:
    """汇总退出码。

    判定顺序：
    1. 存在 ``fatal=True`` 的 ERROR → 2（致命，必须先修）
    2. 存在 MISSING（缺凭据）或任何非致命 ERROR → 1（部分缺失）
    3. 全 OK/SKIPPED → 0

    注意非致命 ERROR 也算 1：任何一处异常都不应被静默判成成功。
    """
    if any(
        item.status is ReadinessStatus.ERROR and item.fatal for item in items
    ):
        return EXIT_FATAL
    if any(
        item.status in (ReadinessStatus.MISSING, ReadinessStatus.ERROR)
        for item in items
    ):
        return EXIT_PARTIAL
    return EXIT_OK


# ──────────────────────────────────────────────────────────────
# 单项检查
# ──────────────────────────────────────────────────────────────

def check_env_file() -> ReadinessItem:
    """检查 ``.env`` 是否可用。

    路径必须与**实际加载配置的地方**一致（:func:`env_file_path`），不能用
    ``Path(".env")``——那是进程工作目录，从快捷方式或别的目录启动时并不指向
    配置文件所在处。实测过：配置其实已正确加载（凭据都读到了），doctor 却报
    「未找到 .env」——一个会把人带偏的假警报。
    """
    from .settings import env_file_path

    path = env_file_path()
    if path.is_file():
        return _item("配置文件", ReadinessStatus.OK, f"已加载 {path}")
    return _item(
        "配置文件",
        ReadinessStatus.SKIPPED,
        f"未找到 {path}，将使用默认值；可从 .env.example 复制后填写",
    )


def check_dependencies() -> list[ReadinessItem]:
    items: list[ReadinessItem] = []
    for module_name, dist_name in REQUIRED_DISTRIBUTIONS:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            items.append(
                _item(
                    f"依赖 {dist_name}",
                    ReadinessStatus.ERROR,
                    f"无法 import {module_name}：{exc}；请运行 pip install -e .",
                    fatal=True,
                )
            )
            continue
        try:
            version = importlib.metadata.version(dist_name)
        except importlib.metadata.PackageNotFoundError:
            version = "未知版本"
        items.append(_item(f"依赖 {dist_name}", ReadinessStatus.OK, version))
    return items


def check_directories(settings: Settings) -> ReadinessItem:
    """逐个目录试写临时文件，确认真正可写。"""
    created = settings.ensure_dirs()
    targets = (settings.data_dir, settings.out_dir, settings.log_dir, settings.backup_dir)
    failures: list[str] = []
    for directory in targets:
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".write-test-", delete=True):
                pass
        except OSError as exc:
            failures.append(f"{directory}: {exc}")
    if failures:
        return _item(
            "目录可写",
            ReadinessStatus.ERROR,
            "以下目录不可写：" + "；".join(failures),
            fatal=True,
        )
    detail = "，".join(str(d) for d in targets)
    if created:
        detail += f"（本次新建 {len(created)} 个）"
    return _item("目录可写", ReadinessStatus.OK, detail)


def check_database(settings: Settings) -> tuple[ReadinessItem, ReadinessItem]:
    """返回 (schema 检查, 运行记录) 两项。"""
    try:
        with open_db(settings) as conn:
            version = current_version(conn)
            table_count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            seeded = has_tables(conn)
            run_count = (
                conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] if seeded else 0
            )
            stale_locks = conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0]
    except DatabaseError as exc:
        return (
            _item("数据库", ReadinessStatus.ERROR, f"不可用：{exc}", fatal=True),
            _item("运行记录", ReadinessStatus.SKIPPED, "数据库不可用，已跳过"),
        )

    schema_item = _item(
        "数据库",
        ReadinessStatus.OK,
        f"{settings.db_path} · schema v{version} · {table_count} 张表",
    )
    detail = f"历史运行 {run_count} 次"
    if stale_locks:
        detail += f" · 锁表 {stale_locks} 条（过期锁会在下次运行被抢占）"
    return schema_item, _item("运行记录", ReadinessStatus.OK, detail)


def check_timezone(settings: Settings) -> ReadinessItem:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(settings.user_timezone))
    return _item(
        "时区",
        ReadinessStatus.OK,
        f"{settings.user_timezone}（当前 {now.strftime('%Y-%m-%d %H:%M %z')}）",
    )


def check_imap_config(settings: Settings) -> ReadinessItem:
    missing: list[str] = []
    if not settings.imap_user.strip():
        missing.append("IMAP_USER")
    if not settings.imap_auth_code_value:
        missing.append("IMAP_AUTH_CODE")
    if missing:
        return _item(
            "163 邮箱凭据",
            ReadinessStatus.MISSING,
            "缺少 " + "、".join(missing)
            + "；授权码在网页版 设置 → POP3/SMTP/IMAP 中新增（16 位，只显示一次）",
        )
    return _item(
        "163 邮箱凭据",
        ReadinessStatus.OK,
        f"{settings.imap_user} @ {settings.imap_host}:{settings.imap_port}"
        f" · 文件夹 {settings.imap_folder_list}",
    )


def check_google_config(settings: Settings) -> ReadinessItem:
    """检查 Google 日历接入状态。

    区分三种「没就绪」，因为使用者的下一步动作完全不同：

    * 没有 ``credentials.json`` → 要去 Google Cloud 建客户端
    * 有 credentials 但没 token → 只需运行 ``automail auth``
    * token 存在但已失效 → 需 ``auth --revoke`` 后重新授权

    另外要校验 ``credentials.json`` 的**客户端类型**：建成「Web 应用」是常见
    错误，而那种类型无法走本地回调。与其等到授权时才莫名失败，不如在这里
    就说清楚。
    """
    from .calendar.auth import inspect_status

    info = inspect_status(settings)

    if not info.credentials_present:
        return _item(
            "Google 凭据",
            ReadinessStatus.MISSING,
            f"未找到 {info.credentials_file}；需在 Google Cloud 创建"
            "「桌面应用」OAuth 客户端并下载",
        )

    # 即使文件存在，也要确认它是桌面应用类型
    try:
        from .calendar.auth import validate_credentials_file

        validate_credentials_file(info.credentials_file)
    except Exception as exc:  # noqa: BLE001 - 校验失败信息本身就是给用户看的
        return _item("Google 凭据", ReadinessStatus.ERROR, str(exc))

    if not info.token_present:
        return _item(
            "Google 凭据",
            ReadinessStatus.MISSING,
            f"{info.credentials_file} 格式正确，但尚未授权；"
            "请运行 automail auth（会打开浏览器，需你点「允许」）",
        )

    if info.needs_reauth:
        return _item(
            "Google 凭据",
            ReadinessStatus.ERROR,
            "授权已失效（invalid_grant），需重新授权："
            "automail auth --revoke 后再运行 automail auth",
        )

    return _item(
        "Google 凭据",
        ReadinessStatus.OK,
        f"已授权 · 日历 {settings.google_calendar_id} · "
        f"范围 calendar.events（最小权限）",
    )


def check_llm_config(settings: Settings) -> ReadinessItem:
    missing: list[str] = []
    if not settings.llm_base_url.strip():
        missing.append("LLM_BASE_URL")
    if not settings.llm_api_key_value:
        missing.append("LLM_API_KEY")
    if missing:
        return _item(
            "LLM 凭据",
            ReadinessStatus.MISSING,
            "缺少 " + "、".join(missing)
            + "；境内服务优先（邮件内容不出境）。未配置时抽取降级为仅规则模式",
        )
    return _item(
        "LLM 凭据",
        ReadinessStatus.OK,
        f"{settings.llm_model} @ {settings.llm_base_url}"
        f" · 每轮上限 {settings.llm_max_calls_per_run} 次",
    )


def check_policy(settings: Settings) -> ReadinessItem:
    """把关键策略值回显出来，避免「以为改了其实没生效」。"""
    return _item(
        "入历策略",
        ReadinessStatus.OK,
        f"自动推送阈值 >{settings.confidence_auto_push_threshold}"
        f" · 每轮上限 {settings.auto_push_limit_per_run}"
        f" · 撤销窗口 {settings.auto_push_delay_minutes} 分钟"
        f" · 非联系人 ICS {'自动' if settings.ics_auto_push_non_contact else '待审'}",
    )


def check_proxy(settings: Settings) -> ReadinessItem:
    return _item("代理", ReadinessStatus.OK, settings.describe_proxies())


def live_checks(
    settings: Settings,
    *,
    imap_ready: bool,
    google_ready: bool,
    llm_ready: bool,
) -> list[ReadinessItem]:
    """联网检查。

    IMAP 已实现（P1）：真实连一次、发 ID、选中第一个文件夹并读取
    UIDVALIDITY/UIDNEXT，但不取任何正文，因此不会改变邮件的已读状态。

    Google 与 LLM 尚未接入（P4/P2），如实报 SKIPPED 而不是假装通过。
    """
    items: list[ReadinessItem] = [check_imap_live(settings, enabled=imap_ready)]

    for name, ready, hint in (
        ("Google 日历连通性", google_ready, "缺少 Google 凭据或未授权"),
        ("LLM 连通性", llm_ready, "缺少 LLM 凭据"),
    ):
        if not ready:
            items.append(_item(name, ReadinessStatus.SKIPPED, f"{hint}，跳过联网检查"))
        else:
            items.append(
                _item(name, ReadinessStatus.SKIPPED, "联网检查将在后续阶段接入")
            )
    return items


def check_imap_live(settings: Settings, *, enabled: bool) -> ReadinessItem:
    """真实连接 IMAP 并做一次只读检查。"""
    from .mail.backend import MailAuthError, MailError, UnsafeLoginError
    from .mail.imap_backend import ImapBackend

    if not enabled:
        return _item("IMAP 连通性", ReadinessStatus.SKIPPED, "缺少 163 凭据，跳过联网检查")

    folders = settings.imap_folder_list
    target = folders[0] if folders else "INBOX"

    try:
        with ImapBackend(settings) as backend:
            caps = backend.capabilities()
            status = backend.select_folder(target, readonly=True)
    except UnsafeLoginError as exc:
        return _item(
            "IMAP 连通性",
            ReadinessStatus.ERROR,
            f"被服务端拒绝（Unsafe Login）：{exc}",
        )
    except MailAuthError as exc:
        return _item("IMAP 连通性", ReadinessStatus.ERROR, str(exc), fatal=True)
    except MailError as exc:
        return _item("IMAP 连通性", ReadinessStatus.ERROR, f"连接失败：{exc}")

    # 163 上 ID 是硬要求，能力缺失说明可能连到了非预期服务端
    id_note = "支持 ID" if "ID" in caps else "[yellow]未声明 ID 能力[/yellow]"
    if status.uid_next is None:
        # 163 实测不返回 UIDNEXT（连显式 STATUS 请求都会被丢弃），
        # 同步改用客户端过滤兜底，因此这是**预期行为**而非故障。
        uid_note = "UIDNEXT 未提供（预期：同步改用客户端过滤）"
    else:
        uid_note = f"UIDNEXT={status.uid_next}"
    return _item(
        "IMAP 连通性",
        ReadinessStatus.OK,
        f"{target} · UIDVALIDITY={status.uid_validity} · {uid_note} · "
        f"共 {status.exists} 封 · {id_note}",
    )


# ──────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────

def run_checks(settings: Settings, *, live: bool = False) -> list[ReadinessItem]:
    """执行全部检查并返回结果列表（顺序即展示顺序）。"""
    items: list[ReadinessItem] = [check_env_file()]
    items.extend(check_dependencies())
    items.append(check_directories(settings))
    schema_item, runs_item = check_database(settings)
    items.append(schema_item)
    items.append(runs_item)
    items.append(check_timezone(settings))
    items.append(check_proxy(settings))
    items.append(check_policy(settings))

    imap = check_imap_config(settings)
    google = check_google_config(settings)
    llm = check_llm_config(settings)
    items.extend([imap, google, llm])

    if live:
        items.extend(
            live_checks(
                settings,
                imap_ready=imap.status is ReadinessStatus.OK,
                google_ready=google.status is ReadinessStatus.OK,
                llm_ready=llm.status is ReadinessStatus.OK,
            )
        )

    return items
