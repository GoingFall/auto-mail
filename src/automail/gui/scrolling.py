"""鼠标滚轮支持。

**为什么需要单独一个模块**：Tkinter 对滚轮的默认支持是**不完整**的，而且
缺口位置很反直觉。实测（Tk 8.6.15）：

* ``Treeview`` / ``Text`` / ``Listbox`` —— 有内建的类绑定，滚轮**能**用
  （但每格只滚 1 行，比平台惯例的 3 行慢）
* ``Canvas`` —— **完全没有**绑定。而设置页正是用一个 Canvas 承载整页内容，
  于是那个页面完全不响应滚轮

更关键的是 Tk 的一个结构性限制：**bindtags 不包含祖先控件**。滚轮事件只会
投递给指针下的那个控件及其*类*，不会冒泡到父容器。所以指针停在设置页的输入框
上时（而输入框占据了那个页面的大部分面积），事件目标是 ``TEntry``——它和它的
类都没有滚轮绑定，事件就此消失，**外层 Canvas 根本收不到**。

因此正确的做法不是"给每个控件加绑定"，而是**在 all 级别统一拦截，再沿着
祖先链找到真正该滚的东西**。本模块就是这件事。

处理方式与取舍：

1. 清掉 Treeview/Text/Listbox 的类级滚轮绑定，改由本模块统一滚动。
   这样每格滚 3 行（贴近 Windows 惯例），且不会与内建绑定**叠加成 6 行**。
2. Canvas 每格 1 个单位 —— 实测 Canvas 的 1 个单位 = 视口高度的 1/10
   （约 60px），恰好接近"3 行"的手感。
3. 指针停在不可滚动区域时**什么都不做**（不向上找同级、不误滚别的列表）。
"""

from __future__ import annotations

import logging
import tkinter as tk
from typing import Any

logger = logging.getLogger("automail.gui.scrolling")

#: 可滚动控件的类名 → 每格滚轮滚动的单位数。
#:
#: Canvas 的 1 个单位是视口的 1/10（Tk 的 yscrollincrement 默认为 0 时），
#: 因此 1 就够；Treeview/Text 的 1 个单位是 1 行，取 3 贴近平台惯例。
_CLASS_UNITS: dict[str, int] = {
    "Treeview": 3,
    "Text": 3,
    "Listbox": 3,
    "Canvas": 1,
}

#: Windows 上一格滚轮的 ``delta`` 值。高精度滚轮/触控板会给更小的值。
_NOTCH = 120


def wheel_units(delta: int, klass: str) -> int:
    """把滚轮事件换算成 ``yview_scroll`` 的单位数（正数向下滚）。

    向上滚返回负数——Tk 的约定是"正数向下"。

    macOS 与部分高精度滚轮给的 ``delta`` 不是 120 的倍数（可能是 ±1），
    因此 ``abs(delta) < 120`` 时按"一个刻度"处理，而不是算出一个 0
    ——那会让滚轮看起来完全没反应。
    """
    if delta == 0:
        return 0

    per = _CLASS_UNITS.get(klass, 3)
    if abs(delta) < _NOTCH:
        notches = 1.0
    else:
        notches = round(abs(delta) / _NOTCH) or 1.0

    units = int(notches) * per
    return -units if delta > 0 else units


def find_scrollable(widget: Any) -> tuple[Any, str] | None:
    """从 ``widget`` 沿祖先链找到最近的可滚动控件。

    祖先链是必须的：指针停在 Canvas 内的输入框上时，事件目标是输入框，
    而真正能滚的是外层 Canvas（bindtags 不含祖先，见模块文档）。
    """
    current = widget
    while current is not None:
        try:
            klass = current.winfo_class()
        except tk.TclError:
            return None
        if klass in _CLASS_UNITS and hasattr(current, "yview_scroll"):
            return current, klass
        current = getattr(current, "master", None)
    return None


class WheelRouter:
    """把滚轮事件路由到指针下方真正该滚动的控件。"""

    def __init__(self, root: tk.Misc) -> None:
        self._root = root
        self._bound = False

    def install(self) -> None:
        """装上滚轮路由。

        先清掉内建的类级绑定（否则会与这里的滚动叠加成双倍速度），
        再在 ``all`` 级别统一拦截——只有 ``all`` 级别能看到"指针下是哪个
        控件"，因此才能沿祖先链找到外层容器。
        """
        if self._bound:
            return

        self._clear_builtin_bindings()

        # Windows / macOS：<MouseWheel> 带 delta
        self._root.bind_all("<MouseWheel>", self._on_wheel, add="+")
        # Linux（X11）：Button-4/5 没有 delta，方向由按键决定
        self._root.bind_all("<Button-4>", self._on_button_up, add="+")
        self._root.bind_all("<Button-5>", self._on_button_down, add="+")

        # 键盘：Canvas **完全没有**键绑定，因此设置页用键盘也滚不动。
        # 只接管 PageUp/PageDown —— 它们在输入框里本来不做任何事，冲突最小。
        # 方向键**不接管**：在输入框里那是移动光标，抢过来会让打字变难受；
        # 而 Treeview/Text 自己已经处理了方向键。
        self._root.bind_all("<Prior>", self._on_page_up, add="+")
        self._root.bind_all("<Next>", self._on_page_down, add="+")
        self._bound = True

    def _on_page_up(self, event: tk.Event) -> str | None:
        return self._on_key(event, direction=-1)

    def _on_page_down(self, event: tk.Event) -> str | None:
        return self._on_key(event, direction=1)

    def _on_key(self, event: tk.Event, *, direction: int) -> str | None:
        """PageUp/PageDown 滚动翻页。

        只处理**事件源自己不会处理**的情况：``Treeview``/``Text``/``Listbox``
        都有内建的翻页绑定，若这里再滚一次会变成翻两页。判据是"找到的可滚动
        控件就是事件源本身且不是 Canvas"——那些控件自己会处理。
        """
        widget = getattr(event, "widget", None)
        target = find_scrollable(widget)
        if target is None:
            return None
        found, klass = target
        if found is widget and klass != "Canvas":
            return None  # 该控件自己有翻页绑定，不要重复

        try:
            found.yview_scroll(direction, "pages")
        except tk.TclError:
            return None
        return "break"

    def _clear_builtin_bindings(self) -> None:
        """清掉 Tk 内建的滚轮类绑定。

        不清的话，指针停在 Treeview 上时**两处**都会滚动：内建类绑定（widget
        自身 bindtags 里）先执行，随后 ``all`` 级别的本模块再执行一次 →
        每格滚 6 行，比预期快一倍。
        """
        for klass in _CLASS_UNITS:
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                try:
                    self._root.bind_class(klass, sequence, "")
                except tk.TclError:  # 某些平台没有这些序列
                    continue

    # ── 事件处理 ──────────────────────────────────────────

    def _on_wheel(self, event: tk.Event) -> str | None:
        return self._scroll(event, delta=int(getattr(event, "delta", 0) or 0))

    def _on_button_up(self, event: tk.Event) -> str | None:
        # X11 只有方向，没有大小
        return self._scroll(event, delta=_NOTCH)

    def _on_button_down(self, event: tk.Event) -> str | None:
        return self._scroll(event, delta=-_NOTCH)

    def _scroll(self, event: tk.Event, *, delta: int) -> str | None:
        target = find_scrollable(getattr(event, "widget", None))
        if target is None:
            return None
        widget, klass = target

        units = wheel_units(delta, klass)
        if units == 0:
            return None
        try:
            widget.yview_scroll(units, "units")
        except tk.TclError:  # 控件正在销毁
            return None
        # 返回 "break"：不要再让任何内层/默认处理重复滚动一次
        return "break"


def install_wheel_support(root: tk.Misc) -> WheelRouter:
    """给整个窗口装上滚轮支持（含 Canvas 与嵌套在其中的控件）。"""
    router = WheelRouter(root)
    router.install()
    return router
