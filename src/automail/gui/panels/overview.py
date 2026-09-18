"""总览页：状态、快捷入口、立即同步、进度与日志。

这是打开窗口第一眼看到的东西，因此信息优先级很明确：
**我还需要做什么**（待审数量）> **系统是否正常**（配置/上次运行）> 其余。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Any

from ..viewmodels import format_relative

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App


class OverviewPanel(ttk.Frame):
    """总览面板。"""

    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master)
        self.app = app
        self._build()

    # ── 布局 ──────────────────────────────────────────────

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(12, 6))

        self.headline = ttk.Label(top, text="", font=("", 13, "bold"))
        self.headline.pack(anchor="w")

        self.config_line = ttk.Label(top, text="", foreground="#555")
        self.config_line.pack(anchor="w", pady=(4, 0))

        # ── 动作区 ──
        actions = ttk.LabelFrame(self, text="操作")
        actions.pack(fill="x", padx=12, pady=8)

        row = ttk.Frame(actions)
        row.pack(fill="x", padx=8, pady=8)

        self.sync_button = ttk.Button(
            row, text="立即同步", command=self._on_sync
        )
        self.sync_button.pack(side="left")

        ttk.Button(row, text="打开摘要", command=self._on_open_digest).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(row, text="打开审计报告", command=self._on_open_audit).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(row, text="刷新", command=self.refresh).pack(
            side="left", padx=(6, 0)
        )

        # 暂停开关：只影响计划任务，不影响上面的「立即同步」。
        # 标签写明作用域，避免使用者以为点了之后手动同步也会被挡。
        self.pause_var = self.app.register_var(
            tk.BooleanVar(master=self, value=False)
        )
        ttk.Checkbutton(
            row,
            text="暂停自动运行（不影响「立即同步」）",
            variable=self.pause_var,
            command=self._on_toggle_pause,
        ).pack(side="left", padx=(16, 0))

        # ── 快捷入口 ──
        links = ttk.LabelFrame(self, text="快捷入口")
        links.pack(fill="x", padx=12, pady=8)

        link_row = ttk.Frame(links)
        link_row.pack(fill="x", padx=8, pady=8)
        ttk.Button(link_row, text="163 邮箱网页版", command=self._on_open_mail).pack(
            side="left"
        )
        ttk.Button(
            link_row, text="Google 日历", command=self._on_open_calendar
        ).pack(side="left", padx=(6, 0))
        ttk.Button(link_row, text="数据目录", command=self._on_open_data).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(link_row, text="日志目录", command=self._on_open_logs).pack(
            side="left", padx=(6, 0)
        )

        # ── 统计 ──
        stats_box = ttk.LabelFrame(self, text="统计")
        stats_box.pack(fill="both", expand=True, padx=12, pady=8)

        self.stats_text = tk.Text(stats_box, height=8, wrap="none")
        self.stats_text.pack(fill="both", expand=True, padx=8, pady=8)
        self.stats_text.configure(state="disabled")

        # ── 日志 ──
        log_box = ttk.LabelFrame(self, text="最近日志")
        log_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.log_text = tk.Text(log_box, height=10, wrap="none")
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.log_text.configure(state="disabled")

    # ── 刷新 ──────────────────────────────────────────────

    def refresh(self) -> None:
        state = self.app.state

        # 暂停状态：反映真实文件状态，而不是只看控件（文件可能被手工删了）
        from ...pause import is_paused

        try:
            self.pause_var.set(is_paused(state.settings))
        except Exception:  # noqa: BLE001 - 读状态失败不该让总览空白
            pass

        # 待审数量是最重要的信息，放在最显眼处
        try:
            from ...review import ReviewQueue

            with state._connect() as conn:
                queue = ReviewQueue(conn)
                pending = queue.summary().get("pending", 0)
                attention = queue.needs_attention_count()
        except Exception:  # noqa: BLE001 - 统计失败不该让总览空白
            pending = attention = 0

        if pending or attention:
            parts = []
            if pending:
                parts.append(f"{pending} 条待审")
            if attention:
                parts.append(f"{attention} 条需要关注")
            self.headline.configure(text="需要你处理：" + "、".join(parts))
        else:
            self.headline.configure(text="没有待处理事项")

        status = state.config_status()
        self.config_line.configure(text=status.summary())
        if status.secrets_error:
            self.config_line.configure(
                text=f"{status.summary()}　⚠️ {status.secrets_error}"
            )

        self._render_stats()
        self._render_runs()
        self._render_log()

    def _render_stats(self) -> None:
        try:
            snapshot = self.app.state.stats()
        except Exception as exc:  # noqa: BLE001
            self._set_text(self.stats_text, f"统计不可用：{exc}")
            return

        lines = [
            f"邮件：{snapshot.messages_total} 封"
            f"　（含 ICS {snapshot.messages_with_ics}）",
            "抽取状态：" + "　".join(
                f"{k} {v}" for k, v in sorted(snapshot.messages_by_extract_status.items())
            )
            or "抽取状态：—",
            "事件：" + (
                "　".join(f"{k} {v}" for k, v in sorted(snapshot.events_by_status.items()))
                or "—"
            ),
            "来源：" + (
                "　".join(f"{k} {v}" for k, v in sorted(snapshot.events_by_source.items()))
                or "—"
            ),
            f"线程：{snapshot.threads_total}　运行记录：{snapshot.runs_total}"
            f"（失败 {snapshot.runs_failed}）",
        ]
        self._set_text(self.stats_text, "\n".join(lines))

    def _render_runs(self) -> None:
        try:
            runs = self.app.state.recent_runs(limit=5)
        except Exception:  # noqa: BLE001
            return
        if not runs:
            return
        lines = []
        for run in runs:
            mark = "OK" if run["exit_code"] == 0 else f"退出码 {run['exit_code']}"
            when = format_relative(run.get("started_at"))
            lines.append(f"  {when}　{run['command']}　{mark}")
        current = self.log_text.get("1.0", "end").strip()
        _ = current
        # 运行历史附在统计文本后面，避免再多一块控件
        existing = self.stats_text.get("1.0", "end").strip()
        self._set_text(
            self.stats_text, existing + "\n\n最近运行：\n" + "\n".join(lines)
        )

    def _render_log(self) -> None:
        lines = self.app._log_lines[-200:]
        self._set_text(self.log_text, "\n".join(lines))

    @staticmethod
    def _set_text(widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    # ── 动作 ──────────────────────────────────────────────

    def _on_toggle_pause(self) -> None:
        """切换「暂停自动运行」。

        只写标记文件，不碰计划任务本身——卸载了还得记得装回来，
        而使用者想要的往往只是"这几天先别自动跑"。
        """
        from ...pause import set_paused

        want = bool(self.pause_var.get())
        try:
            set_paused(self.app.state.settings, want)
        except OSError as exc:
            # 写失败要把开关拨回去，否则界面显示的状态与事实不符
            self.pause_var.set(not want)
            messagebox.showerror(
                "无法修改暂停状态",
                f"{exc}\n\n可以手工在 data 目录下创建或删除 AUTOMATED_RUN_PAUSED。",
                parent=self,
            )
            return

        self.app.status_var.set(
            "已暂停计划任务的自动运行" if want else "已恢复自动运行"
        )

    def _on_sync(self) -> None:
        """立即同步（后台执行，带真实进度）。

        走 ``Pipeline`` 意味着与 30 分钟的计划任务**共用同一把锁**——
        冲突时会明确报"已有运行在进行中"，而不是两份任务同时抓同一个邮箱。
        """
        from ...pipeline import Pipeline

        settings = self.app.state.settings
        reporter = self.app.worker.reporter()

        def job() -> Any:
            with self.app.state._connect() as conn:
                pipeline = Pipeline(settings, conn, reporter=reporter)
                return pipeline.run(apply=True)

        self.app.submit("同步", job)

    def _on_open_mail(self) -> None:
        """打开 163 网页版。

        **无法深链到具体邮件**：163 的网页 URL 是会话式的（``main.jsp?sid=...``），
        没有稳定的单封邮件链接。因此只打开首页；具体邮件请在界面上复制主题后搜索。
        """
        from ... import openers

        openers.open_url("https://mail.163.com/")

    def _on_open_calendar(self) -> None:
        from ... import openers

        openers.open_url("https://calendar.google.com/calendar/u/0/r")

    def _on_open_digest(self) -> None:
        from ... import openers

        openers.open_latest_in_dir(
            self.app.state.settings.out_dir, prefix="digest-", parent=self
        )

    def _on_open_audit(self) -> None:
        from ... import openers

        openers.open_latest_in_dir(
            self.app.state.settings.out_dir, prefix="audit-", parent=self
        )

    def _on_open_data(self) -> None:
        from ... import openers

        openers.open_directory(self.app.state.settings.data_dir)

    def _on_open_logs(self) -> None:
        from ... import openers

        openers.open_directory(self.app.state.settings.log_dir)
