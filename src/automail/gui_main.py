"""图形界面入口（窗口化可执行文件）。

与 :mod:`automail.__main__` 的分工：那个是命令行入口，这个是窗口化入口。
两者由**同一个入口脚本按可执行文件名分派**，因此共享一套打包资源（见
``automail.spec`` 里两个 ``EXE`` 对象共用一个 ``COLLECT``）——避免把同一份
依赖打包两遍。

``--selftest`` 是给构建脚本用的：它构建真实的 Tk 窗口与全部控件后立即销毁，
据此判断「这个 exe 里的界面真的能用」。**存在理由是踩过的坑**：本项目曾把
``unittest`` 从打包里排除，结果 exe 构建成功、``--version`` 也正常，但一调用
日历功能就崩——因为 ``httplib2`` 在导入期依赖它。tkinter 的问题会以完全相同
的方式出现：构建通过、双击才崩。

退出码区分三种结果，**构建脚本据此判断该不该报错**：

* ``0``  —— 界面构建成功
* ``3``  —— 环境不支持图形界面（没有 Tk、或没有可用显示会话）。
  构建机上通常如此，**不应视为失败**——但这台机器上确实跑不了界面，
  所以要如实报出来而不是假装成功。
* ``2``  —— 真的崩了（代码或打包问题），**必须让构建失败**
"""

from __future__ import annotations

import sys

#: 环境不支持图形界面的退出码（构建脚本应跳过、不报错）
EXIT_NO_DISPLAY = 3

#: 判定「只是没有显示会话」的特征串。
#:
#: **必须与"打包缺文件"区分开**：前者在构建机上很常见、应当跳过；后者是
#: 打包缺陷、必须让构建失败。若一律当成 headless 跳过，一个缺 Tcl 数据文件的
#: exe 会顺利发布出去，使用者双击才崩——正是这个自检要防的事。
_NO_DISPLAY_MARKERS = (
    "no display name",
    "couldn't connect to display",
    "cannot connect to display",
    "no display",
    "display is not available",
    "error opening display",
)

#: 判定「打包缺 Tcl/Tk 数据文件」的特征串（属于**代码/打包问题**）。
_MISSING_TCL_MARKERS = (
    "can't find a usable init.tcl",
    "can't find a usable tk.tcl",
    "no such file or directory",
    "cannot find",
)


def _needs_display_skip(exc: BaseException) -> bool:
    """这个 Tk 初始化失败是否属于「本机没有显示会话」→ 可跳过。"""
    if not isinstance(exc, BaseException):
        return False
    text = str(exc).lower()
    # 缺 Tcl 数据文件优先判定为打包问题（它的报错里也可能含 cannot find）
    if any(marker in text for marker in _MISSING_TCL_MARKERS):
        return False
    return any(marker in text for marker in _NO_DISPLAY_MARKERS)


def _describe_tk_failure(exc: BaseException) -> str:
    """把 Tk 初始化失败翻译成可读原因。"""
    text = str(exc)
    if isinstance(exc, ImportError):
        return f"缺少 tkinter：{text}"
    if _needs_display_skip(exc):
        return f"没有可用的显示会话（headless 环境）：{text}"
    if any(marker in text.lower() for marker in _MISSING_TCL_MARKERS):
        return (
            "Tcl/Tk 数据文件缺失——打包时没收集 tcl/tk 目录。"
            f"这是打包问题，不是环境问题：{text}"
        )
    return f"Tk 初始化失败：{text}"


def selftest(*, with_panels: bool = True) -> int:
    """构建界面并立即销毁，用于构建期自检。

    刻意走**真实窗口构建路径**（不是简单 ``import tkinter``）：Tcl/Tk 的
    初始化会去加载 ``init.tcl`` 等数据文件，只有真正建出窗口才能覆盖到。

    ``with_panels`` 会进一步构建真实的 :class:`~automail.gui.app.App`
    （含全部标签页）。这能抓到"某个面板依赖的模块没打进包"——那类问题
    只在真正实例化面板时才暴露，是 ``unittest``/``httplib2`` 那次的同类陷阱。
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:
        print(_describe_tk_failure(exc), file=sys.stderr)
        return EXIT_NO_DISPLAY

    try:
        root = tk.Tk(className="auto-mail-selftest")
    except tk.TclError as exc:
        print(_describe_tk_failure(exc), file=sys.stderr)
        # 只有「本机没有显示会话」才允许跳过；缺 Tcl 数据文件是打包缺陷，
        # 必须让构建失败（否则会发布一个双击就崩的 exe）。
        return EXIT_NO_DISPLAY if _needs_display_skip(exc) else 2

    try:
        root.withdraw()  # 自检不该在屏幕上闪一个窗口

        # 覆盖本项目实际会用到的控件族：没有这一步，"能建根窗口"并不代表
        # ttk 的主题数据也在包里
        frame = ttk.Frame(root)
        notebook = ttk.Notebook(frame)
        for name in ("待审队列", "邮件", "设置"):
            tab = ttk.Frame(notebook)
            ttk.Label(tab, text=name).pack()
            notebook.add(tab, text=name)
        notebook.pack()
        tree = ttk.Treeview(frame, columns=("a", "b"), show="headings")
        tree.heading("a", text="列")
        tree.insert("", "end", values=("1", "2"))
        tree.pack()
        frame.pack()

        # 强制走一遍几何计算：数据文件缺失时常常在这里才暴露
        root.update_idletasks()

        label = ttk.Label(root, text="ok")
        assert label.cget("text") == "ok"
        assert len(notebook.tabs()) == 3
        assert len(tree.get_children()) == 1
    except Exception as exc:  # noqa: BLE001 - 自检要把任何异常翻译成退出码
        print(f"界面自检失败（代码问题）：{type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 2
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    if with_panels:
        code = _selftest_panels()
        if code != 0:
            return code

    print(f"界面自检通过（Tk {root.tk.call('info', 'patchlevel')}）")
    return 0


def _selftest_panels() -> int:
    """构建真实的 App（全部面板）并立刻销毁。

    面板里的 import 错误（少打包一个模块）只有走到这一步才会暴露。
    """
    try:
        from automail.gui.app import App
        from automail.gui.state import AppState

        app = App(state=AppState.create())
        # 逐页 refresh 一遍：数据绑定与 SQL 的语法错误都在这里暴露，
        # 而这些在"只建窗口"的自检里完全测不到
        for name, panel in app.panels.items():
            refresh = getattr(panel, "refresh", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"面板 {name} 刷新失败：{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    import traceback

                    traceback.print_exc()
                    return 2
        app._do_close()
    except Exception as exc:  # noqa: BLE001
        print(f"面板自检失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    """图形界面主入口。"""
    args = list(sys.argv[1:] if argv is None else argv)

    if "--selftest" in args:
        return selftest()

    # 真正的应用窗口由 gui.app 提供（P1 起可用）。
    try:
        from automail.gui.app import run_app
    except ImportError as exc:  # pragma: no cover - 开发早期的过渡状态
        print(f"图形界面尚不可用：{exc}", file=sys.stderr)
        return 2

    return run_app()


if __name__ == "__main__":
    sys.exit(main())
