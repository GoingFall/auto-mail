"""设置面板：凭据、邮箱、LLM、日历授权、计划任务。

**这是风险最高的面板**——它是本项目唯一会写入凭据与配置的地方，而错的配置
比没有配置更麻烦（连用法都跑不起来还查不出原因）。因此：

* 保存分两步走：**先写加密存储，再清 ``.env`` 明文**。中途崩溃的结果是
  "旧值继续生效"（一致但陈旧），而不是"两边都没有"。
* 保存后**重建依赖 settings 的对象**（``AppState.reload_settings``）。
  只换 settings 而让 ``Pipeline`` 继续用旧实例，会出现"设置显示已保存、
  实际不生效"——最难查的一类问题。
* 授权码一律掩码显示，且**从不回显明文**（点「显示」才临时展示）。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..app import App

#: 这些密码类字段存进加密存储，不留在 .env 明文里。
SECRET_FIELDS = ("IMAP_AUTH_CODE", "LLM_API_KEY")


class SettingsPanel(ttk.Frame):
    """设置面板。"""

    def __init__(self, master: tk.Misc, app: App) -> None:
        super().__init__(master)
        self.app = app
        self._secret_entries: dict[str, ttk.Entry] = {}
        self._secret_status: dict[str, ttk.Label] = {}
        self._shown: dict[str, bool] = {}
        self._build()

    # ── 布局 ──────────────────────────────────────────────

    def _build(self) -> None:
        canvas = tk.Canvas(self, highlightthickness=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.status_label = ttk.Label(inner, text="", anchor="w", justify="left")
        self.status_label.pack(fill="x", padx=12, pady=(12, 6))

        self._build_imap(inner)
        self._build_llm(inner)
        self._build_google(inner)
        self._build_policy(inner)
        self._build_tasks(inner)

        buttons = ttk.Frame(inner)
        buttons.pack(fill="x", padx=12, pady=12)
        ttk.Button(buttons, text="保存配置", command=self._on_save).pack(side="left")
        ttk.Button(buttons, text="重新加载", command=self.refresh).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="打开 .env", command=self._open_env).pack(
            side="left", padx=(6, 0)
        )

    def _section(self, parent: tk.Misc, title: str) -> ttk.Frame:
        box = ttk.LabelFrame(parent, text=title)
        box.pack(fill="x", padx=12, pady=6)
        return box

    def _row(self, parent: tk.Misc, label: str, *, secret: bool = False,
             key: str = "", width: int = 40) -> ttk.Entry:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=3)
        ttk.Label(row, text=label, width=22, anchor="w").pack(side="left")

        var = self.app.register_var(tk.StringVar(master=self))
        entry = ttk.Entry(row, textvariable=var, width=width)
        if secret:
            entry.configure(show="●")
        entry.pack(side="left", fill="x", expand=True)

        if secret and key:
            self._secret_entries[key] = entry
            self._shown[key] = False
            # 状态用**独立标签**显示，绝不写进输入框。
            #
            # 曾经把「已配置（留空则不修改）」当 placeholder 塞进 Entry，
            # 结果是**严重的数据损坏 bug**：使用者不点该字段、直接按「保存配置」
            # 时，placeholder 文本会被当成新密码写进去，把真实授权码覆盖掉。
            # tkinter 的 Entry 没有原生 placeholder，任何"塞文本再检测清掉"的
            # 做法都有这个风险——用独立标签就没有这个问题。
            status_label = ttk.Label(row, text="", width=14, foreground="#666")
            status_label.pack(side="left", padx=(6, 0))
            self._secret_status[key] = status_label

            ttk.Button(
                row, text="显示", width=6,
                command=lambda k=key: self._toggle_secret(k),
            ).pack(side="left", padx=(4, 0))

        entry.var = var  # type: ignore[attr-defined]  # 便于统一读写
        return entry

    def _build_imap(self, parent: tk.Misc) -> None:
        box = self._section(parent, "163 邮箱")
        self.imap_user = self._row(box, "邮箱账号", key="", width=40)
        self.imap_code = self._row(box, "授权码", secret=True, key="IMAP_AUTH_CODE")
        ttk.Label(
            box,
            text="授权码是 163 网页版「设置 → POP3/SMTP/IMAP」里生成的 16 位码，"
            "不是登录密码。",
            foreground="#555",
            wraplength=620,
            justify="left",
        ).pack(fill="x", padx=8, pady=(0, 6))

    def _build_llm(self, parent: tk.Misc) -> None:
        box = self._section(parent, "大模型（可选，境内服务优先）")
        self.llm_base_url = self._row(box, "接口地址", width=40)
        self.llm_api_key = self._row(box, "API Key", secret=True, key="LLM_API_KEY")
        self.llm_model = self._row(box, "模型名", width=40)
        ttk.Label(
            box,
            text="留空表示不用大模型：抽取会降级为「仅规则 + 日历邀请」，"
            "并在复盘里如实报告降级。",
            foreground="#555",
            wraplength=620,
            justify="left",
        ).pack(fill="x", padx=8, pady=(0, 6))

    def _build_google(self, parent: tk.Misc) -> None:
        box = self._section(parent, "Google 日历授权")
        self.google_label = ttk.Label(box, text="", anchor="w", justify="left")
        self.google_label.pack(fill="x", padx=8, pady=(4, 6))

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(row, text="开始授权…", command=self._on_auth).pack(side="left")
        ttk.Button(row, text="重新检查", command=self.refresh).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(row, text="撤销本地令牌", command=self._on_revoke).pack(
            side="left", padx=(6, 0)
        )

    def _build_policy(self, parent: tk.Misc) -> None:
        box = self._section(parent, "策略")
        self.policy_label = ttk.Label(box, text="", anchor="w", justify="left")
        self.policy_label.pack(fill="x", padx=8, pady=(4, 6))
        ttk.Button(box, text="打开 .env 调整策略", command=self._open_env).pack(
            anchor="w", padx=8, pady=(0, 8)
        )

    def _build_tasks(self, parent: tk.Misc) -> None:
        box = self._section(parent, "计划任务")
        ttk.Label(
            box,
            text="注册后会自动同步（默认每 30 分钟）并生成摘要。"
            "注册「登录时运行」需要管理员权限，普通用户会自动改用启动文件夹"
            "（效果相同，无需提权）。",
            foreground="#555",
            wraplength=620,
            justify="left",
        ).pack(fill="x", padx=8, pady=(4, 6))

        row = ttk.Frame(box)
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(row, text="注册计划任务", command=self._on_install_tasks).pack(
            side="left"
        )
        ttk.Button(row, text="查看当前状态", command=self._on_task_status).pack(
            side="left", padx=(6, 0)
        )

    # ── 刷新 ──────────────────────────────────────────────

    def refresh(self) -> None:
        settings = self.app.state.settings

        _set(self.imap_user, settings.imap_user)
        _set(self.llm_base_url, settings.llm_base_url)
        _set(self.llm_model, settings.llm_model)

        # 密码**不回显**（避免打开设置就把明文摊在屏幕上），也**不放进输入框**
        # ——输入框里的任何文本都会被当成新值保存。已配置状态用独立标签表示。
        for key, entry in self._secret_entries.items():
            has_value = bool(_current_secret(settings, key))
            _set(entry, "")  # 永远真正为空
            entry.configure(show="●" if not self._shown.get(key) else "")
            label = self._secret_status.get(key)
            if label is not None:
                label.configure(
                    text="✔ 已配置" if has_value else "未配置",
                    foreground="#2e7d32" if has_value else "#b00020",
                )

        status = self.app.state.config_status()
        lines = [status.summary()]
        if status.secrets_error:
            lines.append(
                f"⚠️ 加密凭据无法读取：{status.secrets_error}\n"
                "　 （换了 Windows 账号或换了机器会导致这种情况，重新填写即可）"
            )
        self.status_label.configure(text="\n".join(lines))

        if status.google_ready:
            self.google_label.configure(text=f"已授权：{status.google_detail}")
        else:
            self.google_label.configure(
                text=f"未就绪：{status.google_detail or '尚未授权'}"
            )

        self.policy_label.configure(
            text=(
                f"已读回写：{settings.mark_read_policy}"
                f"（off 关闭 / resolved 只在无待办时标 / processed 全部标）\n"
                f"自动入历阈值：{settings.confidence_auto_push_threshold}"
                f"　时区：{settings.user_timezone}\n"
                f"配置文件：{self.app.state.env_file()}"
            )
        )

    # ── 保存 ──────────────────────────────────────────────

    def _on_save(self) -> None:
        """保存配置。

        顺序（**重要**）：先把非敏感项写进 ``.env``，再把密码写进加密存储，
        最后清掉 ``.env`` 里的明文密码。因为加密存储的优先级**低于** ``.env``，
        若在"写加密"与"清明文"之间崩溃，结果是**旧值继续生效**——一致但陈旧，
        下次保存自愈；反之若先清明文再写加密，崩溃就会两边都没有。
        """
        values: dict[str, str] = {}
        secrets: dict[str, str] = {}

        text = _get(self.imap_user)
        if text:
            values["IMAP_USER"] = text
        for key, entry in self._secret_entries.items():
            typed = _get(entry)
            if typed:  # 留空表示不修改
                secrets[key] = typed

        for key, entry, target in (
            ("LLM_BASE_URL", self.llm_base_url, "llm_base_url"),
            ("LLM_MODEL", self.llm_model, "llm_model"),
        ):
            text = _get(entry)
            if text:
                values[key] = text
            _ = target

        if not values and not secrets:
            messagebox.showinfo("没有改动", "没有需要保存的改动。", parent=self)
            return

        self._do_save(values, secrets)

    def _do_save(self, values: dict[str, str], secrets: dict[str, str]) -> None:
        from ... import envfile
        from ...secrets_store import SecretsStoreError, default_secrets_path, save
        from ...settings import env_file_path

        state = self.app.state
        settings = state.settings
        env_path = env_file_path()
        secrets_path = default_secrets_path(settings.data_dir)

        # ① 非敏感项 → .env（保留注释与未知键）
        try:
            if values:
                envfile.update(
                    env_path,
                    values,
                    backup_dir=settings.backup_dir,
                    backup_keep=settings.db_backup_keep,
                )
        except envfile.EnvFileError as exc:
            messagebox.showerror("保存失败", f"写入 .env 失败：\n{exc}", parent=self)
            return

        # ② 密码 → 加密存储
        if secrets:
            try:
                existing = {}
                from ...secrets_store import load

                loaded = load(secrets_path)
                if loaded.ok:
                    existing = loaded.values
                existing.update(secrets)
                save(secrets_path, existing)
            except SecretsStoreError as exc:
                messagebox.showerror(
                    "加密保存失败",
                    f"{exc}\n\n.env 中的原有值保持不变，凭据未丢失。",
                    parent=self,
                )
                return

            # ③ 确认加密可读后，清掉 .env 明文
            from ...secrets_store import load as reload_secrets

            check = reload_secrets(secrets_path)
            if not check.ok or any(
                check.values.get(k) != v for k, v in secrets.items()
            ):
                messagebox.showwarning(
                    "未清除明文",
                    "凭据已加密保存，但回读校验未通过，因此 .env 中的明文"
                    "**保持原样**（程序仍可用）。请稍后重试。",
                    parent=self,
                )
            else:
                try:
                    envfile.blank_out(
                        env_path,
                        list(secrets),
                        note="已改用 Windows 加密存储，可在图形界面的设置里修改",
                        backup_dir=settings.backup_dir,
                        backup_keep=settings.db_backup_keep,
                    )
                except envfile.EnvFileError:
                    # 明文没清掉不影响可用性，下次保存再试
                    pass

        # ④ 重载配置并重建依赖对象
        try:
            state.reload_settings()
        except Exception as exc:  # noqa: BLE001 - 配置值非法时如实报错
            messagebox.showerror(
                "配置已保存但有误",
                f"{exc}\n\n请修正后重试；程序当前仍按旧配置运行。",
                parent=self,
            )
            return

        self.refresh()
        self.app._refresh_status()
        messagebox.showinfo("已保存", "配置已保存并生效。", parent=self)

    # ── 其他动作 ──────────────────────────────────────────

    def _toggle_secret(self, key: str) -> None:
        self._shown[key] = not self._shown.get(key, False)
        entry = self._secret_entries[key]
        entry.configure(show="" if self._shown[key] else "●")

    def _open_env(self) -> None:
        from ... import openers

        env_path = self.app.state.env_file()
        if env_path is None or not env_path.is_file():
            messagebox.showinfo(
                "还没有 .env",
                "先保存一次配置，或从 .env.example 复制一份。",
                parent=self,
            )
            return
        openers.open_path(env_path)

    def _on_auth(self) -> None:
        """跑 Google OAuth 授权流程。

        必须在后台线程：它会打开浏览器并**等待使用者点完授权**，可能几分钟。
        在主线程里跑会直接把窗口冻住。
        """
        from ...calendar.auth import run_authorization

        settings = self.app.state.settings

        def job() -> str:
            run_authorization(settings)
            return "Google 授权完成"

        self.app.submit("Google 授权", job)

    def _on_revoke(self) -> None:
        if not messagebox.askyesno(
            "撤销令牌",
            "这会删除本地 token.json，需要重新授权才能写入日历。\n"
            "（不会影响你在 Google 账号里的授权记录）\n\n继续？",
            parent=self,
        ):
            return
        from ...calendar.auth import revoke_local_token

        try:
            removed = revoke_local_token(self.app.state.settings)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("撤销失败", str(exc), parent=self)
            return
        self.refresh()
        messagebox.showinfo(
            "已撤销", "本地令牌已删除。" if removed else "本地本来就没有令牌。",
            parent=self,
        )

    def _on_install_tasks(self) -> None:
        self._run_task_script(remove=False)

    def _on_task_status(self) -> None:
        names = (
            "auto-mail startup",
            "auto-mail run",
            "auto-mail digest",
            "auto-mail audit",
            "auto-mail backup",
        )
        lines: list[str] = []
        for name in names:
            try:
                import subprocess

                result = subprocess.run(
                    ["schtasks", "/query", "/tn", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                lines.append(f"{'已注册' if result.returncode == 0 else '未注册'}　{name}")
            except OSError as exc:
                lines.append(f"无法查询　{name}（{exc}）")
        messagebox.showinfo("计划任务状态", "\n".join(lines), parent=self)

    def _run_task_script(self, *, remove: bool) -> None:
        """调用项目里的安装脚本（不自己拼 schtasks 参数）。

        复用脚本而不是重写一份：脚本里已经处理了「ONLOGON 需要管理员权限」
        与「非提权时回退到启动文件夹」这些实测出来的分支。
        """
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[4]
        script = root / "scripts" / "install-tasks-exe.ps1"
        if not script.is_file():
            script = root / "scripts" / "install-tasks.ps1"
        if not script.is_file():
            messagebox.showinfo(
                "未找到脚本",
                "计划任务脚本不在预期位置；便携版请手动运行 "
                "scripts\\install-tasks-exe.ps1。",
                parent=self,
            )
            return

        args = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
        if remove:
            args.append("-Remove")

        def job() -> str:
            result = subprocess.run(
                args, capture_output=True, text=True, check=False
            )
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                raise RuntimeError(output.strip() or f"退出码 {result.returncode}")
            return output.strip() or "完成"

        _ = sys
        self.app.submit("注册计划任务", job)


# ── 小工具 ────────────────────────────────────────────────────


def _get(entry: ttk.Entry) -> str:
    return entry.get().strip()


def _set(entry: ttk.Entry, value: str) -> None:
    entry.delete(0, "end")
    if value:
        entry.insert(0, value)


def _current_secret(settings: Any, key: str) -> str:
    """读取当前密码值（只用于判断是否已配置，绝不用来回显）。"""
    if key == "IMAP_AUTH_CODE":
        return settings.imap_auth_code_value
    if key == "LLM_API_KEY":
        return settings.llm_api_key_value
    return ""


def _build_llm_from_settings(settings: Any) -> Any:
    """按配置构造 LLM 提取器；未配置时返回 ``None``（降级为仅规则）。

    复用 CLI 的构造逻辑，避免两处参数不一致（那会让"界面里能用、
    复盘里不能用"这类问题很难查）。
    """
    from ...cli import _build_llm

    try:
        return _build_llm(settings)
    except Exception:  # noqa: BLE001 - 构造失败等同"不可用"，由调用方降级
        return None
