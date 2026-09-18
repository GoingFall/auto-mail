"""配置路径必须锚到应用基目录，而不是进程工作目录。

这一条对图形界面是**前提条件**：从桌面快捷方式启动时工作目录是别处
（常见 ``C:\\Windows`` 或 ``C:\\Windows\\System32``）。若路径按 CWD 解析，
就会出现「读不到配置、数据库指向错误目录」，甚至像实测那样直接报
``[WinError 5] 拒绝访问: 'data'``——而使用者的数据其实就在 exe 旁边。

对命令行是**行为等价**的：源码运行时 ``app_base_dir()`` 就是当前工作目录；
打包后经 ``auto-mail-run.cmd`` / ``run.ps1`` 启动时它等于 exe 目录，也与
工作目录一致。因此这个改动不改变既有使用方式。
"""

from __future__ import annotations

from pathlib import Path

from automail.settings import (
    _PATH_FIELDS,
    Settings,
    app_base_dir,
    env_file_path,
)


def _switch_base(monkeypatch, base: Path) -> None:
    """把应用基目录指向 ``base``，并把工作目录换到别处。

    两者都换是刻意的：只有"基目录 ≠ 工作目录"才暴露路径解析问题，
    在正常使用（两者相同）时问题不可见。
    """
    monkeypatch.setenv("AUTOMAIL_HOME", str(base))
    monkeypatch.chdir(base.parent)


# ══════════════════════════════════════════════════════════════
# 路径字段清单的完整性
# ══════════════════════════════════════════════════════════════


def test_path_field_list_covers_every_path_field() -> None:
    """``_PATH_FIELDS`` 必须覆盖 ``Settings`` 里**全部** Path 字段。

    漏掉一个就会产生"某些路径跟着 CWD 跑"的诡异行为，而且只在特定启动方式下
    出现，极难定位。用自省而不是人眼核对——新增字段时这条会自动失败。

    ``migrations_dir`` 是**刻意的例外**：它指向打包进 exe 的资源目录，
    由 :func:`default_migrations_dir` 自行探测，语义上不属于"用户数据"。
    """
    declared = {
        name
        for name, field in Settings.model_fields.items()
        if "Path" in str(field.annotation)
    }
    intentionally_excluded = {"migrations_dir"}

    uncovered = declared - set(_PATH_FIELDS) - intentionally_excluded
    assert not uncovered, (
        f"这些 Path 字段没有被锚定到基目录：{sorted(uncovered)}。"
        "请加入 _PATH_FIELDS，否则它们会按进程工作目录解析。"
    )


# ══════════════════════════════════════════════════════════════
# 相对路径 → 基目录下绝对路径
# ══════════════════════════════════════════════════════════════


def test_env_relative_paths_resolve_against_base(tmp_path, monkeypatch) -> None:
    """``.env`` 里的相对路径（模板默认就是相对的）必须锚到基目录。

    ``.env.example`` 里写着 ``DATA_DIR=data``、``GOOGLE_CREDENTIALS_FILE=credentials.json``，
    优先级高于字段默认值——所以只在 ``default_factory`` 里锚定是**不够的**。
    这是实际踩到的 bug：frozen exe 从 ``C:\\Windows`` 启动时试图创建
    ``C:\\Windows\\data`` 并报拒绝访问。
    """
    base = tmp_path / "app"
    base.mkdir()
    (base / ".env").write_text(
        "DATA_DIR=data\n"
        "OUT_DIR=out\n"
        "LOG_DIR=logs\n"
        "GOOGLE_CREDENTIALS_FILE=credentials.json\n"
        "GOOGLE_TOKEN_FILE=token.json\n",
        encoding="utf-8",
    )
    _switch_base(monkeypatch, base)

    settings = Settings()

    for name in _PATH_FIELDS:
        value = Path(getattr(settings, name))
        assert value.is_absolute(), f"{name} 应为绝对路径，实际 {value}"
        assert value.parent == base or value == base / value.name, (
            f"{name} 应位于基目录下，实际 {value}"
        )

    assert settings.data_dir == base / "data"
    assert settings.google_credentials_file == base / "credentials.json"


def test_absolute_env_paths_are_respected(tmp_path, monkeypatch) -> None:
    """``.env`` 里显式写的**绝对**路径必须原样尊重（不能强行搬回基目录）。

    使用者把数据放到别的盘（例如不受云盘同步的目录）是合理需求。
    """
    base = tmp_path / "app"
    base.mkdir()
    other = tmp_path / "elsewhere" / "mydata"
    (base / ".env").write_text(f"DATA_DIR={other}\n", encoding="utf-8")
    _switch_base(monkeypatch, base)

    assert Settings().data_dir == other


def test_derived_paths_stay_inside_data_dir(tmp_path, monkeypatch) -> None:
    """派生路径（数据库、备份）也要跟着走，不能有漏网的。"""
    base = tmp_path / "app"
    base.mkdir()
    _switch_base(monkeypatch, base)

    settings = Settings()
    assert settings.db_path == base / "data" / "automail.db"
    assert settings.backup_dir == base / "data" / "backups"
    assert settings.db_path.is_absolute()


def test_env_file_path_is_absolute() -> None:
    """``.env`` 自身的位置也要绝对——否则从别的目录启动就读不到配置。"""
    assert env_file_path().is_absolute()
    assert env_file_path().name == ".env"
    assert env_file_path().parent == app_base_dir()


def test_env_file_is_found_from_other_working_directory(tmp_path, monkeypatch) -> None:
    """**端到端**：工作目录在别处时仍要读到基目录下的 ``.env``。

    这是图形界面场景的最小复现——没有这一条，GUI 启动后会显示「未配置」。
    """
    base = tmp_path / "app"
    base.mkdir()
    (base / ".env").write_text("IMAP_USER=from-base-dir@example.com\n", encoding="utf-8")
    _switch_base(monkeypatch, base)

    assert Settings().imap_user == "from-base-dir@example.com"


def test_switch_base_actually_moves_cwd(tmp_path, monkeypatch) -> None:
    """守护上面那些用例的前提：基目录与工作目录确实不同。

    如果两者相同，所有断言都会因为"恰好正确"而通过，测试就没有意义了。
    """
    base = tmp_path / "app"
    base.mkdir()
    _switch_base(monkeypatch, base)
    assert Path.cwd() != base


# ══════════════════════════════════════════════════════════════
# 与旧行为等价
# ══════════════════════════════════════════════════════════════


def test_source_run_equivalent_to_old_behavior(tmp_path, monkeypatch) -> None:
    """源码运行时基目录 = 当前工作目录 → 与改动前完全一致。

    这是"不改变既有使用方式"的依据：命令行把它当项目根目录用，
    行为没有任何变化。
    """
    monkeypatch.delenv("AUTOMAIL_HOME", raising=False)
    monkeypatch.chdir(tmp_path)

    assert app_base_dir() == tmp_path
    settings = Settings()
    assert settings.data_dir == tmp_path / "data"
    assert settings.out_dir == tmp_path / "out"


def test_automa_home_moves_config_too(tmp_path, monkeypatch) -> None:
    """``AUTOMAIL_HOME`` 现在也会移动配置与凭据文件。

    此前它只影响 ``data/out/logs``，``.env`` 与凭据仍按 CWD 找——那是不一致的：
    使用者以为把整个应用数据搬走了，配置却还在原处。文档里已写明这一变化。
    """
    home = tmp_path / "moved"
    home.mkdir()
    _switch_base(monkeypatch, home)

    assert app_base_dir() == home
    assert env_file_path() == home / ".env"
    assert Settings().google_token_file == home / "token.json"


# ══════════════════════════════════════════════════════════════
# doctor 必须与实际加载路径一致
# ══════════════════════════════════════════════════════════════


def test_doctor_env_check_uses_same_path_as_loader(tmp_path, monkeypatch) -> None:
    """**实测 bug**：doctor 曾硬编码 ``Path(".env")``（相对 CWD），于是配置
    其实已经正确加载（凭据都能读到），doctor 却报「未找到 .env」——
    一个会把人带偏的假警报，而且只在从别的目录启动时出现。

    doctor 的作用就是让人判断「环境对不对」，它自己报错最伤信任。
    """
    from automail.doctor import check_env_file
    from automail.models import ReadinessStatus

    base = tmp_path / "app"
    base.mkdir()
    (base / ".env").write_text("IMAP_USER=a@b.com\n", encoding="utf-8")
    _switch_base(monkeypatch, base)

    item = check_env_file()

    assert item.status is ReadinessStatus.OK, (
        f"配置在 {base / '.env'}，doctor 却报 {item.status}: {item.detail}"
    )
    assert str(base) in item.detail, "应指出实际路径，便于排查"


def test_doctor_env_check_reports_missing_with_real_path(tmp_path, monkeypatch) -> None:
    """没有配置文件时要报 SKIPPED，并给出**实际查找的路径**。"""
    from automail.doctor import check_env_file
    from automail.models import ReadinessStatus

    base = tmp_path / "app"
    base.mkdir()
    _switch_base(monkeypatch, base)

    item = check_env_file()
    assert item.status is ReadinessStatus.SKIPPED
    assert str(base) in item.detail


# ══════════════════════════════════════════════════════════════
# 输出编码（中文控制台）
# ══════════════════════════════════════════════════════════════


def test_make_output_lenient_prevents_emoji_crash() -> None:
    """**实测 bug**：中文 Windows 控制台默认 GBK，邮件主题里的 emoji 会让
    渲染抛 ``UnicodeEncodeError``，整个命令以「未预期的错误」结束——
    而它本可以正常显示其余内容（实测 ``stats`` 因此完全不可用）。
    """
    import io
    import sys

    from automail.__main__ import _make_output_lenient

    class _StrictGbkStream(io.TextIOWrapper):
        """模拟中文控制台：GBK，且遇到无法编码的字符就抛错。"""

        def __init__(self) -> None:
            super().__init__(io.BytesIO(), encoding="gbk", errors="strict")

    stream = _StrictGbkStream()
    real_stdout = sys.stdout
    try:
        sys.stdout = stream
        _make_output_lenient()
        stream.write("信用卡 💳 通知")  # 修复前这里会抛 UnicodeEncodeError
        stream.flush()
    finally:
        sys.stdout = real_stdout


def test_lenient_output_keeps_native_encoding() -> None:
    """**回归（实测乱码）**：只放宽 errors，**不改 encoding**。

    早先的实现把编码强制改成 UTF-8。崩溃确实没了，但 GBK 控制台会把 UTF-8
    字节按 GBK 解码，中文全变乱码（``邮件`` → ``閭�浠�``）——修好了崩溃、
    弄坏了正常显示，而且比崩溃更隐蔽（看起来只是"字体问题"）。

    这条用例断言编码保持原样：控制台是 GBK 就还是 GBK。
    """
    import io
    import sys

    from automail.__main__ import _make_output_lenient

    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    real_stdout = sys.stdout
    try:
        sys.stdout = stream
        _make_output_lenient()
        assert stream.encoding.lower().replace("-", "") == "gbk", (
            f"编码不应被改成 {stream.encoding}——那会让 GBK 控制台显示乱码"
        )
        assert stream.errors == "replace", "无法编码的字符应降级"
    finally:
        sys.stdout = real_stdout


def test_lenient_output_emoji_becomes_placeholder_not_crash() -> None:
    """降级后的具体表现：emoji 变成占位符，中文仍然正常。

    这是取舍的核心——**显示成问号远好过整条命令失败**，也好过满屏乱码。
    """
    import io
    import sys

    from automail.__main__ import _make_output_lenient

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
    real_stdout = sys.stdout
    try:
        sys.stdout = stream
        _make_output_lenient()
        stream.write("邮件 💳 通知")
        stream.flush()
    finally:
        sys.stdout = real_stdout

    written = buffer.getvalue().decode("gbk")
    assert "邮件" in written, f"中文必须正常显示：{written!r}"
    assert "通知" in written
    assert "💳" not in written, "emoji 应降级而非原样写入"
    assert "?" in written, "降级为占位符"


def test_make_output_lenient_tolerates_streams_without_reconfigure() -> None:
    """某些流（例如被替换成 StringIO）没有 ``reconfigure``——不能因此崩。

    测试与 CI 里常见这种替换。
    """
    import io
    import sys

    from automail.__main__ import _make_output_lenient

    real_stdout = sys.stdout
    try:
        sys.stdout = io.StringIO()  # 没有 reconfigure
        _make_output_lenient()  # 不应抛
    finally:
        sys.stdout = real_stdout


def test_cli_main_also_makes_output_lenient() -> None:
    """``console_scripts`` 入口（``automail`` 命令）不经过 ``__main__``，
    因此它也必须自己处理编码。

    漏掉它的后果与上面一样：只要库里有带 emoji 的邮件，命令就崩。
    """
    text = (
        Path(__file__).resolve().parents[1] / "src" / "automail" / "cli.py"
    ).read_text(encoding="utf-8")
    assert "_make_output_lenient" in text, "cli.main 必须处理控制台编码"
