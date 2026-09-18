"""鼠标滚轮/键盘滚动支持的测试。

**为什么这个功能有存在价值**（实测缺口）：

* ``Canvas`` 在 Tk 里**完全没有**滚轮绑定，而设置页正是用一个 Canvas
  承载整页内容 → 那个页面整体不响应滚轮
* 更要紧的是 Tk 的结构性限制：**bindtags 不包含祖先控件**。滚轮事件只投给
  指针下的控件及其*类*，不冒泡。指针停在设置页的输入框上时（输入框占了该页
  大部分面积），事件目标是 ``TEntry``——它和它的类都没有滚轮绑定，事件就此
  消失，外层 Canvas 根本收不到。

因此修法不是"给每个控件加绑定"，而是**在 all 级别拦截后沿祖先链找目标**。

测试里的两条易错点：

1. 有些控件（Treeview/Text/Listbox）**自带**滚轮与翻页绑定，若不先清掉/跳过，
   就会与本模块叠加成双倍速度——看起来只是"滚太快了"，很容易忽略。
2. ``focus_set`` 在窗口没有真实 OS 焦点时**不足以让键盘事件被投递**，
   测试里必须用 ``focus_force``，否则会误判成"没生效"。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import pytest

from automail.gui.scrolling import (
    WheelRouter,
    find_scrollable,
    install_wheel_support,
    wheel_units,
)

# ══════════════════════════════════════════════════════════════
# 换算（纯函数，不需要显示会话）
# ══════════════════════════════════════════════════════════════


def test_wheel_direction_up_is_negative() -> None:
    """向上滚必须是负数——Tk 的约定是"正数向下"。

    反了的话滚轮方向会整个颠倒，而且很容易被当成"系统设置不同"。
    """
    assert wheel_units(-120, "Treeview") > 0, "delta 为负（向下）应滚动为正"
    assert wheel_units(120, "Treeview") < 0, "delta 为正（向上）应为负"


def test_wheel_scrolls_three_lines_by_default() -> None:
    """一格滚轮滚 3 行（贴近 Windows 惯例）。

    Tk 内建是 1 行，实测偏慢；改用 3 行。
    """
    assert abs(wheel_units(-120, "Treeview")) == 3
    assert abs(wheel_units(-120, "Text")) == 3
    assert abs(wheel_units(-120, "Listbox")) == 3


def test_canvas_uses_one_unit_per_notch() -> None:
    """Canvas 每格 1 个单位。

    实测 Canvas 的 1 个单位 = 视口高度的 1/10（约 60px），已经接近"3 行"的
    手感；若也乘 3 会一次滚掉近半屏。
    """
    assert abs(wheel_units(-120, "Canvas")) == 1


def test_high_precision_delta_does_not_produce_zero() -> None:
    """触控板/高精度滚轮给的小 delta 不能被算成 0。

    若按 ``delta/120`` 取整，``delta=1`` 会得到 0 单位 → **看起来完全没反应**。
    因此小于一格时按一格处理。
    """
    assert wheel_units(1, "Treeview") != 0
    assert wheel_units(-1, "Treeview") != 0
    assert abs(wheel_units(1, "Treeview")) == 3


def test_zero_delta_is_noop() -> None:
    assert wheel_units(0, "Treeview") == 0


def test_multiple_notches_scale() -> None:
    """连续快滚（delta=240）应滚两格。"""
    assert abs(wheel_units(-240, "Treeview")) == 6


# ══════════════════════════════════════════════════════════════
# 祖先链查找（纯逻辑，只需 Tk 但不需显示）
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def tk_root():
    """一个**已映射**（mapped）的窗口，放在屏幕外以免闪窗。

    **不能用 ``withdraw()``**：取消映射后 Tk 不做几何计算，Canvas 的尺寸停在
    1x1，``yview`` 退化成 ``(0.002, 0.002)``——滚动就无法被观测到，测试会
    全部误判成"没生效"（实测踩到，而同样的操作在真实窗口里完全正常）。

    放到 ``+3000+3000`` 是为了不打扰正在用电脑的人。
    """
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.geometry("320x200+3000+3000")
    root.update()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_find_scrollable_returns_widget_itself(tk_root) -> None:
    tree = ttk.Treeview(tk_root)
    found = find_scrollable(tree)
    assert found is not None
    assert found[0] is tree
    assert found[1] == "Treeview"


def test_find_scrollable_walks_up_to_canvas(tk_root) -> None:
    """**核心**：指针停在 Canvas 内的输入框上时，要找到外层 Canvas。

    bindtags 不含祖先，所以事件目标是输入框；只有沿着 ``master`` 往上走
    才找得到真正能滚的 Canvas。这条不成立，设置页就整页滚不动。
    """
    canvas = tk.Canvas(tk_root)
    inner = ttk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor="nw")
    entry = ttk.Entry(inner)
    entry.pack()

    found = find_scrollable(entry)
    assert found is not None, "必须沿祖先链找到可滚动控件"
    assert found[0] is canvas, f"应找到 Canvas，实际 {found[1]}"
    assert found[1] == "Canvas"


def test_find_scrollable_returns_none_outside_scrollables(tk_root) -> None:
    """纯布局容器里没有可滚动控件时返回 ``None``（不误滚别处）。"""
    frame = ttk.Frame(tk_root)
    label = ttk.Label(frame, text="x")
    assert find_scrollable(label) is None


def test_find_scrollable_prefers_nearest_ancestor(tk_root) -> None:
    """嵌套两层可滚动区域时，取**最近**的那个。

    取最外层会让"滚内部小列表却滚了整个页面"，方向感完全错乱。
    """
    outer = tk.Canvas(tk_root)
    inner_frame = ttk.Frame(outer)
    outer.create_window((0, 0), window=inner_frame, anchor="nw")
    inner_text = tk.Text(inner_frame)
    inner_text.pack()

    found = find_scrollable(inner_text)
    assert found is not None
    assert found[0] is inner_text, f"应取最近的 Text，实际 {found[1]}"


# ══════════════════════════════════════════════════════════════
# 安装与叠加
# ══════════════════════════════════════════════════════════════


def test_install_clears_builtin_bindings(tk_root) -> None:
    """必须清掉内建类级滚轮绑定，否则每格滚两倍。

    内建绑定在控件自己的 bindtags 里（先执行），本模块在 ``all`` 级别
    （后执行）→ 两处都滚。实测表现为"滚太快"，很容易忽略成个人感觉。
    """
    assert tk_root.bind_class("Treeview", "<MouseWheel>"), "前置：内建绑定应存在"
    install_wheel_support(tk_root)
    assert not tk_root.bind_class("Treeview", "<MouseWheel>"), "应已清掉"
    assert not tk_root.bind_class("Text", "<MouseWheel>")


def test_install_is_idempotent(tk_root) -> None:
    """重复安装不应重复绑定（那会变成多倍滚动）。"""
    router = WheelRouter(tk_root)
    router.install()
    router.install()
    assert router._bound is True


def test_wheel_scrolls_canvas_through_nested_entry(tk_root) -> None:
    """端到端：滚轮落在输入框上时，外层 Canvas 要滚动。"""
    canvas = tk.Canvas(tk_root, height=100)
    inner = ttk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor="nw")
    for _ in range(40):
        ttk.Label(inner, text="字段").pack(anchor="w")
        ttk.Entry(inner).pack(fill="x")
    canvas.pack(fill="both", expand=True)
    canvas.update_idletasks()
    canvas.configure(scrollregion=canvas.bbox("all"))
    tk_root.update()

    install_wheel_support(tk_root)

    entry = next(c for c in inner.winfo_children() if c.winfo_class() == "TEntry")
    before = canvas.yview()[0]
    entry.event_generate("<MouseWheel>", delta=-120)
    tk_root.update()

    assert canvas.yview()[0] > before, (
        "指针在输入框上时外层 Canvas 必须滚动（这是设置页滚不动的根因）"
    )


def test_wheel_over_blank_canvas_scrolls(tk_root) -> None:
    canvas = tk.Canvas(tk_root, height=100)
    inner = ttk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor="nw")
    for _ in range(40):
        ttk.Label(inner, text="x").pack()
    canvas.pack(fill="both", expand=True)
    canvas.update_idletasks()
    canvas.configure(scrollregion=canvas.bbox("all"))
    tk_root.update()

    install_wheel_support(tk_root)
    before = canvas.yview()[0]
    canvas.event_generate("<MouseWheel>", delta=-120)
    tk_root.update()
    assert canvas.yview()[0] > before


def test_treeview_does_not_double_scroll(tk_root) -> None:
    """Treeview 每格只滚 3 行，不是 6 行（验证没与内建绑定叠加）。"""
    tree = ttk.Treeview(tk_root, columns=("a",), show="headings", height=10)
    tree.heading("a", text="a")
    for index in range(200):
        tree.insert("", "end", values=(index,))
    tree.pack(fill="both", expand=True)
    tk_root.update()

    install_wheel_support(tk_root)

    tree.yview_moveto(0)
    tk_root.update()
    before = tree.yview()[0]
    tree.event_generate("<MouseWheel>", delta=-120)
    tk_root.update()
    moved_rows = (tree.yview()[0] - before) * len(tree.get_children())

    assert 2.0 <= moved_rows <= 4.0, (
        f"应滚约 3 行，实际 {moved_rows:.1f} 行"
        "（约 6 行说明与内建绑定叠加了）"
    )


def test_unscrollable_area_is_noop(tk_root) -> None:
    """指针停在不可滚动区域时什么都不做（不误滚别的列表）。"""
    frame = ttk.Frame(tk_root)
    frame.pack()
    install_wheel_support(tk_root)
    # 不应抛异常
    frame.event_generate("<MouseWheel>", delta=-120)
    tk_root.update()


# ══════════════════════════════════════════════════════════════
# 键盘翻页（Canvas 同样没有键绑定）
# ══════════════════════════════════════════════════════════════


def test_page_down_scrolls_canvas(tk_root) -> None:
    """Canvas 连键绑定都没有，设置页用键盘也滚不动。

    只接管 PageUp/PageDown：它们在输入框里本来不做任何事，冲突最小。
    方向键**不接管**——那在输入框里是移动光标。
    """
    canvas = tk.Canvas(tk_root, height=100)
    inner = ttk.Frame(canvas)
    canvas.create_window((0, 0), window=inner, anchor="nw")
    for _ in range(60):
        ttk.Label(inner, text="x").pack()
    canvas.pack(fill="both", expand=True)
    canvas.update_idletasks()
    canvas.configure(scrollregion=canvas.bbox("all"))
    tk_root.update()

    install_wheel_support(tk_root)
    # 注意用 focus_force：窗口没有真实 OS 焦点时 focus_set 不足以投递键事件
    canvas.focus_force()
    tk_root.update()

    before = canvas.yview()[0]
    canvas.event_generate("<Next>")
    tk_root.update()
    assert canvas.yview()[0] > before


def test_treeview_paging_not_doubled(tk_root) -> None:
    """Treeview 自己有翻页绑定，本模块必须跳过它，否则一次翻两页。"""
    tree = ttk.Treeview(tk_root, columns=("a",), show="headings", height=10)
    tree.heading("a", text="a")
    for index in range(300):
        tree.insert("", "end", values=(index,))
    tree.pack(fill="both", expand=True)
    tk_root.update()

    install_wheel_support(tk_root)
    tree.focus_force()
    tk_root.update()

    def measure() -> float:
        tree.yview_moveto(0)
        tk_root.update()
        before = tree.yview()[0]
        tree.event_generate("<Next>")
        tk_root.update()
        return (tree.yview()[0] - before) * len(tree.get_children())

    with_router = measure()
    tk_root.unbind_all("<Next>")
    native = measure()
    assert abs(with_router - native) < 0.5, (
        f"路由后 {with_router:.1f} 行 vs 原生 {native:.1f} 行——说明叠加了"
    )


# ══════════════════════════════════════════════════════════════
# 设置面板：密码字段必须真为空（数据损坏 bug 的端到端回归）
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def gui_app(tmp_path, monkeypatch):
    """一个指向临时目录的真实 App（不碰使用者数据）。"""
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        "IMAP_USER=real@example.com\nIMAP_AUTH_CODE=REAL_SECRET_16CH\n"
        "LLM_BASE_URL=https://api.example.com\nLLM_API_KEY=sk-real-key\n",
        encoding="utf-8",
    )
    try:
        from automail.gui.app import App
        from automail.gui.state import AppState
    except ImportError:  # pragma: no cover
        pytest.skip("GUI 不可用")

    try:
        app = App(state=AppState.create())
    except tk.TclError:
        pytest.skip("no display")
    app.root.update()
    yield app
    try:
        app._do_close()
    except Exception:  # noqa: BLE001
        pass


def test_password_entry_is_actually_empty(gui_app) -> None:
    """**数据损坏 bug 的端到端回归**：密码框里必须是空字符串。

    这条比只查源码更有价值——它直接读控件的真实内容。占位提示被塞进 Entry
    时，``.get()`` 会返回那段中文提示，而保存逻辑会把它当新密码写进去，
    **把真实授权码覆盖掉**。使用者不会立刻发现，直到某次同步报授权失败。
    """
    panel = gui_app.panels["settings"]
    panel.refresh()
    gui_app.root.update()

    for key, entry in panel._secret_entries.items():
        assert entry.get() == "", (
            f"{key} 的输入框内容应为空，实际 {entry.get()!r}"
            "——非空文本会被当成新密码保存，覆盖真实凭据"
        )


def test_password_status_shown_by_separate_label(gui_app) -> None:
    """已配置状态由独立标签表达（而不是塞进输入框）。"""
    panel = gui_app.panels["settings"]
    panel.refresh()
    gui_app.root.update()

    label = panel._secret_status["IMAP_AUTH_CODE"]
    assert "已配置" in label.cget("text"), (
        f"应显示已配置，实际 {label.cget('text')!r}"
    )


def test_saving_with_empty_password_fields_does_not_change_secrets(
    gui_app, tmp_path
) -> None:
    """**最关键的一条**：不动密码框直接保存，不得改动已有凭据。

    模拟使用者「只想改邮箱账号，然后顺手点了保存」这个非常常见的动作。
    """
    import re

    env_path = tmp_path / ".env"
    before = env_path.read_text(encoding="utf-8")

    panel = gui_app.panels["settings"]
    panel.refresh()
    gui_app.root.update()

    # 只改非密码项；密码字段保持界面上的实际内容（应为空）
    values = {"IMAP_USER": "changed@example.com"}
    secrets = {
        key: entry.get().strip()
        for key, entry in panel._secret_entries.items()
        if entry.get().strip()
    }
    assert secrets == {}, f"密码字段应为空，实际 {secrets}"

    panel._do_save(values, secrets)

    after = env_path.read_text(encoding="utf-8")
    original_code = re.search(r"^IMAP_AUTH_CODE=(.*)$", before, re.M).group(1)
    final_code = re.search(r"^IMAP_AUTH_CODE=(.*)$", after, re.M).group(1)
    assert final_code == original_code, (
        f"授权码被改动了：{original_code!r} -> {final_code!r}"
    )
    assert "changed@example.com" in after, "非密码项的修改应当生效"


def test_panels_populated_on_startup(gui_app) -> None:
    """**实测 bug 的回归**：启动后各面板就该有内容，不需要手动点刷新。

    原先启动时一次都不刷、且非强制刷新只刷当前可见页 → 打开窗口每页空白，
    使用者会以为"配置没读到"。
    """
    settings_panel = gui_app.panels["settings"]
    assert settings_panel.imap_user.get() == "real@example.com", (
        "启动后设置页就该显示已填的账号"
    )
    assert settings_panel.llm_base_url.get() == "https://api.example.com"
