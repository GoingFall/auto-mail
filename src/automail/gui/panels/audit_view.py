"""审计复盘面板：把「可能没抽对」的邮件挑出来给人看。

对应命令行的 ``automail audit``。这是**为迭代抽取质量而做的**：真实邮件里最
危险的缺陷不是"抽错了"，而是**"静默漏掉"**——预筛判定值得抽取、之后却没产出
任何候选，而流程照常报成功。

面板的重点因此是分类，而不是罗列：

* **很可能漏抽** —— 最该看的
* **值得留意** —— 有可疑迹象，但可能是正常情况
* **未判定** —— 本该由 LLM 兜底而本轮没调（**观测缺口，不是邮件的问题**）

第三类必须单独分出来：实测 72 小时窗口里 16 个可疑项有 15 个都是同一句
"未配置 LLM"，混在一起会把真正的信号淹掉，报告也就没人看了。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

#: 可疑度 → 界面标签。与 ``audit.py`` 的常量对应。
LEVEL_LABELS = {2: "很可能漏抽", 1: "值得留意", 0: "正常"}


class AuditPanel(ttk.Frame):
    """审计复盘面板。"""

    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master)
        self.app = app
        self._items: list[Any] = []
        self._build()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(toolbar, text="回看最近").pack(side="left")
        self.hours = ttk.Combobox(
            toolbar, state="readonly", width=6, values=("24", "48", "72", "168")
        )
        self.hours.current(0)
        self.hours.pack(side="left")
        ttk.Label(toolbar, text=" 小时").pack(side="left")

        self.with_llm = self.app.register_var(
            tk.BooleanVar(master=self, value=False)
        )
        ttk.Checkbutton(
            toolbar,
            text="调用 LLM 复查（更准，但消耗额度）",
            variable=self.with_llm,
        ).pack(side="left", padx=(10, 0))

        ttk.Button(toolbar, text="开始复盘", command=self._run).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="打开报告", command=self._open_report).pack(
            side="right"
        )

        self.summary = ttk.Label(self, text="尚未运行。", anchor="w")
        self.summary.pack(fill="x", padx=10, pady=(0, 6))

        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        columns = (
            ("level", "分类", 100),
            ("id", "ID", 55),
            ("subject", "主题", 340),
            ("reason", "原因", 380),
        )
        self.tree = ttk.Treeview(
            table_frame, columns=[c[0] for c in columns], show="headings"
        )
        for key, heading, width in columns:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, anchor="w", stretch=(key == "reason"))
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        self.tree.tag_configure("likely", foreground="#b00020")
        self.tree.tag_configure("maybe", foreground="#8a5a00")
        self.tree.tag_configure("unjudged", foreground="#666666")

    # ── 运行 ──────────────────────────────────────────────

    def _run(self) -> None:
        """跑复盘。

        默认**不调用 LLM**（复盘是给人看的，不是再花一次额度得到同样的答案）；
        勾选后才调用，此时「未判定」这一类才有结论。
        """
        from ...audit import run_audit

        state = self.app.state
        hours = int(self.hours.get())
        use_llm = bool(self.with_llm.get())

        def job() -> Any:
            llm = None
            if use_llm:
                from .settings_panel import _build_llm_from_settings

                llm = _build_llm_from_settings(state.settings)
            with state._connect() as conn:
                return run_audit(state.settings, conn, hours=hours, llm=llm)

        self.app.submit("复盘", job)

    def refresh(self) -> None:
        """本面板不自动刷新——复盘要主动触发（它可能调用 LLM，有成本）。"""

    # ── 展示 ──────────────────────────────────────────────

    def show_report(self, report: Any) -> None:
        """由主窗口在任务结束后调用。"""
        counts = report.counts()
        text = (
            f"邮件 {counts['messages']} 封　"
            f"很可能漏抽 {counts['likely_missed']}　"
            f"值得留意 {counts['maybe']}　"
            f"已抽出 {counts['with_candidates']}　"
            f"库中结果过时 {counts['stale']}"
        )
        if counts.get("unjudged"):
            text += f"　未判定 {counts['unjudged']}（需勾选 LLM 复查）"
        text += f"\n窗口：{report.window_start} → {report.window_end}"
        text += f"　LLM：{'已调用' if report.used_llm else '未调用'}"
        self.summary.configure(text=text)

        self.tree.delete(*self.tree.get_children())
        self._items = list(report.items)

        # 可疑项排在最前（报告是一眼扫的）
        ordered = list(report.suspect_items) + [
            i for i in report.items if not i.needs_eyes
        ]
        unjudged_ids = {id(i) for i in report.unjudged_items}

        for item in ordered:
            level = int(getattr(item, "suspicion_level", 0))
            if id(item) in unjudged_ids:
                bucket, tag = "未判定", "unjudged"
                reason = "需要 LLM 才能判断（本轮未调用）"
            else:
                bucket = LEVEL_LABELS.get(level, "正常")
                tag = {2: "likely", 1: "maybe"}.get(level, "")
                reason = str(getattr(item, "suspicion", "") or "")
            self.tree.insert(
                "",
                "end",
                values=(
                    bucket,
                    str(getattr(item, "message_id", "")),
                    str(getattr(item, "subject", "")),
                    reason,
                ),
                tags=(tag,) if tag else (),
            )

    def _open_report(self) -> None:
        from ... import openers

        openers.open_latest_in_dir(
            self.app.state.settings.out_dir,
            prefix="audit-",
            parent=self,
            empty_hint="还没有复盘报告。先点「开始复盘」。",
        )
