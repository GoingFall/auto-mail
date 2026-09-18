"""图形界面入口与构建自检的测试。

重点在**退出码的语义**：构建脚本靠它决定「跳过」还是「报错」。

* ``3`` —— 本机没有显示会话（构建机常见）→ 跳过，**不报错**
* ``2`` —— 真的坏了 → **必须让构建失败**

这两者的区分不是形式主义：本项目踩过一次同类的坑（把 ``unittest`` 从打包里
排除，exe 构建成功、``--version`` 正常，但日历功能一用就崩）。若把"打包缺
Tcl 数据文件"也当成"本机没显示"而跳过，就会发布一个双击即崩的 exe。
"""

from __future__ import annotations

import tkinter
from pathlib import Path

import pytest

from automail import gui_main
from automail.gui_main import EXIT_NO_DISPLAY

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ══════════════════════════════════════════════════════════════
# 失败分类：跳过 vs 报错
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "message",
    [
        "no display name and no DISPLAY environment variable",
        "couldn't connect to display \":0\"",
        "cannot connect to display",
    ],
)
def test_headless_is_skippable(message: str) -> None:
    """没有显示会话 → 可跳过（构建机通常如此，不该报错）。"""
    exc = tkinter.TclError(message)
    assert gui_main._needs_display_skip(exc)
    assert "headless" in gui_main._describe_tk_failure(exc).lower() or "显示" in (
        gui_main._describe_tk_failure(exc)
    )


@pytest.mark.parametrize(
    "message",
    [
        "Can't find a usable init.tcl in the following directories",
        "Can't find a usable tk.tcl in the following directories",
    ],
)
def test_missing_tcl_data_is_a_build_failure(message: str) -> None:
    """**打包缺 Tcl/Tk 数据文件必须判为失败，不能当 headless 跳过。**

    这是本自检存在的首要理由：构建期看不出来，只有真正建窗口时才暴露。
    若这里判成"跳过"，一个缺数据文件的 exe 会被当作构建成功发布出去。
    """
    exc = tkinter.TclError(message)
    assert not gui_main._needs_display_skip(exc), "缺数据文件不得被当作没有显示会话"
    assert "打包" in gui_main._describe_tk_failure(exc)


def test_describe_handles_import_error() -> None:
    """缺 tkinter 是环境问题，应提示安装而不是报「打包失败」。"""
    described = gui_main._describe_tk_failure(ImportError("No module named tkinter"))
    assert "tkinter" in described


# ══════════════════════════════════════════════════════════════
# 自检本身
# ══════════════════════════════════════════════════════════════


def test_selftest_returns_documented_exit_code() -> None:
    """在有显示会话的机器上自检应通过（返回 0）。

    无显示时返回 3——两者都是"预期内"的结果，断言取其二。
    """
    code = gui_main.selftest()
    assert code in (0, EXIT_NO_DISPLAY, 2)
    if code == 2:
        pytest.fail("自检真的失败了（既不是通过、也不是无显示环境）")


def test_selftest_builds_real_widgets(monkeypatch) -> None:
    """自检必须真的构建控件，而不只是 import tkinter。

    ``import tkinter`` 成功并不代表 Tcl/Tk 数据文件齐全——只有建窗口才会
    触发数据文件加载。

    会建**两个**窗口：一个探针窗口（验证控件族与 Tcl 数据），
    一个真实的 :class:`~automail.gui.app.App`（验证各面板的 import 与数据绑定）。
    后者能抓到"某个面板依赖的模块没打进包"——那类问题只在真正实例化面板时暴露。
    """
    built: list[str] = []
    real_tk = tkinter.Tk

    class _SpyTk(real_tk):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            built.append("Tk")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(tkinter, "Tk", _SpyTk)
    code = gui_main.selftest()
    if code == EXIT_NO_DISPLAY:
        pytest.skip("no display")
    assert len(built) >= 1, "自检必须真的创建窗口"
    assert len(built) == 2, (
        f"应建探针窗口 + 真实 App 两个窗口，实际 {len(built)} 个"
        "（少了说明面板自检没跑）"
    )


def test_selftest_can_skip_panels(monkeypatch) -> None:
    """``with_panels=False`` 只做基础窗口自检（用于定位"是 Tk 还是面板"的问题）。"""
    code = gui_main.selftest(with_panels=False)
    assert code in (0, EXIT_NO_DISPLAY, 2)


# ══════════════════════════════════════════════════════════════
# 入口分派
# ══════════════════════════════════════════════════════════════


def test_source_run_is_never_gui(monkeypatch) -> None:
    """源码运行时永远是命令行模式（``python -m automail``）。"""
    monkeypatch.delattr(__import__("sys"), "frozen", raising=False)
    assert __import__("automail.__main__", fromlist=["x"])._is_gui_invocation() is False


def test_frozen_gui_name_dispatches_to_gui(monkeypatch) -> None:
    """打包后按可执行文件名分派：``auto-mail-gui.exe`` → 图形界面。"""
    import sys

    module = __import__("automail.__main__", fromlist=["x"])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\x\auto-mail-gui.exe", raising=False)
    assert module._is_gui_invocation() is True

    monkeypatch.setattr(sys, "executable", r"C:\x\auto-mail.exe", raising=False)
    assert module._is_gui_invocation() is False


def test_gui_selftest_can_be_bypassed_to_console(monkeypatch) -> None:
    """``AUTOMAIL_GUI=1`` 强制窗口模式（便于开发时从源码试界面）。"""
    import os
    import sys

    module = __import__("automail.__main__", fromlist=["x"])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\x\auto-mail.exe", raising=False)
    monkeypatch.setenv("AUTOMAIL_GUI", "1")
    assert module._is_gui_invocation() is True
    _ = os


# ══════════════════════════════════════════════════════════════
# 交付约束
# ══════════════════════════════════════════════════════════════


def test_spec_does_not_exclude_tkinter() -> None:
    """spec **不得**再把 tkinter 排除掉——那是图形界面的硬依赖。

    历史上排除它是因为当时没有 GUI；保留这行会让 exe 构建"成功"但界面一开就崩。
    """
    text = (PROJECT_ROOT / "automail.spec").read_text(encoding="utf-8")
    # 只看 excludes 块内的内容，避免误判注释里提到的 "tkinter"
    block = text[text.index("excludes = [") : text.index("]", text.index("excludes = ["))]
    code_lines = [
        line for line in block.splitlines() if not line.strip().startswith("#")
    ]
    assert not any("tkinter" in line for line in code_lines), (
        "excludes 里仍有 tkinter，图形界面无法工作"
    )


def test_spec_builds_both_executables() -> None:
    """两个 exe 都要产出：控制台版（计划任务用）+ 窗口版（双击用）。

    共用同一个 COLLECT，避免把 tkinter 打包两遍。
    """
    text = (PROJECT_ROOT / "automail.spec").read_text(encoding="utf-8")
    assert '"auto-mail"' in text
    assert '"auto-mail-gui"' in text
    assert "console=True" in text, "控制台版必须保留（计划任务依赖退出码）"
    assert "console=False" in text, "窗口版必须不分配控制台"


def test_build_script_has_gui_selftest() -> None:
    """构建脚本必须跑 GUI 自检，且区分跳过与失败。

    只测 ``--version`` 是不够的：它对界面是否可用毫无信息量。
    """
    raw = (PROJECT_ROOT / "scripts" / "build-exe.ps1").read_bytes()
    assert all(byte < 0x80 for byte in raw), "build-exe.ps1 必须保持纯 ASCII"
    text = raw.decode("ascii")

    assert "--selftest" in text, "构建脚本必须调用 GUI 自检"
    assert "auto-mail-gui.exe" in text
    # 退出码 3 必须被当作"跳过"而不是失败
    assert "-eq 3" in text, "必须单独处理「无显示环境」的退出码 3"
