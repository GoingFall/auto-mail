"""运行脚本与命令行入口的交付约束测试。

这里的用例都对应过真实 bug，不是理论推演：

* ``scripts/run.ps1`` 曾是 UTF-8 编码（无 BOM）且带中文注释。Windows
  PowerShell 5.1 按系统代码页（zh-CN 下为 GBK）读取 .ps1，中文变乱码后
  **吞掉了换行符**，把 ``exit $LASTEXITCODE`` 并进了注释——脚本照常「成功」
  结束并返回 0，于是所有计划任务都会误判成功。
* ``ValidateSet`` 曾漏掉 ``runs``，导致该命令经脚本调用时参数校验失败。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from automail import cli as cli_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = PROJECT_ROOT / "scripts" / "run.ps1"

#: CLI 实际注册的命令名（自动从 typer 取出，避免与脚本手写列表漂移）
def registered_commands() -> set[str]:
    import typer

    command = typer.main.get_command(cli_module.app)
    return set(getattr(command, "commands", {}) or {})


def test_run_script_exists() -> None:
    assert RUN_SCRIPT.is_file(), f"缺少计划任务入口脚本：{RUN_SCRIPT}"


def test_run_script_is_pure_ascii() -> None:
    """脚本必须纯 ASCII。

    PowerShell 5.1 无法可靠解析无 BOM 的 UTF-8 非 ASCII 内容；乱码合并行
    会静默删掉语句。用英文注释是唯一稳妥做法。
    """
    raw = RUN_SCRIPT.read_bytes()
    offenders = [
        (index, byte)
        for index, byte in enumerate(raw)
        if byte > 0x7F
    ]
    assert not offenders, (
        f"scripts/run.ps1 含非 ASCII 字节（首个位于偏移 {offenders[0][0]}）。"
        "请只用 ASCII 编写该脚本。"
    )


def test_run_script_has_no_bom() -> None:
    """带 BOM 会让部分调用方式出现多余字符，统一不带 BOM。"""
    raw = RUN_SCRIPT.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_run_script_propagates_exit_code() -> None:
    """脚本必须显式 exit 退出码，且先捕获再退出。"""
    text = RUN_SCRIPT.read_text(encoding="ascii")
    assert re.search(r"\$exitCode\s*=\s*\$LASTEXITCODE", text), (
        "应先捕获 $LASTEXITCODE 到变量"
    )
    assert re.search(r"exit\s+\$exitCode", text), "必须以 exit $exitCode 结束"


def test_run_script_validateset_covers_all_commands() -> None:
    """ValidateSet 必须覆盖 CLI 注册的全部命令，否则脚本调用会被拒绝。"""
    text = RUN_SCRIPT.read_text(encoding="ascii")
    match = re.search(r"\[ValidateSet\(([^)]*)\)\]", text)
    assert match, "未找到 ValidateSet 声明的命令白名单"

    declared = {
        item.strip().strip("'\"")
        for item in match.group(1).split(",")
        if item.strip()
    }
    actual = registered_commands()
    missing = actual - declared
    extra = declared - actual
    assert not missing, f"ValidateSet 漏掉了命令：{sorted(missing)}"
    assert not extra, f"ValidateSet 包含不存在的命令：{sorted(extra)}"


def test_run_script_pins_interpreter_path() -> None:
    """计划任务不应依赖 PATH——必须用项目内虚拟环境的绝对路径。"""
    text = RUN_SCRIPT.read_text(encoding="ascii")
    assert ".venv" in text, "应固定使用项目内 .venv 解释器"
    assert "Set-Location" in text, "应固定工作目录"


def test_run_script_reports_missing_venv_as_fatal() -> None:
    """缺少虚拟环境时应以退出码 2 明确失败，而不是静默返回 0。"""
    text = RUN_SCRIPT.read_text(encoding="ascii")
    assert "exit 2" in text


@pytest.mark.parametrize("command", ["doctor", "runs"])
def test_registered_commands_include_core_ones(command: str) -> None:
    assert command in registered_commands()


# ── install-tasks.ps1：任务计划安装脚本 ─────────────────────

INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install-tasks.ps1"


def test_install_tasks_exists() -> None:
    assert INSTALL_SCRIPT.is_file(), f"缺少任务计划安装脚本：{INSTALL_SCRIPT}"


def test_install_tasks_is_pure_ascii() -> None:
    """同 run.ps1：PowerShell 5.1 按系统代码页解析，非 ASCII 会坏掉脚本。

    这不是理论风险——run.ps1 曾因中文注释吞掉换行，把 `exit` 语句并进注释，
    导致所有计划任务误判成功。
    """
    raw = INSTALL_SCRIPT.read_bytes()
    offenders = [i for i, b in enumerate(raw) if b > 0x7F]
    assert not offenders, (
        f"scripts/install-tasks.ps1 含非 ASCII 字节（首个位于偏移 {offenders[0]}）"
    )


def test_install_tasks_has_no_bom() -> None:
    assert not INSTALL_SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


def test_install_tasks_guards_polling_interval() -> None:
    """必须拒绝过短的轮询间隔。

    163 不支持 IDLE，只能轮询；间隔过短会触发风控并断连（实测过）。
    这个守卫是防止有人「图快」把间隔调到 1 分钟。
    """
    text = INSTALL_SCRIPT.read_text(encoding="ascii")
    assert "IntervalMinutes -lt 15" in text, "缺少最小轮询间隔守卫"
    assert "risk control" in text.lower() or "风控" in text


def test_install_tasks_registers_expected_tasks() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="ascii")
    assert "auto-mail run" in text
    assert "auto-mail digest" in text
    assert "auto-mail backup" in text


def test_install_tasks_supports_removal() -> None:
    """必须能卸载——只装不卸会留下用户无法清理的系统状态。"""
    text = INSTALL_SCRIPT.read_text(encoding="ascii")
    assert "-Remove" in text
    assert "schtasks /delete" in text or "/delete" in text


def test_install_tasks_uses_fixed_runner_path() -> None:
    """任务必须调用项目内固定的 run.ps1，而不是依赖 PATH。"""
    text = INSTALL_SCRIPT.read_text(encoding="ascii")
    assert "run.ps1" in text
    assert "Split-Path -Parent $PSScriptRoot" in text


def test_readme_documents_task_installation() -> None:
    """使用者必须能从 README 找到「如何安装任务计划」。

    内容本身已按读者分流到命令行指南（README 变薄、只做入口），因此这里
    断言的是**可发现性**：README 指向该指南，且指南确实讲了安装方式。
    只查文件是否存在会让「文档被删了测试还绿」。
    """
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "guide-cli.md" in readme, "README 必须提供通往命令行指南的入口"

    guide = PROJECT_ROOT / "docs" / "guide-cli.md"
    assert guide.exists(), "README 指向的命令行指南必须存在"
    assert "install-tasks.ps1" in guide.read_text(encoding="utf-8")


def test_install_tasks_passes_schedule_args_individually() -> None:
    """**真机验证发现的 bug 回归测试**。

    曾把 '/sc minute /mo 30' 作为**一个**字符串元素传给 schtasks，
    PowerShell 会把它整体当作单个参数交给原生 exe，schtasks 报
    「无效参数/选项」。必须让每个开关各自成为独立参数。

    语法检查与单元测试都发现不了这个问题——只有真跑一遍才会暴露。
    """
    text = INSTALL_SCRIPT.read_text(encoding="ascii")
    # 只看实际代码，排除注释
    code = chr(10).join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )

    # 调用处必须逐个传开关
    assert "/sc minute /mo $IntervalMinutes" in code
    assert "/sc daily /st $DigestTime" in code
    assert "/sc weekly /d SUN /st 03:00" in code

    # 不得把多个开关塞进同一个字符串字面量
    assert "'/sc " not in code, "不得把多个开关拼成一个字符串"

    # 函数应以剩余参数接收开关
    assert "ValueFromRemainingArguments" in code

def test_install_tasks_fatal_paths_control_exit_code() -> None:
    """致命分支必须真正返回非零退出码。

    曾用 Write-Error，而 $ErrorActionPreference='Stop' 让它抛终止异常，
    后面的 ``exit 2`` 永不执行 → 脚本"失败"却返回 0（真机验证发现）。
    """
    text = INSTALL_SCRIPT.read_text(encoding="ascii")

    # 只检查**实际代码**，排除注释行（注释里提到 Write-Error 是在解释原因）
    code_lines = [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    code = chr(10).join(code_lines)

    assert "Write-Error" not in code, (
        "致命输出不得用 Write-Error：Stop 偏好会让它抛终止异常，"
        "后面的 exit N 永不执行，脚本失败却返回 0"
    )
    assert "[Console]::Error.WriteLine" in code


# ── 登录补处理（关机期间收到的邮件）─────────────────────────
#
# 这两条对应实测发现的两个 bug：
#
# 1. ``$LogonDelayMinutes`` / ``$NoLogon`` 被使用却未在 param 块声明。
#    PowerShell 里未定义变量是 $null，``'{0:00}:00' -f $null`` 得到 ``:00``，
#    而 schtasks 要求 ``mmmm:ss``（实测 ``:00`` 与 ``02:00`` 都被拒绝，
#    只有 ``0002:00`` 通过）。结果：at-logon 任务永远注册不上，且报的是
#    一句看不出原因的「/DELAY 值无效」。
# 2. 非管理员账号下 ``schtasks /sc onlogon`` 返回「拒绝访问」——必须提供
#    不需要提权的替代路径（每用户启动文件夹），否则使用者只能自己去提权。

EXE_INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install-tasks-exe.ps1"


def _code_only(script: Path) -> str:
    """只取实际代码，排除注释行（注释里常提到这些名字是在解释原因）。"""
    text = script.read_text(encoding="ascii")
    return chr(10).join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("script", [INSTALL_SCRIPT, EXE_INSTALL_SCRIPT])
def test_logon_delay_variables_are_declared(script: Path) -> None:
    """用到的 LogonDelayMinutes / NoLogon 必须在 param 块里声明。

    未声明时 PowerShell 取 $null：延迟格式化出来是 ``:00``（非法），
    at-logon 任务静默注册失败。
    """
    code = _code_only(script)
    if "LogonDelayMinutes" not in code and "NoLogon" not in code:
        return  # 该脚本不涉及登录任务

    assert "[int]$LogonDelayMinutes" in code, "$LogonDelayMinutes 未声明"
    assert "[switch]$NoLogon" in code, "$NoLogon 未声明"


@pytest.mark.parametrize("script", [INSTALL_SCRIPT, EXE_INSTALL_SCRIPT])
def test_logon_delay_uses_four_digit_minutes(script: Path) -> None:
    """延迟必须是 ``mmmm:ss``（4 位分钟），``{0:00}`` 产出的 ``02:00`` 非法。

    实测：``/delay 02:00`` → 「/DELAY 值无效(延迟应该采用 mmmm:ss 格式)」；
    ``0002:00`` 才被接受。
    """
    code = _code_only(script)
    if "onlogon" not in code:
        return

    assert "'{0:0000}:00'" in code, "延迟格式化必须用 4 位分钟（mmmm:ss）"
    assert "'{0:00}:00'" not in code, "2 位分钟会被 schtasks 拒绝"


@pytest.mark.parametrize("script", [INSTALL_SCRIPT, EXE_INSTALL_SCRIPT])
def test_logon_task_falls_back_without_admin(script: Path) -> None:
    """无管理员权限时必须回退到启动文件夹，而不是只报错误。

    ``schtasks /sc onlogon`` 对非提权账号返回「拒绝访问」（实测）。要求使用者
    为了一个个人邮件工具去提权，成本高于收益；每用户启动文件夹无需提权且
    效果相同。
    """
    code = _code_only(script)
    assert "IsInRole" in code, "必须先检测是否具备管理员权限"
    assert "Startup" in code, "缺少启动文件夹回退路径"
    assert "WScript.Shell" in code, "应生成 .lnk（避免每次登录闪出控制台窗口）"


@pytest.mark.parametrize("script", [INSTALL_SCRIPT, EXE_INSTALL_SCRIPT])
def test_startup_helpers_defined_before_use(script: Path) -> None:
    """``Remove-StartupEntry`` 的定义必须出现在 ``-Remove`` 分支**之前**。

    PowerShell 只在定义语句执行时绑定函数，定义在后面的函数在先前代码里
    调用不到（实测报「无法将...识别为 cmdlet」）。而 ``-Remove`` 分支在
    脚本前部，所以清理逻辑会直接失败。
    """
    code = _code_only(script)
    if "Remove-StartupEntry" not in code:
        return

    definition = code.index("function Remove-StartupEntry")
    # 第一个实际调用点（排除定义行本身）
    call = code.index("Remove-StartupEntry", definition + len("function Remove-StartupEntry"))
    assert definition < call, "调用点必须在定义之后"


@pytest.mark.parametrize("script", [INSTALL_SCRIPT, EXE_INSTALL_SCRIPT])
def test_startup_entry_is_removed(script: Path) -> None:
    """``-Remove`` 必须也清掉启动文件夹里的快捷方式。

    否则「卸载」之后每次登录仍会跑一次——而使用者以为已经移除了。
    """
    code = _code_only(script)
    if "New-StartupEntry" not in code:
        return
    assert "Remove-StartupEntry" in code, "卸载路径未清理启动项"
