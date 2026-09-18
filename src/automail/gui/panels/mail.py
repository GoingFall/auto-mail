"""邮件浏览：列表 + 详情（脱敏正文片段 + 抽出的事件 + 抽取依据）。

**能看到的内容有明确边界**：库里只存清洗后的**片段**（上限 ``excerpt_max_chars``，
默认 4000 字），全文从未落盘（只留哈希）。界面必须如实说明这一点——否则使用者
会以为"邮件内容丢了"，而那是刻意的隐私设计。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

from ..state import FILTER_LABELS, select_mail_filter
from ..viewmodels import (
    extract_status_label,
    format_time,
    mail_row,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

COLUMNS = (
    ("id", "ID", 55),
    ("received", "收到", 105),
    ("subject", "主题", 400),
    ("from", "发件人", 190),
    ("events", "事件", 50),
    ("status", "抽取状态", 100),
)


class MailPanel(ttk.Frame):
    """邮件列表面板。"""

    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master)
        self.app = app
        self._by_id: dict[int, Any] = {}
        self._build()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(toolbar, text="筛选：").pack(side="left")
        self.filter_box = ttk.Combobox(
            toolbar,
            state="readonly",
            width=16,
            values=[FILTER_LABELS[k] for k in FILTER_LABELS],
        )
        self.filter_box.current(0)
        self.filter_box.pack(side="left")
        self.filter_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        ttk.Label(toolbar, text="　搜索：").pack(side="left")
        self.search_var = self.app.register_var(tk.StringVar(master=self))
        entry = ttk.Entry(toolbar, textvariable=self.search_var, width=24)
        entry.pack(side="left")
        entry.bind("<Return>", lambda _e: self.refresh())

        ttk.Button(toolbar, text="搜索", command=self.refresh).pack(
            side="left", padx=(6, 0)
        )

        ttk.Button(toolbar, text="复制主题", command=self._copy_subject).pack(
            side="right"
        )

        # ── 列表 ──
        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=10, pady=4)

        self.tree = ttk.Treeview(
            table_frame, columns=[c[0] for c in COLUMNS], show="headings"
        )
        for key, heading, width in COLUMNS:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "subject"))
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())

        # ── 详情 ──
        detail_box = ttk.LabelFrame(self, text="详情")
        detail_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.detail_head = ttk.Label(detail_box, text="", anchor="w", justify="left")
        self.detail_head.pack(fill="x", padx=8, pady=(8, 0))

        panes = ttk.PanedWindow(detail_box, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8, pady=8)

        events_frame = ttk.LabelFrame(panes, text="抽出的事件")
        self.event_tree = ttk.Treeview(
            events_frame,
            columns=("id", "when", "title", "source", "confidence", "status"),
            show="headings",
            height=5,
        )
        for key, heading, width in (
            ("id", "ID", 55),
            ("when", "时间", 150),
            ("title", "标题", 300),
            ("source", "来源", 70),
            ("confidence", "置信", 55),
            ("status", "状态", 160),
        ):
            self.event_tree.heading(key, text=heading)
            self.event_tree.column(key, width=width, anchor="w")
        self.event_tree.pack(fill="both", expand=True, padx=6, pady=6)
        panes.add(events_frame, weight=1)

        body_frame = ttk.LabelFrame(panes, text="正文片段（清洗后，非全文）")
        self.body_text = tk.Text(body_frame, height=10, wrap="word")
        body_scroll = ttk.Scrollbar(
            body_frame, orient="vertical", command=self.body_text.yview
        )
        self.body_text.configure(yscrollcommand=body_scroll.set)
        self.body_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        body_scroll.pack(side="right", fill="y", padx=(0, 6), pady=6)
        self.body_text.configure(state="disabled")
        panes.add(body_frame, weight=2)

    # ── 数据 ──────────────────────────────────────────────

    def refresh(self) -> None:
        label = self.filter_box.get()
        key = next(
            (k for k, v in FILTER_LABELS.items() if v == label), "all"
        )
        key = select_mail_filter(key)

        rows = self.app.state.list_mail(
            filter_key=key, limit=500, search=self.search_var.get()
        )
        self._by_id = {r.message_id: r for r in rows}

        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert("", "end", iid=str(row.message_id), values=mail_row(row))

        self.app.status_var.set(f"邮件：{len(rows)} 封（{label}）")

    def _selected_id(self) -> int | None:
        selection = self.tree.selection()
        return int(selection[0]) if selection else None

    def _show_detail(self) -> None:
        message_id = self._selected_id()
        if message_id is None:
            return
        detail = self.app.state.mail_detail(message_id)
        if detail is None:
            return

        from ..viewmodels import event_row

        self.detail_head.configure(
            text=(
                f"{detail.get('subject') or '(无主题)'}\n"
                f"发件人：{detail.get('from_name') or ''} "
                f"<{detail.get('from_addr') or ''}>\n"
                f"收到：{format_time(detail.get('received_at'))}　"
                f"抽取：{extract_status_label(str(detail.get('extract_status') or ''))}"
            )
        )

        self.event_tree.delete(*self.event_tree.get_children())
        for event in detail.get("events", []):
            self.event_tree.insert("", "end", values=event_row(event))

        excerpt = detail.get("body_excerpt") or ""
        hint = ""
        if not excerpt:
            hint = "（无片段：该邮件未成功解析正文，或正文已被清理）"
        elif len(excerpt) >= 4000:
            # 达到上限时明确说明被截断，避免使用者以为邮件就这么短
            hint = "\n\n…（已达片段上限，后续内容未保存）"
        self._set_body(excerpt + hint if excerpt else hint)

    def _set_body(self, text: str) -> None:
        self.body_text.configure(state="normal")
        self.body_text.delete("1.0", "end")
        self.body_text.insert("1.0", text)
        self.body_text.configure(state="disabled")

    # ── 动作 ──────────────────────────────────────────────

    def _copy_subject(self) -> None:
        """复制主题到剪贴板。

        这是「在 163 里找到这封邮件」唯一可靠的办法：163 的网页 URL 是会话式的，
        无法深链到单封邮件（见 ``automail.openers``）。
        """
        message_id = self._selected_id()
        if message_id is None:
            return
        row = self._by_id.get(message_id)
        if row is None:
            return
        self.clipboard_clear()
        self.clipboard_append(row.subject)
        self.app.status_var.set("主题已复制，可粘贴到邮箱搜索框")
