"""可执行入口：``python -m automail`` 与打包后的 exe 共用。

为什么单独一个模块而不是直接指向 ``cli.py``：

* 打包成 exe 后，``sys.exit(main())`` 的返回值就是进程退出码，
  计划任务据此判断成败，因此必须显式传递。
* Windows 上双击运行时，若程序瞬间失败（例如缺配置），
  窗口会一闪而过看不到原因。这里在**冻结模式下捕获致命异常并暂停**，
  让使用者能看到错误；源码运行时保持原样（终端不会消失）。
"""

from __future__ import annotations

import sys


def _is_own_console() -> bool:
    """当前控制台是否是为本进程新建的（即用户「双击」启动）。

    为什么不用 ``stdin.isatty()``：在 Git Bash 等终端模拟器下即使重定向了
    stdin，``isatty()`` 仍可能返回 True（伪终端），判断不可靠。若在计划任务里
    误判为交互式，``input()`` 会让任务**永久挂住**——这比看不到错误严重得多。

    Windows 上可靠的判据是 ``GetConsoleProcessList``：控制台只附着本进程时，
    说明它是随本次启动新建的（双击场景）；由 cmd/计划任务/终端启动时，
    控制台里还会有 shell 等其它进程。
    """
    if not getattr(sys, "frozen", False):
        return False

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        process_ids = (ctypes.c_uint * 8)()
        count = kernel32.GetConsoleProcessList(process_ids, 8)
        # count == 1 表示只有本进程附着 → 新建的控制台（双击）
        # count == 0 表示没有控制台（无窗口，例如计划任务）
        return count == 1
    except Exception:  # noqa: BLE001 - 非 Windows 或调用失败
        return False


def _make_output_lenient() -> None:
    """让标准输出/错误在**无法编码的字符**上降级，而不是崩溃。

    **实测的崩溃点**：中文 Windows 控制台默认代码页是 GBK，而邮件主题里常有
    GBK 表示不了的字符（emoji、生僻字）。渲染到终端就抛
    ``UnicodeEncodeError: 'gbk' codec can't encode character '\\U0001f4b3'``，
    整个命令以「未预期的错误」结束——而它本可以正常显示其余内容。
    触发路径就是 ``automail stats``：库里有一封带 emoji 的邮件，统计就再也
    跑不出结果。

    **关键：只改 errors，不改 encoding。** 早先的实现把编码强改成 UTF-8，
    结果在 GBK 控制台里中文全变成乱码（``邮件`` → ``閭�浠�``）——修好了崩溃却
    弄坏了正常显示。保留原编码、只让编码不了的字符降级为 ``?``，才是正确取舍：
    显示成一个问号远好过整条命令失败，也好过满屏乱码。

    输出被重定向时 Python 默认已用 UTF-8，这里同样不动编码。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        encoding = getattr(stream, "encoding", None) or "utf-8"
        try:
            reconfigure(encoding=encoding, errors="replace")
        except (ValueError, OSError):  # noqa: BLE001 - 某些流不支持重配置
            pass


def _pause_if_frozen() -> None:
    """仅在「双击启动且运行出错」时等一个回车，让使用者看清错误。

    三种情况都不暂停（否则会挂住调用方）：
    * 非冻结模式（源码运行，终端本来就不会消失）
    * 由 shell / 计划任务启动（控制台里还有别的进程）
    * 没有控制台
    """
    if not _is_own_console():
        return
    try:
        input("\n按回车键退出…")
    except (EOFError, KeyboardInterrupt):
        pass


def _first_run_guide() -> None:
    """便携版首次使用引导（只在真正首次时执行一次）。

    判据：数据目录不存在。这样「配好了、用过一次」之后不再打扰。

    双击运行时最常见的困惑是「我该把文件放哪」，因此这里直接告诉路径，
    而不是甩一句「缺少凭据」。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        from automail.settings import app_base_dir

        base = app_base_dir()
        if (base / "data").exists():
            return  # 已经用过，不再提示

        # .env.example 用于生成初始 .env。打包后它可能落在 exe 同级，
        # 也可能在 PyInstaller 的资源目录（_internal），两处都要找。
        from automail.cli import _env_template_path
        from automail.portable import (
            ensure_portable_layout,
            find_config_sources,
            print_first_run_guide,
        )

        report = ensure_portable_layout(base, template=_env_template_path())
        report.discovered = find_config_sources(base)
        print_first_run_guide(report)
    except Exception:  # noqa: BLE001 - 引导失败不应阻止命令执行
        pass


def _is_gui_invocation() -> bool:
    """本次启动是否来自窗口化的那个可执行文件。

    打包后有两个 exe，**共用同一份资源与同一个入口脚本**（见 ``automail.spec``
    里两个 ``EXE`` 对象共用一个 ``COLLECT``）——把依赖打包两遍既臃肿又容易
    不一致。因此按可执行文件名分派，这是两个入口唯一的差别。

    源码运行时（``python -m automail``）永远是命令行模式。
    """
    if not getattr(sys, "frozen", False):
        return False
    import os
    from pathlib import Path

    name = Path(sys.executable).stem.lower()
    return name.endswith("-gui") or os.environ.get("AUTOMAIL_GUI") == "1"


def main() -> int:
    # 必须在任何输出之前：中文控制台默认 GBK，邮件主题里的 emoji 会让
    # 渲染直接抛 UnicodeEncodeError（实测 stats 命令因此完全不可用）。
    _make_output_lenient()

    if _is_gui_invocation():
        # 窗口化入口：不走首次引导的「按回车退出」逻辑——窗口程序没有控制台，
        # 在 stdin 上等待会变成一次永久挂起。
        from automail.gui_main import main as gui_main

        return gui_main()

    _first_run_guide()
    try:
        # 用**绝对**导入：这个文件会被 PyInstaller 当作顶层入口脚本打包，
        # 此时它没有父包，`from .cli import ...` 会报
        # "attempted relative import with no known parent package"。
        from automail.cli import main as cli_main

        return cli_main()
    except SystemExit as exc:  # 某些路径会直接抛 SystemExit
        return int(exc.code or 0)
    except BaseException as exc:  # noqa: BLE001 - 兜底，确保退出码非零
        import traceback

        print(f"启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    code = main()
    if code != 0:
        _pause_if_frozen()
    sys.exit(code)
