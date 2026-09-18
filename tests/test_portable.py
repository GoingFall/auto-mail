"""打包与便携版支持测试。

**这里的用例都来自真实打包/运行中踩到的问题**：

* `__main__.py` 用相对导入 → 打包后报 "no known parent package"
* exe 的数据目录随工作目录漂移 → 双击启动时数据落到意想不到的地方
* `.env.example` 落在 `_internal/` 而非 exe 同级 → 首次引导说"生成不了 .env"
* openai 3.x 依赖 `httpx2` 而非 `httpx` → 写错名字导致 LLM 在 exe 里不可用
* 发现"已有配置"时把目标目录自己也算作来源 → 提示"可复制到本目录"的噪音
* 双击失败时窗口一闪而过 → 但若不加区分地暂停，计划任务会永久挂住
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from automail.portable import (
    LayoutReport,
    describe_missing,
    ensure_portable_layout,
    find_config_sources,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ──────────────────────────────────────────────────────────────
# 入口点
# ──────────────────────────────────────────────────────────────


def test_main_module_exists() -> None:
    """必须有 `__main__.py`：打包入口与 `python -m automail` 共用。"""
    assert (PROJECT_ROOT / "src" / "automail" / "__main__.py").is_file()


def test_main_module_uses_absolute_imports() -> None:
    """**回归测试**：入口脚本不得用相对导入。

    PyInstaller 把入口脚本当**顶层脚本**，此时它没有父包，
    `from .cli import ...` 会报 "attempted relative import with no known
    parent package"——只有在打包后才能发现。
    """
    text = (PROJECT_ROOT / "src" / "automail" / "__main__.py").read_text(encoding="utf-8")
    # 只看代码行，排除注释——注释里解释「为什么不用相对导入」是必要的
    code = chr(10).join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "from .cli import" not in code, "入口脚本不得用相对导入"
    assert "from automail.cli import" in code


def test_console_pause_only_for_own_console() -> None:
    """暂停逻辑必须区分「双击」与「计划任务」。

    若用 ``stdin.isatty()`` 判断是不可靠的：Git Bash 等伪终端下即使重定向
    stdin 也可能返回 True。而误判为交互式会让计划任务在 ``input()`` 上
    **永久挂住**——比看不到错误严重得多。

    因此用 Windows 的 ``GetConsoleProcessList``：控制台只附着本进程时才暂停。
    """
    text = (PROJECT_ROOT / "src" / "automail" / "__main__.py").read_text(encoding="utf-8")
    assert "GetConsoleProcessList" in text, "应使用 GetConsoleProcessList 判断双击"

    # 用 AST 取出函数**实际编译出的名字引用**，避免把解释性注释算进去
    import ast

    tree = ast.parse(text)
    referenced = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    } | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert "isatty" not in referenced, (
        "isatty 在伪终端下不可靠，不应在可执行代码中调用"
    )


# ──────────────────────────────────────────────────────────────
# 路径解析（打包后最关键的一点）
# ──────────────────────────────────────────────────────────────


def test_app_base_dir_honours_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AUTOMAIL_HOME`` 可显式指定数据根目录。

    用途：把数据放到不受云盘同步的目录（凭据与邮件片段不该进网盘）。
    """
    from automail.settings import app_base_dir

    monkeypatch.setenv("AUTOMAIL_HOME", "D:/custom/home")
    assert app_base_dir() == Path("D:/custom/home")


def test_app_base_dir_is_cwd_when_not_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    from automail.settings import app_base_dir

    monkeypatch.delenv("AUTOMAIL_HOME", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert app_base_dir() == Path.cwd()


def test_app_base_dir_is_exe_dir_when_frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """**核心行为**：打包后数据目录必须是 exe 所在目录。

    否则双击运行时工作目录可能是不确定的位置（甚至 System32），
    使用者会找不到自己的数据。
    """
    from automail.settings import app_base_dir

    monkeypatch.delenv("AUTOMAIL_HOME", raising=False)
    exe = tmp_path / "auto-mail.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert app_base_dir() == tmp_path


def test_default_paths_derive_from_base_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    from automail.settings import Settings

    monkeypatch.setenv("AUTOMAIL_HOME", "D:/x/home")
    settings = Settings(_env_file=None)
    assert settings.data_dir == Path("D:/x/home") / "data"
    assert settings.db_path == Path("D:/x/home") / "data" / "automail.db"


def test_migrations_dir_exists_in_source_tree() -> None:
    """迁移目录必须能被找到——找不到会让迁移静默跳过（数据库没有表）。"""
    from automail.settings import Settings

    settings = Settings(_env_file=None)
    assert settings.migrations_dir.is_dir()
    assert list(settings.migrations_dir.glob("*.sql"))


# ──────────────────────────────────────────────────────────────
# 便携目录引导
# ──────────────────────────────────────────────────────────────


def test_ensure_layout_creates_dirs_and_env(tmp_path: Path) -> None:
    template = tmp_path / "template.env"
    template.write_text("IMAP_USER=\n", encoding="utf-8")
    base = tmp_path / "app"
    base.mkdir()

    report = ensure_portable_layout(base, template=template)

    assert report.env_created is True
    assert (base / ".env").is_file()
    assert (base / ".env").read_text(encoding="utf-8") == "IMAP_USER=\n"
    for name in ("data", "out", "logs"):
        assert (base / name).is_dir()


def test_ensure_layout_does_not_overwrite_existing_env(tmp_path: Path) -> None:
    """**不得覆盖已有 .env**——那里面是使用者的密钥与配置。"""
    base = tmp_path / "app"
    base.mkdir()
    env = base / ".env"
    env.write_text("IMAP_USER=me@163.com\n", encoding="utf-8")
    template = tmp_path / "template.env"
    template.write_text("IMAP_USER=\n", encoding="utf-8")

    report = ensure_portable_layout(base, template=template)

    assert report.env_created is False
    assert env.read_text(encoding="utf-8") == "IMAP_USER=me@163.com\n"


def test_ensure_layout_reports_missing_credentials(tmp_path: Path) -> None:
    base = tmp_path / "app"
    base.mkdir()
    report = ensure_portable_layout(base)
    assert "credentials.json" in report.missing
    assert "token.json" in report.missing
    assert report.ready is False


def test_ensure_layout_idempotent(tmp_path: Path) -> None:
    base = tmp_path / "app"
    base.mkdir()
    first = ensure_portable_layout(base)
    second = ensure_portable_layout(base)
    assert first.created_dirs, "首次应创建目录"
    assert second.created_dirs == [], "第二次不应重复创建"


# ──────────────────────────────────────────────────────────────
# 配置发现
# ──────────────────────────────────────────────────────────────


def test_find_config_sources_excludes_target_dir(tmp_path: Path) -> None:
    """**回归测试**：目标目录不能算作「发现的来源」。

    实测踩到：首次引导生成 .env 后，紧接着提示
    「发现已有 .env：<本目录>\\.env（可复制到本目录）」——纯属噪音，
    会让人以为配置已经就绪。
    """
    base = tmp_path / "app"
    base.mkdir()
    (base / ".env").write_text("x", encoding="utf-8")

    found = find_config_sources(base, search_roots=[base])
    assert ".env" not in found, "不得把目标目录自己视为来源"


def test_find_config_sources_finds_elsewhere(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "token.json").write_text("{}", encoding="utf-8")

    base = tmp_path / "app"
    base.mkdir()

    found = find_config_sources(base, search_roots=[source])
    assert found.get("token.json") == source / "token.json"


def test_find_config_sources_ignores_missing_roots(tmp_path: Path) -> None:
    base = tmp_path / "app"
    base.mkdir()
    assert find_config_sources(base, search_roots=[tmp_path / "nope"]) == {}


def test_describe_missing_is_actionable() -> None:
    """提示必须可执行——不能只说「缺少 token.json」。"""
    report = LayoutReport(base_dir=Path("."), missing=["token.json"])
    lines = describe_missing(report)
    assert any("auth" in line for line in lines)


def test_describe_missing_mentions_discovered_files() -> None:
    report = LayoutReport(
        base_dir=Path("."),
        missing=["token.json"],
        discovered={"token.json": Path("D:/dev/auto-mail/token.json")},
    )
    lines = describe_missing(report)
    assert any("token.json" in line and "D:/dev/auto-mail" in line.replace("\\", "/") for line in lines)


# ──────────────────────────────────────────────────────────────
# 打包配置
# ──────────────────────────────────────────────────────────────


def test_spec_exists_and_collects_migrations() -> None:
    spec = (PROJECT_ROOT / "automail.spec").read_text(encoding="utf-8")
    assert "migrations" in spec, "必须收集迁移脚本"
    assert "tzdata" in spec, "必须收集 tzdata（Windows 无系统时区库）"


def test_spec_uses_correct_openai_http_dependency() -> None:
    """**回归测试**：openai 3.x 依赖的是 ``httpx2`` 而不是 ``httpx``。

    写错名字的后果：打包时报 "Hidden import 'httpx' not found"，
    且 LLM 功能在 exe 里 ImportError——而源码运行时完全正常。
    """
    spec = (PROJECT_ROOT / "automail.spec").read_text(encoding="utf-8")
    assert "httpx2" in spec
    # 断言不存在孤立的 httpx 声明（httpx2 会匹配到 "httpx"，因此逐行判断）
    for line in spec.splitlines():
        stripped = line.strip().strip(",").strip('"')
        assert stripped != "httpx", "不应声明 httpx（实际包名是 httpx2）"


def test_actual_httpx_dependency_name() -> None:
    """直接从 openai 的元数据确认依赖名，避免 spec 与事实漂移。"""
    import importlib.metadata as md

    try:
        requires = md.requires("openai") or []
    except md.PackageNotFoundError:  # pragma: no cover
        pytest.skip("openai 未安装")

    http_deps = [r for r in requires if "httpx" in r.lower()]
    assert http_deps, "openai 应有 HTTP 依赖"
    assert any("httpx2" in r.lower() for r in http_deps), (
        f"openai 的 HTTP 依赖名变了，spec 需要同步更新：{http_deps}"
    )


def test_build_scripts_exist_and_are_ascii() -> None:
    for name in ("build-exe.ps1", "install-tasks-exe.ps1"):
        path = PROJECT_ROOT / "scripts" / name
        assert path.is_file(), f"缺少 {name}"
        raw = path.read_bytes()
        offenders = [i for i, b in enumerate(raw) if b > 0x7F]
        assert not offenders, f"{name} 含非 ASCII 字节（PowerShell 5.1 会解析错）"


def test_install_tasks_exe_sets_working_directory() -> None:
    """关键：计划任务必须把工作目录设成 exe 目录。

    冻结模式下 ``app_base_dir()`` 是 exe 所在目录，因此 ``data/``、``.env``
    都在那里。``schtasks /TR`` 无法直接设置工作目录，必须用
    ``cmd /c "cd /d <dir> && <exe> ..."`` 包一层。
    """
    text = (PROJECT_ROOT / "scripts" / "install-tasks-exe.ps1").read_text(encoding="ascii")
    assert "cd /d" in text, "必须以 cd /d 设置工作目录"
    assert "cmd /c" in text
    assert "auto-mail.exe" in text


def test_install_tasks_exe_guards_interval() -> None:
    text = (PROJECT_ROOT / "scripts" / "install-tasks-exe.ps1").read_text(encoding="ascii")
    assert "IntervalMinutes -lt 15" in text


def test_entry_point_importable() -> None:
    """`python -m automail` 应可导入（打包入口与之一致）。"""
    spec = importlib.util.find_spec("automail.__main__")
    assert spec is not None


def test_spec_does_not_exclude_stdlib_test_modules() -> None:
    """**回归测试**：不得排除 ``unittest`` / ``test`` 这类标准库模块。

    实测踩到：排除了 ``unittest`` 后，日历功能在 exe 里报
    ``No module named 'unittest'``——因为 ``httplib2/iri2uri.py`` 在
    **模块导入时**就执行 ``import unittest``（自测代码放在模块级），
    而 httplib2 是 googleapiclient 的默认传输层。

    这类错误只在打包后、且只在真正调用该功能时才暴露。
    """
    spec = (PROJECT_ROOT / "automail.spec").read_text(encoding="utf-8")
    code = chr(10).join(
        line for line in spec.splitlines() if not line.lstrip().startswith("#")
    )
    for module in ("unittest", "sqlite3.test"):
        assert f'"{module}"' not in code, (
            f"不得排除 {module}：标准库模块可能被依赖链在导入期使用"
        )


def test_httplib2_imports_unittest_at_module_level() -> None:
    """固化上面那条判断的**依据**：确认 httplib2 确实在导入期需要 unittest。

    如果哪天 httplib2 不再这样做了，这条测试会失败，提示我们可以重新评估
    是否排除 unittest。
    """
    import pathlib

    try:
        import httplib2
    except ImportError:  # pragma: no cover
        pytest.skip("httplib2 未安装")

    source = pathlib.Path(httplib2.__file__).parent / "version.py"
    _ = source
    # 直接检查 iri2uri 的模块级导入
    iri = pathlib.Path(httplib2.__file__).parent / "iri2uri.py"
    text = iri.read_text(encoding="utf-8", errors="replace")
    assert "import unittest" in text, (
        "httplib2/iri2uri.py 不再导入 unittest —— 可重新评估是否排除 unittest"
    )


def test_googleapiclient_imports_without_test_modules_available() -> None:
    """日历后端的导入链不应依赖测试专用模块。

    这里在**源码环境**下验证导入链完整；真正的冻结环境验证由
    ``scripts/build-exe.ps1`` 的冒烟测试与手动的 `digest` 调用覆盖。
    """
    import importlib

    for module in (
        "googleapiclient.discovery",
        "googleapiclient.http",
        "google_auth_httplib2",
        "httplib2",
    ):
        importlib.import_module(module)


# ──────────────────────────────────────────────────────────────
# 计划任务：包装器与权限约束
# ──────────────────────────────────────────────────────────────


def _install_exe_script() -> str:
    return (PROJECT_ROOT / "scripts" / "install-tasks-exe.ps1").read_text(encoding="ascii")


def test_uses_wrapper_cmd_not_inline_cmd_c() -> None:
    """**回归测试**：必须用包装器 .cmd，而不是把 ``cmd /c "... && ..."`` 塞进 /TR。

    实测踩到：``schtasks /create /tr 'cmd /c "cd /d X && Y"'`` 会让内嵌的
    ``&&`` 泄漏成 schtasks 自己的参数，报
    「无效参数/选项 - '&&'」。试过双引号与 ``--%`` 转义都失败。

    包装器文件绕开所有引号层级，而且使用者能直接看、直接跑。
    """
    text = _install_exe_script()
    assert "New-RunWrapper" in text, "应生成包装器脚本"
    assert "auto-mail-run.cmd" in text
    assert "%~dp0" in text, "包装器应基于自身路径定位 exe 与工作目录"


def test_wrapper_sets_working_directory_and_propagates_exit_code() -> None:
    """包装器必须 cd 到 exe 目录，并把退出码透传。

    工作目录决定 ``data/``、``out/``、``.env`` 的位置（冻结模式下
    ``app_base_dir()`` 是 exe 目录）；退出码决定计划任务能否判断成败。
    """
    text = _install_exe_script()
    assert 'cd /d "%~dp0"' in text
    assert "exit /b %ERRORLEVEL%" in text


def test_wrapper_uses_crlf_line_endings() -> None:
    """.cmd 文件必须有 CRLF 行尾。

    纯 LF 的 .cmd 在 Windows 上可能被解析错（命令粘连）。生成时要显式写
    ``\r\n``，不能依赖 PowerShell 的默认输出。
    """
    text = _install_exe_script()
    assert '`r`n' in text, "应显式使用 CRLF 拼接包装器内容"


def test_checks_admin_before_onlogon_task() -> None:
    """**环境约束**：``ONLOGON`` 任务需要管理员权限。

    非管理员账户注册会得到「拒绝访问」（已实测）。脚本必须**预先检测**并
    走一条**不需要提权**的路径，而不是抛一个无意义的权限错误，也不能因此
    把整个安装判为失败。

    回退方案是当前用户的启动文件夹：它同样在登录时运行，但不需要管理员。
    """
    text = _install_exe_script()
    assert "IsInRole" in text and "Administrator" in text
    assert "Startup" in text, "无提权时必须回退到启动文件夹"
    assert "WScript.Shell" in text, "应生成 .lnk 快捷方式"
    # 未提权时不应计入失败
    assert "Using the current user Startup folder" in text


def test_delay_format_matches_schtasks_expectation() -> None:
    """``/delay`` 必须是 ``mmmm:ss``，且分钟要 **4 位**。

    实测（本机 schtasks）：``:00`` 与 ``02:00`` 都被拒绝——
    「/DELAY 值无效(延迟应该采用 mmmm:ss 格式)」；只有 ``0002:00`` 通过，
    随后才因权限不足报「拒绝访问」（说明参数本身已被接受）。

    原先用 ``'{0:00}:00'`` 生成 ``02:00``，是**被服务端拒绝**的格式；
    这个错误在使用者那侧只表现为 at-logon 任务神秘地注册不上。
    """
    text = _install_exe_script()
    assert "'{0:0000}:00' -f $LogonDelayMinutes" in text, "必须是 4 位分钟"
    assert "'{0:00}:00'" not in text, "2 位分钟会被 schtasks 拒绝"


def test_logon_delay_minutes_is_declared() -> None:
    """``$LogonDelayMinutes`` 必须在 param 块声明，否则取 $null → ``:00``。"""
    text = _install_exe_script()
    assert "[int]$LogonDelayMinutes" in text


def test_registers_all_expected_tasks() -> None:
    text = _install_exe_script()
    for name in (
        "auto-mail startup",
        "auto-mail run",
        "auto-mail digest",
        "auto-mail audit",
        "auto-mail backup",
    ):
        assert name in text, f"应注册 {name}"


def test_remove_covers_all_tasks() -> None:
    """卸载必须覆盖全部任务，否则会留下使用者无法清理的系统状态。"""
    text = _install_exe_script()
    remove_block = text[text.index("if ($Remove)") : text.index("if (-not (Test-Path $Exe))")]
    for name in (
        "auto-mail startup",
        "auto-mail run",
        "auto-mail digest",
        "auto-mail audit",
        "auto-mail backup",
    ):
        assert name in remove_block, f"卸载应包含 {name}"
    # 启动文件夹里的快捷方式也要清掉，否则「已卸载」却仍在每次登录时运行
    assert "Remove-StartupEntry" in remove_block
