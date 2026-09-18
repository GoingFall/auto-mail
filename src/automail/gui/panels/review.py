"""待审事件：批准、否决、忽略、修正、接管。

这是图形界面**最能超越命令行**的地方：审核队列在终端里是一张会被换行打散的
表格，很难扫；在这里可以用表格、多选、就地编辑。

两个关键语义（与后端一致，界面必须如实反映）：

* **批准不写日历。** ``approve()`` 只改状态，推送由 ``push`` 单独执行——
  这样即使推送时断网，人的判断也不会丢。因此界面要显示「已批准，等待写入
  日历」这个中间态，而不是假装已经写完。
* **冻结态要用「接管」而不是「批准」。** 被外部修改/冲突/日历中消失的事件，
  状态机不允许直接批准；给错按钮会让人反复点击却毫无反应。
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..viewmodels import (
    event_status_label,
    is_frozen,
    needs_attention,
    review_detail,
    review_row,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

COLUMNS = (
    ("id", "ID", 60),
    ("when", "时间", 150),
    ("title", "标题", 380),
    ("source", "来源", 80),
    ("confidence", "置信", 60),
    ("status", "状态", 170),
)


class ReviewPanel(ttk.Frame):
    """待审队列面板。"""

    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master)
        self.app = app
        self._items: list[Any] = []
        self._by_id: dict[int, Any] = {}
        self._build()

    # ── 布局 ──────────────────────────────────────────────

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(toolbar, text="状态：").pack(side="left")
        self.status_filter = ttk.Combobox(
            toolbar,
            state="readonly",
            width=14,
            values=("待审", "已批准", "需要关注", "全部"),
        )
        self.status_filter.current(0)
        self.status_filter.pack(side="left")
        self.status_filter.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        ttk.Label(toolbar, text="　天数：").pack(side="left")
        self.since_days = ttk.Combobox(
            toolbar, state="readonly", width=8, values=("全部", "1", "3", "7", "30")
        )
        self.since_days.current(0)
        self.since_days.pack(side="left")
        self.since_days.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        ttk.Button(toolbar, text="刷新", command=self.refresh).pack(
            side="left", padx=(8, 0)
        )

        # 动作按钮放在右侧：它们是"对选中项做什么"
        ttk.Button(toolbar, text="接管", command=self._on_adopt).pack(
            side="right", padx=(4, 0)
        )
        ttk.Button(toolbar, text="修正…", command=self._on_edit).pack(
            side="right", padx=(4, 0)
        )
        ttk.Button(toolbar, text="忽略", command=lambda: self._act("ignore")).pack(
            side="right", padx=(4, 0)
        )
        ttk.Button(toolbar, text="否决", command=lambda: self._act("reject")).pack(
            side="right", padx=(4, 0)
        )
        ttk.Button(toolbar, text="批准", command=lambda: self._act("approve")).pack(
            side="right", padx=(4, 0)
        )

        # ── 表格 ──
        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=10, pady=4)

        self.tree = ttk.Treeview(
            table_frame,
            columns=[c[0] for c in COLUMNS],
            show="headings",
            selectmode="extended",  # 多选：批量否决很常用
        )
        for key, heading, width in COLUMNS:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "title"))
        self.tree.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())

        # 需要关注的行用标签着色（比加一列状态文字更快被注意到）
        self.tree.tag_configure("attention", foreground="#b00020")
        self.tree.tag_configure("frozen", foreground="#8a5a00")

        # ── 详情 ──
        detail_box = ttk.LabelFrame(self, text="详情")
        detail_box.pack(fill="both", expand=False, padx=10, pady=(0, 10))
        self.detail = tk.Text(detail_box, height=7, wrap="word")
        self.detail.pack(fill="both", expand=True, padx=8, pady=8)
        self.detail.configure(state="disabled")

    # ── 数据 ──────────────────────────────────────────────

    def refresh(self) -> None:
        from ...models import EventStatus
        from ...review import ReviewQueue, annotate_probable_duplicates

        label = self.status_filter.get()
        statuses: tuple[EventStatus, ...]
        if label == "全部":
            statuses = tuple(EventStatus)
        elif label == "已批准":
            statuses = (EventStatus.APPROVED,)
        elif label == "需要关注":
            statuses = (
                EventStatus.EXTERNALLY_MODIFIED,
                EventStatus.CONFLICT,
                EventStatus.MISSING,
                EventStatus.PUSH_FAILED,
                EventStatus.UNCERTAIN,
            )
        else:
            statuses = (EventStatus.PENDING,)

        raw_days = self.since_days.get()
        since = None if raw_days == "全部" else int(raw_days)

        try:
            with self.app.state._connect() as conn:
                queue = ReviewQueue(conn)
                items = queue.list_items(statuses=statuses, limit=500, since_days=since)
                # 疑似重复只提示不合并（自动合并实测不安全）
                annotate_probable_duplicates(items)
        except Exception as exc:  # noqa: BLE001 - 数据库问题不该让面板空白
            self.app.status_var.set(f"读取待审队列失败：{exc}")
            items = []

        self._items = items
        self._by_id = {int(i.event_id): i for i in items}

        self.tree.delete(*self.tree.get_children())
        for item in items:
            status = str(getattr(item, "status", "") or "")
            tags: tuple[str, ...] = ()
            if is_frozen(status):
                tags = ("frozen",)
            elif needs_attention(status):
                tags = ("attention",)
            self.tree.insert(
                "",
                "end",
                iid=str(item.event_id),
                values=review_row(item),
                tags=tags,
            )

        self.app.status_var.set(f"待审队列：{len(items)} 条")

    def _selected_ids(self) -> list[int]:
        return [int(iid) for iid in self.tree.selection()]

    def _show_detail(self) -> None:
        selected = self._selected_ids()
        if not selected:
            self._set_detail("")
            return
        item = self._by_id.get(selected[0])
        self._set_detail(review_detail(item) if item else "")

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    # ── 动作 ──────────────────────────────────────────────

    def _act(self, action: str) -> None:
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("未选中", "请先选中要处理的事件。", parent=self)
            return

        labels = {"approve": "批准", "reject": "否决", "ignore": "忽略"}
        label = labels.get(action, action)

        # 批准冻结态是无效操作 —— 提前拦下并指引到「接管」，
        # 比让人点了没反应要好
        if action == "approve":
            frozen = [
                i for i in ids if is_frozen(str(self._by_id[i].status))
            ]
            if frozen:
                messagebox.showinfo(
                    "需要先接管",
                    f"#{frozen[0]} 等 {len(frozen)} 条处于冻结态（被外部修改或冲突），"
                    "不能直接批准。\n\n请用「接管」表示以我方内容为准。",
                    parent=self,
                )
                return

        if len(ids) > 1 and not messagebox.askyesno(
            f"批量{label}", f"确认{label} {len(ids)} 条事件？", parent=self
        ):
            return

        self._run_action(action, ids, label)

    def _on_adopt(self) -> None:
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("未选中", "请先选中要接管的事件。", parent=self)
            return
        self._run_action("adopt", ids, "接管")

    def _run_action(self, action: str, ids: list[int], label: str) -> None:
        """执行状态转移。

        只在**后台线程**做数据库写入，完成后由主窗口刷新表格——保持
        "界面线程不碰数据库"的一致规则（连接也不能跨线程）。
        """
        from ...review import ReviewQueue

        settings = self.app.state.settings
        state = self.app.state

        def job() -> str:
            with state._connect() as conn:
                queue = ReviewQueue(conn)
                method = getattr(queue, action)
                result = method(ids)
                conn.commit()
            return (
                f"{label}：成功 {result.changed}，跳过 {result.skipped}，"
                f"未找到 {result.not_found}"
            )

        _ = settings
        self.app.submit(label, job)

    def _on_edit(self) -> None:
        """修正标题与时间。

        时间输入沿用与流水线相同的解析路径（``parse_user_datetime``），
        不在面板里另写一份格式逻辑——两份校验迟早分叉，而这里改错会
        直接写进日历。
        """
        ids = self._selected_ids()
        if len(ids) != 1:
            messagebox.showinfo("请选一条", "修正一次只能选一条事件。", parent=self)
            return
        item = self._by_id.get(ids[0])
        if item is None:
            return

        title = simpledialog.askstring(
            "修正标题", "标题：", initialvalue=str(item.title), parent=self
        )
        if title is None:
            return
        title = title.strip()
        if not title:
            messagebox.showinfo("标题不能为空", "标题不能为空。", parent=self)
            return

        current = self._local_iso(item.start_ts)
        raw = simpledialog.askstring(
            "修正时间",
            "开始时间（YYYY-MM-DD HH:MM，留空表示不修改）：",
            initialvalue=current,
            parent=self,
        )
        if raw is None:
            return
        raw = raw.strip()

        start_iso: str | None = None
        if raw and raw != current:
            parsed = self._parse_datetime(raw)
            if parsed is None:
                messagebox.showerror(
                    "时间格式不对",
                    "请用 YYYY-MM-DD HH:MM，例如 2026-10-01 10:30。",
                    parent=self,
                )
                return
            start_iso = parsed

        self._submit_edit(ids[0], title, start_iso)

    def _local_iso(self, value: str | None) -> str:
        """把库里的 UTC 串转成本地 ``YYYY-MM-DD HH:MM`` 供编辑。"""
        from ..viewmodels import to_local

        local = to_local(value)
        return local.strftime("%Y-%m-%d %H:%M") if local else ""

    def _parse_datetime(self, text: str) -> str | None:
        """把本地时间文本解析为 UTC ISO 串；失败返回 ``None``。"""
        tz_name = self.app.state.settings.user_timezone
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                naive = datetime.strptime(text, fmt)
            except ValueError:
                continue
            try:
                local = naive.replace(tzinfo=ZoneInfo(tz_name))
            except Exception:  # noqa: BLE001 - 时区名非法时按 UTC 处理
                from datetime import UTC

                local = naive.replace(tzinfo=UTC)
            from datetime import UTC

            return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return None

    def _submit_edit(self, event_id: int, title: str, start_ts: str | None) -> None:
        from ...review import ReviewQueue

        state = self.app.state

        def job() -> str:
            with state._connect() as conn:
                result = ReviewQueue(conn).edit(
                    event_id, title=title, start_ts=start_ts
                )
                conn.commit()
            if result.not_found:
                return f"#{event_id} 不存在"
            # 编辑后置为已批准：人的修正本身就是决定
            return f"已修正 #{event_id}（记为人工修改，自动流程不会覆盖）"

        self.app.submit("修正事件", job)


def status_hint(status: str) -> str:
    """给状态栏用的一句话解释（界面上鼠标悬停等场景）。"""
    return event_status_label(status)
