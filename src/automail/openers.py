"""打开外部资源：URL、目录、文件。

存在两个理由：

1. **集中一处处理失败**。打开浏览器/资源管理器在真实环境里会失败（没有默认
   浏览器、路径不存在、权限不足），分散在各面板里就会变成一堆未处理的异常。
   这里统一捕获并转成可读提示。
2. **明确区分"能做到"与"做不到"**。使用者点「打开邮件」时期待跳到那一封，
   但 163 的网页 URL 是会话式的，**做不到深链到单封邮件**。与其假装，
   不如只打开首页并在界面上提供「复制主题」——见 :func:`open_mail_home`。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

logger = logging.getLogger("automail.openers")

#: 163 网页版首页。
#:
#: 注意：**无法深链到具体邮件**。163 的 URL 形如 ``main.jsp?sid=...``，
#: sid 是会话态的，没有稳定的单封邮件地址。这是服务端的限制，不是实现偷懒。
MAIL_HOME_URL = "https://mail.163.com/"

#: Google 日历网页版（可按事件 id 深链，见 gcal_web_url）。
CALENDAR_HOME_URL = "https://calendar.google.com/calendar/u/0/r"


def open_url(url: str) -> bool:
    """用系统默认浏览器打开 URL，返回是否成功。

    ``webbrowser.open`` 在无默认浏览器时**返回 False 而不抛异常**，
    因此必须检查返回值——否则界面会显示"已打开"而实际什么都没发生。
    """
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception as exc:  # noqa: BLE001 - 打开失败不该影响界面
        logger.warning("打开链接失败 %s：%s", url, exc)
        return False


def open_path(path: Path) -> bool:
    """用系统默认程序打开文件或目录。"""
    target = str(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        if sys.platform == "darwin":
            subprocess.run(["open", target], check=False)
            return True
        subprocess.run(["xdg-open", target], check=False)
        return True
    except OSError as exc:
        logger.warning("打开 %s 失败：%s", target, exc)
        return False


def open_directory(path: Path) -> bool:
    """在文件管理器中打开目录（不存在时先创建，避免"打不开"的困惑）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("创建目录 %s 失败：%s", path, exc)
        return False
    return open_path(path)


def find_latest(directory: Path, *, prefix: str) -> Path | None:
    """找目录下按名字排序最新的一个文件（名字含日期，故按名排序即按时间）。"""
    try:
        candidates = sorted(
            (p for p in directory.iterdir() if p.is_file() and p.name.startswith(prefix)),
            key=lambda p: p.name,
        )
    except OSError:
        return None
    return candidates[-1] if candidates else None


def open_latest_in_dir(
    directory: Path, *, prefix: str, parent: Any = None, empty_hint: str = ""
) -> bool:
    """打开目录里最新的 ``prefix*`` 文件；没有就提示。

    用于「打开摘要」「打开审计报告」：这两个文件按日期命名，使用者想看的
    几乎总是最新那份。
    """
    latest = find_latest(directory, prefix=prefix)
    if latest is None:
        from tkinter import messagebox

        messagebox.showinfo(
            "还没有文件",
            empty_hint or f"{directory} 下还没有 {prefix}* 文件。\n先运行一次生成。",
            parent=parent,
        )
        return False
    return open_path(latest)


def gcal_web_url(event_id: str | None) -> str | None:
    """由 ``gcal_event_id`` 拼出 Google 日历的事件链接。

    用官方文档给出的 ``eid`` 形式：``base64(event_id + ' ' + calendar_id)``。
    这里只处理 ``primary``，因为本项目只写主日历。

    **拿不到就返回 ``None``**（退回打开日历首页），绝不编一个可能 404 的链接
    ——点了打不开比"只能到首页"更让人困惑。
    """
    if not event_id:
        return None
    import base64

    try:
        raw = f"{event_id} primary".encode()
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    except Exception:  # noqa: BLE001
        return None
    return f"https://calendar.google.com/calendar/u/0/r/eventedit/{encoded}"
