"""日历视图：今天与未来 7 天，并可跳到 Google 日历。

数据来源与摘要一致（``digest`` 的分组逻辑），这样界面与每日摘要不会给出
互相矛盾的说法。

**关于「打开某条事件」**：库里**没有**存 Google 的 ``htmlLink``——它在
规范化哈希的黑名单里（因为它对内容比对毫无意义，且会让每次读取都产生哈希
漂移）。因此这里只能：
* 有 ``gcal_event_id`` 时用 ``openers.gcal_web_url`` 拼事件链接（尽力而为）；
* 否则退回打开日历首页。
如实说明，不假装能精确跳转。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from ..viewmodels import format_event_when, source_label

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App


class CalendarPanel(ttk.Frame):
    """日历面板。"""

    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master)
        self.app = app
        self._events: list[dict[str, Any]] = []
        self._build()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Button(toolbar, text="刷新", command=self.refresh).pack(side="left")
        ttk.Button(toolbar, text="在浏览器中打开日历", command=self._open_calendar).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(toolbar, text="打开选中事件", command=self._open_selected).pack(
            side="left", padx=(6, 0)
        )

        self.summary = ttk.Label(self, text="", anchor="w")
        self.summary.pack(fill="x", padx=10, pady=(0, 6))

        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        columns = (
            ("when", "时间", 170),
            ("title", "标题", 400),
            ("source", "来源", 90),
            ("status", "状态", 120),
            ("location", "地点", 200),
        )
        self.tree = ttk.Treeview(
            table_frame, columns=[c[0] for c in columns], show="headings"
        )
        for key, heading, width in columns:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "title"))
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        self.hint = ttk.Label(
            self,
            text="提示：已写入日历的事件可直接跳到 Google 日历；"
            "若提示无法定位，说明该事件尚未写入或缺少日历 ID。",
            foreground="#555",
            anchor="w",
        )
        self.hint.pack(fill="x", padx=10, pady=(0, 10))

    # ── 数据 ──────────────────────────────────────────────

    def refresh(self) -> None:
        """从日历后端读取今天与未来 7 天。

        只读操作。日历后端在凭据缺失时会退回内存实现，那种情况下这里会显示
        空——因此状态栏要说明「未授权」而不是让人以为"日历是空的"。
        """
        status = self.app.state.config_status()
        if not status.google_ready:
            self.summary.configure(
                text=f"日历未授权：{status.google_detail or '请到「设置」完成授权'}"
            )
            self.tree.delete(*self.tree.get_children())
            self._events = []
            return

        try:
            events = self._fetch_events()
        except Exception as exc:  # noqa: BLE001 - 日历失败不该让面板崩
            self.summary.configure(text=f"读取日历失败：{exc}")
            return

        self._events = events
        self.tree.delete(*self.tree.get_children())
        for index, event in enumerate(events):
            payload = event.get("payload") or event
            start = _start_of(payload)
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    format_event_when(
                        start, all_day=_is_all_day(payload)
                    ),
                    _summary_of(payload),
                    source_label(str(payload.get("source") or "")),
                    str(payload.get("status") or ""),
                    str(payload.get("location") or ""),
                ),
            )
        self.summary.configure(text=f"今天与未来 7 天：{len(events)} 条")

    def _fetch_events(self) -> list[dict[str, Any]]:
        """用与摘要相同的窗口与分组读取日历。"""
        from ...cli import _build_calendar  # 复用既有的后端构造逻辑

        backend = _build_calendar(self.app.state.settings, None)
        raw = backend.list_events(max_results=250) or []
        return list(raw)

    # ── 动作 ──────────────────────────────────────────────

    def _open_calendar(self) -> None:
        from ... import openers

        openers.open_url(openers.CALENDAR_HOME_URL)

    def _open_selected(self) -> None:
        """打开选中事件。

        先用 ``gcal_event_id`` 拼链接；拼不出就退回日历首页，并明确告知，
        而不是打开一个 404 页面。
        """
        from ... import openers

        selection = self.tree.selection()
        if not selection:
            return
        event = self._events[int(selection[0])]
        payload = event.get("payload") or event
        event_id = payload.get("id") or payload.get("gcal_event_id")

        url = openers.gcal_web_url(str(event_id) if event_id else None)
        if url and openers.open_url(url):
            return
        openers.open_url(openers.CALENDAR_HOME_URL)
        self.app.status_var.set("无法定位到该事件，已打开日历首页")


def _start_of(payload: dict[str, Any]) -> str | None:
    start = payload.get("start") or {}
    if isinstance(start, dict):
        return start.get("dateTime") or start.get("date")
    return None


def _is_all_day(payload: dict[str, Any]) -> bool:
    start = payload.get("start") or {}
    return isinstance(start, dict) and bool(start.get("date")) and not start.get(
        "dateTime"
    )


def _summary_of(payload: dict[str, Any]) -> str:
    return str(payload.get("summary") or "(无标题)")
