# PyInstaller 打包配置
#
# 用法（在项目根目录）：
#     .venv\Scripts\python.exe -m PyInstaller automail.spec --noconfirm
# 产物：dist/auto-mail/auto-mail.exe
#
# 为什么用 onedir 而不是 onefile：
#   * onefile 每次启动都要把整个包解压到临时目录，启动慢（本程序依赖较重）
#   * onefile 运行时的 sys._MEIPASS 是临时目录，使用者往里放 .env 没有意义，
#     而 onedir 的目录结构对使用者是可见、可维护的
#   * 升级时 onedir 只需替换目录，排查问题也更容易（能看到依赖文件）

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = Path(SPECPATH).resolve()
SRC = PROJECT_ROOT / "src"

# ── 必须显式收集的资源 ────────────────────────────────────────────
datas = []

# 1) 迁移脚本：程序运行时用 Path 读取，PyInstaller 无法静态分析出它们
datas += [(str(SRC / "automail" / "migrations"), "automail/migrations")]

# 1b) .env.example：便携版首次运行用它生成初始 .env
#     放在 exe 同目录（"."），方便使用者直接看到、编辑
datas += [(str(PROJECT_ROOT / ".env.example"), ".")]

# 2) tzdata：Windows 没有系统时区数据库，zoneinfo 依赖它。
#    它是纯数据包，不收集的话运行时会 ZoneInfoNotFoundError——
#    而且只在用到时区时才崩，属于"打包后才发现"的典型问题。
datas += collect_data_files("tzdata")

# 3) charset_normalizer 的字符集检测表（数据文件）
datas += collect_data_files("charset_normalizer")

# ── 必须显式声明的动态导入 ────────────────────────────────────────
hiddenimports = [
    # google-api-python-client 的 discovery 文档由运行时动态加载
    "googleapiclient.discovery",
    "googleapiclient.discovery_cache",
    "googleapiclient.http",
    "googleapiclient.model",
    "googleapiclient.errors",
    "google_auth_httplib2",
    "google_auth_oauthlib.flow",
    "google.oauth2.credentials",
    "google.auth.transport.requests",
    # httplib2 是 googleapiclient 的默认传输层，导入是间接的
    "httplib2",
    # dateparser 的语言数据是动态注册的
    "dateparser",
    "dateparser.languages",
    "dateparser.utils.strptime",
    # icalendar 的时区处理
    "icalendar",
    "icalendar.prop",
    # openai SDK 内部按需导入。
    # 注意：openai 3.x 依赖的是 **httpx2**（不是 httpx），
    # 写错名字会在打包时报 "Hidden import not found"，
    # 且 LLM 功能在 exe 里会 ImportError。
    "openai",
    "httpx2",
    "anyio",
    "jiter",
    "sniffio",
]

# 4) dateparser / icalendar 的子模块较多，整体收集更稳妥
hiddenimports += collect_submodules("dateparser")
hiddenimports += collect_submodules("icalendar")
hiddenimports += collect_submodules("httpx2")
hiddenimports += collect_submodules("openai")

# ── 明确排除的大包（减小体积）──────────────────────────────────────
#
# 教训：**不要排除标准库里"看起来只给测试用"的模块**。
# 曾把 ``unittest`` 排除掉，结果日历功能在 exe 里报
# ``No module named 'unittest'``——因为 ``httplib2/iri2uri.py`` 在
# **模块导入时**就执行 ``import unittest``（它把自测代码放在模块级），
# 而 httplib2 是 googleapiclient 的默认传输层。
#
# 这类错误只在打包后、且只在真正调用该功能时才暴露，代价很高。
# 因此这里只排除**确定无关**的重型第三方包。
excludes = [
    # 注意：**不要**再排除 tkinter。图形界面需要它，当初排除是因为没有 GUI。
    # 排除项的取舍原则见上方注释：只排除确定无关的重型第三方包。
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "PIL",
    "IPython",
    "jupyter",
    "pytest",
    "_pytest",
    # 仅打包期使用
    "PyInstaller",
]

# tkinter 的 Tcl/Tk 数据文件（init.tcl、ttk 主题等）由 PyInstaller 自带的
# ``hook-_tkinter.py`` 收集，**不需要**在这里 collect_data_files("tkinter")
# ——实测该调用返回 0 个条目（tkinter 是纯代码包，数据在 tcl/ 目录下），
# 写上去只是看起来在做事。
#
# 真正的验收手段是构建脚本里的 ``--selftest``：它真的建一次窗口，
# 因此能发现"数据文件没打进去"这类只在运行时暴露的问题。


a = Analysis(
    [str(SRC / "automail" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# 两个入口共用同一份 Analysis / PYZ / 依赖，只有 bootloader 的子系统不同：
#   auto-mail.exe      控制台程序 —— 计划任务据此拿退出码，必须保留
#   auto-mail-gui.exe  窗口程序   —— 双击不弹黑窗
# 共用 COLLECT 是刻意的：把 tkinter 与 Tcl/Tk 打包两遍既臃肿（约多 10MB），
# 又容易出现两份依赖不一致。两者的差异只在运行时按可执行文件名分派
# （见 src/automail/__main__.py 的 _is_gui_invocation）。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="auto-mail",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="auto-mail-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # 窗口化：不分配控制台。这是它与上面那个唯一的功能差别。
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    exe_gui,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="auto-mail",
)
