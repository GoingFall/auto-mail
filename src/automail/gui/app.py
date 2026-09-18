"""主窗口：标签页、状态栏、后台任务与关闭语义。

**本模块是唯一碰 Tk 的地方**（连同 ``panels/``）。所有能在无显示环境下测的逻辑
都在 ``state`` / ``worker`` / ``viewmodels`` 里。

三条实现约束（都有具体理由，不是风格偏好）：

1. **后台线程只往队列里放事件，主线程用 ``after()`` 取。** Tk 不是线程安全的，
   从工作线程直接改控件会随机崩溃。
2. **关窗时若任务在跑，提供"完成后退出 / 继续在后台"，不提供强杀。**
   运行锁在 ``finally`` 里释放；强杀会让锁滞留到 TTL（默认 30 分钟），
   期间一切运行（含计划任务）都报"已有运行在进行中"。
3. **配置保存后重建依赖对象**（``AppState.reload_settings``）：``Pipeline``
   在构造时就绑定了 settings，不重建会出现"设置已保存但不生效"。
"""

from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

from .state import AppState
from .worker import LogRecord, ProgressEvent, TaskResult, Worker

logger = logging.getLogger("automail.gui.app")

APP_TITLE = "auto-mail · 邮箱助手"


class GuiLogHandler(logging.Handler):
    """把日志送进界面的队列（只读日志页 + 状态栏尾部）。

    **只往队列里放，绝不碰控件**——日志可能从任何线程产生，包括工作线程。
    """

    def __init__(self, worker: Worker) -> None:
        super().__init__()
        self._worker = worker

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001 - 格式化失败不能影响日志本身
            return
        self._worker.events.put(LogRecord(level=record.levelno, text=text))


class App:
    """主窗口。"""

    #: 主线程轮询队列的间隔（毫秒）。
    #: 100ms 足够让进度看起来是实时的，又不会白烧 CPU。
    POLL_MS = 100

    def __init__(self, state: AppState | None = None) -> None:
        self.state = state or AppState.create()
        self.worker = Worker()

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.minsize(900, 560)

        self._closing = False
        self._exit_when_done = False
        self._log_lines: list[str] = []
        #: 本窗口创建的全部 Tk 变量，销毁前需统一释放（见 _release_tk_vars）
        self._tk_vars: list[tk.Variable] = []

        self._build_widgets()
        # 滚轮必须在控件建好之后装：它要清掉内建类绑定并接管全部滚动。
        # 放在这里而不是每个面板各装一次——Tk 的 boundtags 不含祖先，
        # 滚轮事件不会冒泡，只有在 all 级别统一拦截才能正确路由。
        self._install_wheel_support()
        self._restore_geometry()
        self._install_log_bridge()
        self._install_close_handler()

        self.worker.start()
        self._poll()
        self._refresh_status()

        # 启动时刷新**全部**面板，而不是只刷当前可见的那个。
        #
        # 这里踩过一个坑：原先只刷当前页，且启动时一次都没刷 → 打开窗口后
        # 每一页都是空的（设置页显示"未配置"、邮件页一篇空白），要手动点
        # 「刷新」才显示。使用者的第一印象就是"我的配置没被读到"。
        self.refresh_all(force=True)

    def _install_wheel_support(self) -> None:
        """给窗口装上鼠标滚轮支持（含 Canvas 与嵌套控件）。

        Tkinter 的默认支持不完整：Canvas 完全没有滚轮绑定，而设置页正是
        用一个 Canvas 承载整页内容；更要紧的是 bindtags 不含祖先，指针停在
        输入框上时事件不会冒泡到外层 Canvas。这两个缺口叠加的结果是
        **设置页整体不响应滚轮**。
        """
        from .scrolling import install_wheel_support

        self.wheel = install_wheel_support(self.root)

    # ── 构建 ──────────────────────────────────────────────

    def _build_widgets(self) -> None:
        style = ttk.Style(self.root)
        # 默认主题在 Windows 上可用；失败不致命（自检也会覆盖这一路径）
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except tk.TclError:
            pass

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)
        # 切换标签页就刷新那一页，避免看到过期数据
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # 面板在需要时才导入：tkinter 缺失的环境下 gui_main 已提前退出，
        # 但这也让"只测 state/worker"不必加载面板代码
        from .panels import (
            audit_view,
            calendar_view,
            mail,
            overview,
            review,
            settings_panel,
        )

        self.panels: dict[str, Any] = {}
        for key, title, factory in (
            ("overview", "总览", overview.OverviewPanel),
            ("review", "待审事件", review.ReviewPanel),
            ("mail", "邮件", mail.MailPanel),
            ("calendar", "日历", calendar_view.CalendarPanel),
            ("audit", "复盘", audit_view.AuditPanel),
            ("settings", "设置", settings_panel.SettingsPanel),
        ):
            panel = factory(self.notebook, self)
            self.notebook.add(panel, text=title)
            self.panels[key] = panel

        self._build_statusbar()

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", side="bottom")

        # 显式传 master：不传会挂到 _default_root，窗口销毁后 __del__ 会报错
        self.status_var = self.register_var(
            tk.StringVar(master=self.root, value="就绪")
        )
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(
            side="left", padx=8, pady=3
        )

        self.progress = ttk.Progressbar(bar, mode="determinate", length=160)
        self.progress.pack(side="right", padx=8, pady=3)

    def _install_log_bridge(self) -> None:
        """把根日志器的输出接一份到界面。

        只加 handler，不改级别、不改其它 handler——命令行与文件日志保持原样。
        """
        handler = GuiLogHandler(self.worker)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger("automail").addHandler(handler)
        self._log_handler = handler

    def _install_close_handler(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ── 几何持久化 ────────────────────────────────────────

    def _geometry_file(self) -> Path:
        return self.state.settings.data_dir / "gui_state.json"

    def _restore_geometry(self) -> None:
        """恢复上次的窗口大小、位置与选中标签页。

        成本极低但体验差别明显：每次打开都回到习惯的位置，而不是默认小窗。
        """
        import json

        try:
            raw = self._geometry_file().read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            self.root.geometry("1100x700")
            return

        geometry = data.get("geometry")
        if isinstance(geometry, str) and "x" in geometry:
            try:
                self.root.geometry(geometry)
            except tk.TclError:
                self.root.geometry("1100x700")
        else:
            self.root.geometry("1100x700")

        tab = data.get("tab")
        if isinstance(tab, int) and 0 <= tab < len(self.notebook.tabs()):
            self.notebook.select(tab)

    def _save_geometry(self) -> None:
        import json

        try:
            self._geometry_file().parent.mkdir(parents=True, exist_ok=True)
            self._geometry_file().write_text(
                json.dumps(
                    {
                        "geometry": self.root.geometry(),
                        "tab": self.notebook.index(self.notebook.select()),
                    }
                ),
                encoding="utf-8",
            )
        except (OSError, tk.TclError, ValueError):
            # 存不上无关紧要，绝不能让关闭流程失败
            pass

    # ── 事件循环 ──────────────────────────────────────────

    def _poll(self) -> None:
        """主线程消费队列：**这是后台与界面之间唯一的通道**。"""
        for event in self.worker.drain():
            self._handle_event(event)
        if not self._closing:
            self.root.after(self.POLL_MS, self._poll)

    def _handle_event(self, event: Any) -> None:
        if isinstance(event, TaskResult):
            self._on_task_done(event)
        elif isinstance(event, ProgressEvent):
            from .viewmodels import progress_ratio, progress_text

            self.status_var.set(progress_text(event.stage, event.index, event.total, kind=event.kind))
            self.progress["value"] = progress_ratio(
                event.stage, event.index, event.total, kind=event.kind
            ) * 100
        elif isinstance(event, LogRecord):
            self._on_log(event)

    def _on_task_done(self, result: TaskResult) -> None:
        self.progress["value"] = 0
        if result.ok:
            self.status_var.set(f"{result.name} 完成")
            self._route_result(result)
        else:
            self.status_var.set(f"{result.name} 失败")
            # 日志在**主线程**记录，而不是工作线程——后台线程调用 logging 会
            # 与主线程 GC 竞争 CPython 的 ABC 缓存并崩溃解释器（见 worker._loop）。
            logger.error(
                "后台任务失败：%s\n%s", result.name, result.detail or result.error
            )
            messagebox.showerror(
                "任务失败",
                f"{result.name} 失败：\n\n{result.error}",
                parent=self.root,
            )
        # 任何任务结束后都刷新一遍：同步会改变邮件、抽取会改变待审项
        self.refresh_all()

        if self._exit_when_done and not self.worker.busy:
            self._do_close()

    def _route_result(self, result: TaskResult) -> None:
        """把任务返回值交给对应的面板。

        有些结果本身就是"要看的东西"（例如复盘报告），不交给面板就会丢掉——
        重跑一次复盘可能又要花 LLM 额度。
        """
        report = result.value
        if result.name == "复盘" and hasattr(report, "counts"):
            panel = self.panels.get("audit")
            show = getattr(panel, "show_report", None)
            if callable(show):
                show(report)
                self.notebook.select(self.panels["audit"])

    def _on_log(self, record: LogRecord) -> None:
        self._log_lines.append(record.text)
        del self._log_lines[:-500]  # 只留最近 500 行，避免无限增长
        # 错误顺手反映到状态栏：使用者常盯着状态栏，而错误最容易出现在那里
        if record.level >= logging.ERROR:
            self.status_var.set(record.text[:120])

    # ── 任务提交 ──────────────────────────────────────────

    def submit(self, name: str, fn: Any, *, refresh: bool = True) -> bool:
        """提交后台任务；忙碌时提示并返回 ``False``。

        忙碌时**不排队**：排队会让使用者连点几下攒出一串任务，
        而且进度显示会来回跳。
        """
        if not self.worker.submit(name, fn):
            messagebox.showinfo(
                "正在运行",
                f"「{self.worker.current_task}」正在运行，请等它结束。",
                parent=self.root,
            )
            return False
        self.status_var.set(f"{name}…")
        _ = refresh
        return True

    def refresh_all(self, *, force: bool = False, only: str | None = None) -> None:
        """刷新面板。

        默认只刷**当前可见**的那一个：隐藏标签页的刷新在任务结束后是浪费
        （还会拖慢响应），而且切换过去时 :meth:`_on_tab_changed` 会补刷。

        ``force=True`` 时刷全部——启动时必须如此（否则每页都空白），
        ``only=<名字>`` 用于定向刷新。
        """
        if only is not None:
            targets = [only] if only in self.panels else []
        elif force:
            targets = list(self.panels)
        else:
            current = self._current_panel_name()
            targets = [current] if current else []

        for name in targets:
            panel = self.panels.get(name)
            refresh = getattr(panel, "refresh", None)
            if not callable(refresh):
                continue
            try:
                refresh()
            except Exception:  # noqa: BLE001 - 单个面板出错不该拖垮界面
                logger.exception("面板 %s 刷新失败", name)

    def _on_tab_changed(self, _event: tk.Event | None = None) -> None:
        """切换标签页时刷新那一页。

        不刷的话，切到另一页看到的可能是几分钟前的旧数据（或从未刷过的空白），
        而使用者会以为那是当前状态。
        """
        self.refresh_all(only=self._current_panel_name())

    def _current_panel_name(self) -> str:
        try:
            index = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return ""
        tabs = list(self.panels)
        return tabs[index] if 0 <= index < len(tabs) else ""

    def select_panel(self, name: str) -> None:
        """切到指定面板。用名字而不是数字下标。

        下标会随标签页增删而失效——本项目就发生过：插入「复盘」页之后，
        原先写死的 ``select(3)`` 从「设置」变成了别的页。

        切换后会刷新该页（``<<NotebookTabChanged>>`` 已经负责，这里不重复调，
        否则一次切换刷两遍）。
        """
        tabs = list(self.panels)
        if name in tabs:
            self.notebook.select(tabs.index(name))

    def _refresh_status(self) -> None:
        status = self.state.config_status()
        self.status_var.set(status.summary())
        if not status.usable:
            # 没配置就用不了，直接把人带到设置页——比让他自己找更友好
            self.select_panel("settings")
            if status.secrets_error:
                self.status_var.set(f"{status.summary()}　⚠️ {status.secrets_error}")

    # ── 关闭 ──────────────────────────────────────────────

    def on_close(self) -> None:
        """关窗处理。

        任务在跑时给出**两个真实可选**的选项，而不是"确定要退出吗"这种
        点了就强杀的假确认——强杀会留下未释放的运行锁（见模块文档）。
        """
        if not self.worker.busy:
            self._do_close()
            return

        current = self.worker.current_task or "任务"
        choice = messagebox.askyesnocancel(
            "正在运行",
            f"「{current}」正在运行。\n\n"
            "是：等它完成后自动退出\n"
            "否：继续在后台运行（保持窗口）\n"
            "取消：什么都不做\n\n"
            "提示：强制结束会让运行锁滞留到超时（默认 30 分钟），"
            "期间计划任务都会报「已有运行在进行中」。",
            parent=self.root,
        )
        if choice is True:
            self._exit_when_done = True
            self.status_var.set(f"等待「{current}」完成后退出…")
        # False（继续在后台）与 None（取消）都什么都不做 —— 窗口保持打开

    def _do_close(self) -> None:
        self._closing = True
        self._save_geometry()
        try:
            logging.getLogger("automail").removeHandler(self._log_handler)
        except Exception:  # noqa: BLE001
            pass
        # 非守护 worker：空闲时正常退出；仍在跑则不强行结束
        self.worker.shutdown()
        self._release_tk_vars()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def register_var(self, var: tk.Variable) -> tk.Variable:
        """登记一个 Tk 变量，供关窗时统一释放。

        面板创建变量时都应走这里（``self.app.register_var(...)``），否则
        根窗口销毁后它们仍会尝试访问 Tcl，在 GC 时抛
        ``RuntimeError: main thread is not in main loop``。
        """
        self._tk_vars.append(var)
        return var

    def _release_tk_vars(self) -> None:
        """在销毁根窗口**之前**释放 Tk 变量。

        为什么必须显式做：``tkinter.Variable.__del__`` 会调用 Tcl 去 unset 变量，
        而它只在 ``_tk is None`` 时才跳过。根窗口销毁后这些变量往往还被引用着
        （被控件、被 panel 属性），等到 GC 时才回收——那时解释器可能已经不在
        主循环里，``__del__`` 就抛 ``RuntimeError: main thread is not in main loop``，
        表现为退出时的噪音报错（实测在测试里以 PytestUnraisableExceptionWarning
        出现，也说明真实程序退出时可能会有同类报错）。

        这里把 ``_tk`` 置空（tkinter 认可的安全出口），让后续 GC 直接跳过。
        """
        for var in self._tk_vars:
            try:
                var._tk = None  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
        self._tk_vars.clear()

    # ── 运行 ──────────────────────────────────────────────

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_app(state: AppState | None = None) -> int:
    """图形界面入口（供 ``gui_main`` 调用）。"""
    app = App(state)
    return app.run()
