"""应用状态：连接与刷新，**不含任何 Tk 代码**。

刻意把这一层与窗口分开，理由是可测性：``tkinter`` 需要显示会话，CI 与
headless 机器上跑不了。把数据与逻辑放在这里，就能在本机/CI 上用普通单测覆盖
"刷新拿到了什么、配置重载后哪些对象被重建"这类关键行为——而这些恰恰是最容易
出错的地方（例如 reload 之后忘了重建 Pipeline，界面仍用旧配置）。

窗口层只做两件事：把这里的数据画出来，把用户的动作转成这里的调用。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db import open_db
from ..settings import Settings, SettingsError, load_settings

logger = logging.getLogger("automail.gui.state")


@dataclass(slots=True)
class ConfigStatus:
    """配置完整性概览，供状态栏与设置页展示。"""

    imap_ready: bool = False
    imap_user: str = ""
    llm_ready: bool = False
    llm_model: str = ""
    google_ready: bool = False
    google_detail: str = ""
    secrets_error: str | None = None
    """加密凭据读取失败的原因（``None`` 表示正常）。

    **必须与"未配置"区分开**：换了 Windows 账号或从别的机器拷了 ``data/``
    时 DPAPI 解不开，如果只显示成"未配置"，使用者会反复重填却始终不生效，
    且无从知道真实原因。
    """

    env_file: Path | None = None

    @property
    def usable(self) -> bool:
        """是否具备最基本的可用条件（能同步邮件）。"""
        return self.imap_ready

    def summary(self) -> str:
        parts = []
        parts.append("邮箱已配置" if self.imap_ready else "邮箱未配置")
        parts.append("LLM 已配置" if self.llm_ready else "LLM 未配置")
        parts.append("日历已授权" if self.google_ready else "日历未授权")
        return " ｜ ".join(parts)


@dataclass(slots=True)
class MailRow:
    """邮件列表的一行（界面用不到原始 sqlite.Row，转成普通数据）。"""

    message_id: int
    uid: int
    subject: str
    from_addr: str
    from_name: str
    received_at: str | None
    extract_status: str
    is_unread: bool
    has_ics: bool
    event_count: int
    marked_read_at: str | None


def mail_filter_clause(key: str) -> str:
    """把界面的筛选名翻成 SQL 条件（不返回用户输入，避免注入面）。

    返回的是**固定字符串**，调用处不接受任意输入——界面上是下拉框，
    传进来的只能是这几个已知值。
    """
    clauses = {
        "all": "1=1",
        "unread": "(m.flags IS NULL OR m.flags NOT LIKE '%\\Seen%')",
        "with_events": "EXISTS (SELECT 1 FROM events e WHERE e.message_id = m.id)",
        "no_events": "NOT EXISTS (SELECT 1 FROM events e WHERE e.message_id = m.id)",
        "extract_failed": "m.extract_status = 'failed'",
        "pending_extract": "m.extract_status = 'pending'",
        "marked_by_us": "m.marked_read_at IS NOT NULL",
    }
    return clauses.get(key, clauses["all"])


FILTER_LABELS: dict[str, str] = {
    "all": "全部",
    "unread": "未读",
    "with_events": "有事件",
    "no_events": "无事件",
    "extract_failed": "抽取失败",
    "pending_extract": "待抽取",
    "marked_by_us": "本程序标过已读",
}


def select_mail_filter(key: str) -> str:
    """校验筛选键，未知值回退到 ``all``。"""
    return key if key in FILTER_LABELS else "all"


@dataclass(slots=True)
class AppState:
    """窗口需要的一切数据入口。

    生命周期的关键点：``settings`` 被 ``Pipeline`` 在**构造时绑定**
    （``pipeline.py`` 的 ``__init__``），因此改完配置必须
    :meth:`reload_settings` 重建依赖对象，否则界面显示"已保存"、
    实际仍用旧凭据。这一点有专门的测试钉住。
    """

    settings: Settings
    runs: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(cls, *, env_file: Path | None = None) -> AppState:
        """按当前环境加载配置。

        ``SettingsError``（配置值非法，例如时区拼错）在这里**不抛出**——
        否则窗口根本起不来，使用者连"去设置里改回来"的机会都没有。
        退回到默认配置，让界面能开、并在设置页报出错误。
        """
        try:
            settings = load_settings()
            if env_file is not None:
                settings = load_settings(_env_file=env_file)
        except SettingsError as exc:
            logger.warning("配置非法，退回默认值以便界面可启动：%s", exc)
            settings = load_settings(_env_file=None)
            state = cls(settings=settings)
            state.config_error = str(exc)
            return state

        return cls(settings=settings)

    #: 启动时配置非法的话记在这里（``None`` 表示正常）
    config_error: str | None = None

    # ── 刷新 ──────────────────────────────────────────────

    def reload_settings(self, *, env_file: Path | None = None) -> None:
        """重新加载配置。

        **必须重建依赖 settings 的对象**（Pipeline/日历/LLM 在构造时就绑定了
        settings 实例）：只换 ``self.settings`` 而让旧对象继续用旧配置，
        会造成"设置已保存但不生效"——最难查的一类问题。
        """
        overrides = {"_env_file": env_file} if env_file is not None else {}
        try:
            self.settings = load_settings(**overrides)
            self.config_error = None
        except SettingsError as exc:
            self.config_error = str(exc)
            raise

    def config_status(self) -> ConfigStatus:
        """汇总配置完整性与凭据状态。"""
        from ..secrets_store import last_error as secrets_error

        settings = self.settings
        status = ConfigStatus(
            imap_user=settings.imap_user,
            imap_ready=bool(settings.imap_user.strip() and settings.imap_auth_code_value),
            llm_model=settings.llm_model,
            llm_ready=bool(settings.llm_base_url.strip() and settings.llm_api_key_value),
            env_file=self.env_file(),
            secrets_error=secrets_error(),
        )

        # Google 授权状态：完全离线判断，不发起网络请求
        try:
            from ..calendar.auth import inspect_status

            info = inspect_status(settings)
            status.google_ready = bool(getattr(info, "ready", False))
            status.google_detail = str(getattr(info, "detail", ""))
        except Exception as exc:  # noqa: BLE001 - 状态查询失败不该影响界面
            status.google_detail = f"无法判断：{exc}"

        return status

    def env_file(self) -> Path | None:
        from ..settings import env_file_path

        return env_file_path()

    # ── 邮件 ──────────────────────────────────────────────

    def list_mail(
        self,
        *,
        filter_key: str = "all",
        limit: int = 300,
        search: str = "",
    ) -> list[MailRow]:
        """列出邮件（带事件计数与已读状态）。

        ``search`` 只在本地库里做 LIKE 匹配——不联网、不进邮箱。
        """
        clause = mail_filter_clause(select_mail_filter(filter_key))
        params: list[Any] = [self.settings.account]
        search_clause = ""
        if search.strip():
            search_clause = " AND (m.subject LIKE ? OR m.from_addr LIKE ?)"
            like = f"%{search.strip()}%"
            params.extend([like, like])
        params.append(int(limit))

        sql = f"""
            SELECT m.id, m.uid, m.subject, m.from_addr, m.from_name,
                   m.received_at, m.extract_status, m.flags, m.has_ics,
                   m.marked_read_at,
                   (SELECT COUNT(*) FROM events e WHERE e.message_id = m.id)
                       AS event_count
              FROM messages m
             WHERE m.account = ? AND m.is_canonical = 1 AND m.stale = 0
               AND {clause}{search_clause}
             ORDER BY m.received_at DESC, m.id DESC
             LIMIT ?
        """
        rows = self._query(sql, params)
        return [
            MailRow(
                message_id=int(r["id"]),
                uid=int(r["uid"]),
                subject=r["subject"] or "(无主题)",
                from_addr=r["from_addr"] or "",
                from_name=r["from_name"] or "",
                received_at=r["received_at"],
                extract_status=r["extract_status"],
                is_unread="\\seen" not in (r["flags"] or "").lower(),
                has_ics=bool(r["has_ics"]),
                event_count=int(r["event_count"] or 0),
                marked_read_at=r["marked_read_at"],
            )
            for r in rows
        ]

    def mail_detail(self, message_id: int) -> dict[str, Any] | None:
        """单封邮件的详情（含脱敏正文片段）。

        正文片段上限由 ``excerpt_max_chars`` 决定（默认 4000）——库里**只存
        这段片段**，全文从未落盘，因此界面能显示的就这么多。
        """
        rows = self._query("SELECT * FROM messages WHERE id = ?", [int(message_id)])
        if not rows:
            return None
        row = dict(rows[0])
        row["events"] = [
            dict(r)
            for r in self._query(
                "SELECT id, title, start_ts, end_ts, all_day, source, confidence, "
                "status, evidence FROM events WHERE message_id = ? ORDER BY start_ts",
                [int(message_id)],
            )
        ]
        return row

    def stats(self) -> Any:
        """只读统计快照。"""
        from ..stats import StatsCollector

        with self._connect() as conn:
            return StatsCollector(self.settings, conn).collect()

    def recent_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        from ..db import RunRepository

        with self._connect() as conn:
            records = RunRepository(conn).recent(limit=limit)
        return [
            {
                "id": r.id,
                "command": r.command,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "exit_code": r.exit_code,
                "error": r.error,
            }
            for r in records
        ]

    # ── 内部 ──────────────────────────────────────────────

    def _connect(self) -> Any:
        """为本任务建立独立连接（``open_db`` 的上下文管理器）。

        **不能跨线程复用连接**：sqlite 连接默认绑定创建它的线程，后台任务
        与主线程共用会抛 ``ProgrammingError``。每次操作自建、用完即关，
        代价可忽略（本地文件 + WAL）。
        """
        return open_db(self.settings)

    def _query(self, sql: str, params: list[Any]) -> list[sqlite3.Row]:
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                return list(conn.execute(sql, params).fetchall())
        except sqlite3.Error as exc:
            logger.warning("查询失败：%s", exc)
            return []
