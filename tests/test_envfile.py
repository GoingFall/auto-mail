"""``.env`` 改写器的测试。

这是本项目**第一次改写使用者的配置文件**。风险不在"写不进去"，而在**静默
丢东西**：注释、空行、键顺序、程序不认识的自定义键。这类丢失往往过很久才被
发现，所以测试的重心是「除目标键外，一个字节都不许变」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from automail.envfile import (
    EnvFileError,
    blank_out,
    read_entries,
    read_value,
    update,
)

SAMPLE = """\
# 顶部说明注释
# 第二行注释

IMAP_HOST=imap.163.com
IMAP_USER=someone@163.com
IMAP_AUTH_CODE=secret-value

# ── LLM ──
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-abc

# 自定义键（程序不认识的也要保留）
MY_CUSTOM_FLAG=1
"""


def _write(tmp_path: Path, text: str = SAMPLE, name: str = ".env") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════
# 读取
# ══════════════════════════════════════════════════════════════


def test_read_entries_parses_values(tmp_path) -> None:
    entries = read_entries(_write(tmp_path))
    assert entries["IMAP_USER"].value == "someone@163.com"
    assert entries["LLM_API_KEY"].value == "sk-abc"
    assert entries["MY_CUSTOM_FLAG"].value == "1"


def test_read_entries_ignores_commented_assignments(tmp_path) -> None:
    """注释里的 ``KEY=...`` 不是真值。

    ``.env.example`` 就是用 ``# HTTPS_PROXY=`` 这种写法标注可选键的——若把它
    当赋值读出来，程序会以为使用者配了代理。
    """
    path = _write(tmp_path, "# HTTPS_PROXY=\n#HH=1\nREAL=2\n")
    entries = read_entries(path)
    assert "HTTPS_PROXY" not in entries
    assert "HH" not in entries
    assert entries["REAL"].value == "2"


def test_read_entries_missing_file_is_empty(tmp_path) -> None:
    assert read_entries(tmp_path / "nope.env") == {}
    assert read_value(tmp_path / "nope.env", "ANY", "fallback") == "fallback"


def test_read_entries_rejects_duplicate_keys(tmp_path) -> None:
    """**重复键必须报错，不能猜哪个生效。**

    真实生效顺序由 pydantic-settings 决定，靠猜没用；宁可让人先清理。
    报错信息要指出**具体行号**，否则在一个几百行的文件里根本找不到。
    """
    path = _write(tmp_path, "A=1\nB=2\nA=3\n")
    with pytest.raises(EnvFileError) as exc:
        read_entries(path)
    message = str(exc.value)
    assert "A" in message
    assert "1" in message and "3" in message, "应指出两处行号"


def test_read_entries_unquotes(tmp_path) -> None:
    entries = read_entries(_write(tmp_path, 'A="x y"\nB=\'p q\'\nC=plain\n'))
    assert entries["A"].value == "x y"
    assert entries["B"].value == "p q"
    assert entries["C"].value == "plain"


def test_read_entries_keeps_export_prefix(tmp_path) -> None:
    entries = read_entries(_write(tmp_path, "export A=1\n"))
    assert entries["A"].value == "1"


# ══════════════════════════════════════════════════════════════
# 改写：只动目标，其余逐字保留
# ══════════════════════════════════════════════════════════════


def test_update_changes_only_target_line(tmp_path) -> None:
    """**核心保证**：只有目标键那一行变，其余全部不动。

    这是本模块存在的全部意义。逐行比对而不是只数注释数量——那样会漏掉
    空行、顺序、缩进之类的丢失。
    """
    path = _write(tmp_path)
    before = path.read_text(encoding="utf-8").splitlines()

    update(path, {"IMAP_AUTH_CODE": "new-value"})

    after = path.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after), "行数不得变化"

    diffs = [
        i
        for i, (b, a) in enumerate(zip(before, after, strict=True))
        if b != a
    ]
    assert len(diffs) == 1, f"只应一行变化，实际变化行：{[i + 1 for i in diffs]}"
    # 直接断言那一行的内容，而不是硬编码行号 —— 避免测试样例调整后失效
    assert before[diffs[0]] == "IMAP_AUTH_CODE=secret-value"
    assert after[diffs[0]] == "IMAP_AUTH_CODE=new-value"


def test_update_preserves_all_comments(tmp_path) -> None:
    path = _write(tmp_path)
    before = path.read_text(encoding="utf-8")
    update(path, {"LLM_API_KEY": "sk-new"})
    after = path.read_text(encoding="utf-8")

    def comments(text: str) -> list[str]:
        return [line for line in text.splitlines() if line.strip().startswith("#")]

    assert comments(before) == comments(after), "注释必须逐字保留（含顺序）"


def test_update_preserves_unknown_keys(tmp_path) -> None:
    """程序不认识的自定义键必须原样保留。

    使用者的 ``.env`` 里可能有别的东西（本项目加过的 ZZ_* 就是例子），
    改写时丢掉它们等于替人做决定。
    """
    path = _write(tmp_path)
    update(path, {"IMAP_AUTH_CODE": "x"})
    entries = read_entries(path)
    assert entries["MY_CUSTOM_FLAG"].value == "1"


def test_update_appends_missing_key(tmp_path) -> None:
    path = _write(tmp_path)
    update(path, {"MARK_READ_POLICY": "resolved"})
    entries = read_entries(path)
    assert entries["MARK_READ_POLICY"].value == "resolved"
    # 原有键一个都不能少
    assert entries["IMAP_USER"].value == "someone@163.com"


def test_update_is_idempotent(tmp_path) -> None:
    """写入相同值 → 不产生改动、不产生备份。

    幂等不只是省事：没有它，每次保存设置都会堆积一份备份、
    并把文件时间戳刷新，掩盖真正的改动。
    """
    path = _write(tmp_path)
    update(path, {"IMAP_AUTH_CODE": "same"}, backup_dir=tmp_path / "bk")
    first = path.read_text(encoding="utf-8")

    result = update(path, {"IMAP_AUTH_CODE": "same"}, backup_dir=tmp_path / "bk")
    assert result.changed == {}
    assert result.backup is None
    assert path.read_text(encoding="utf-8") == first


def test_update_empty_values_dict_is_noop(tmp_path) -> None:
    path = _write(tmp_path)
    before = path.read_text(encoding="utf-8")
    update(path, {})
    assert path.read_text(encoding="utf-8") == before


def test_update_quotes_values_needing_it(tmp_path) -> None:
    """含空格或 ``#`` 的值必须加引号，否则会被截断/当成注释（静默改错值）。"""
    path = _write(tmp_path)
    update(path, {"A": "has space", "B": "has#hash"})
    entries = read_entries(path)
    assert entries["A"].value == "has space"
    assert entries["B"].value == "has#hash"


def test_update_preserves_crlf(tmp_path) -> None:
    """CRLF 文件改写后仍是 CRLF（Windows 上手工编辑过的 .env 常见）。"""
    path = tmp_path / "crlf.env"
    path.write_bytes(b"A=1\r\nB=2\r\n")
    update(path, {"A": "9"})
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"A=9\r\n" in raw


def test_update_does_not_touch_file_on_duplicate_key(tmp_path) -> None:
    """重复键报错时**不得**留下半成品——错误要在动磁盘之前抛出。"""
    path = _write(tmp_path, "A=1\nA=2\n")
    original = path.read_text(encoding="utf-8")
    with pytest.raises(EnvFileError):
        update(path, {"A": "9"}, backup_dir=tmp_path / "bk")
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "bk").exists()


# ══════════════════════════════════════════════════════════════
# 备份
# ══════════════════════════════════════════════════════════════


def test_update_creates_backup(tmp_path) -> None:
    path = _write(tmp_path)
    backup_dir = tmp_path / "bk"
    result = update(path, {"A": "1"}, backup_dir=backup_dir)
    assert result.backup is not None
    assert result.backup.is_file()
    assert "IMAP_USER" in result.backup.read_text(encoding="utf-8")


def test_backups_are_pruned(tmp_path) -> None:
    """备份按数量清理，不能无限堆积。"""
    path = _write(tmp_path)
    backup_dir = tmp_path / "bk"
    for index in range(6):
        update(path, {"COUNTER": str(index)}, backup_dir=backup_dir, backup_keep=2)
    remaining = list(backup_dir.glob(".env.*.bak"))
    assert len(remaining) == 2, f"应只保留 2 份，实际 {len(remaining)}"


def test_backup_failure_does_not_block_save(tmp_path, monkeypatch) -> None:
    """备份失败不该让使用者改不了配置——但必须留痕（返回 None）。"""
    path = _write(tmp_path)
    import automail.envfile as envfile_module

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(envfile_module.shutil, "copyfile", _boom)
    result = update(path, {"A": "1"}, backup_dir=tmp_path / "bk")
    assert result.backup is None
    assert read_entries(path)["A"].value == "1", "保存仍应成功"


# ══════════════════════════════════════════════════════════════
# blank_out（清空但保留键行 + 说明注释）
# ══════════════════════════════════════════════════════════════


def test_blank_out_keeps_key_line(tmp_path) -> None:
    """清空密码但**保留 ``KEY=`` 行**：让人在文件里仍能看到这个键存在。"""
    path = _write(tmp_path)
    blank_out(path, ["IMAP_AUTH_CODE", "LLM_API_KEY"])

    text = path.read_text(encoding="utf-8")
    assert "IMAP_AUTH_CODE=\n" in text
    assert "LLM_API_KEY=\n" in text
    assert "secret-value" not in text
    assert "sk-abc" not in text


def test_blank_out_loses_no_keys(tmp_path) -> None:
    """**回归**：清空多个键时不得丢失其它键。

    这是最危险的失败模式——静默删掉一行配置，使用者很久以后才发现。
    用键集合比对而不是文本包含判断。
    """
    path = _write(tmp_path)
    before = set(read_entries(path))

    blank_out(path, ["IMAP_AUTH_CODE", "LLM_API_KEY"])
    blank_out(path, ["IMAP_AUTH_CODE"])  # 第二次：幂等

    after = set(read_entries(path))
    assert after == before, f"丢失的键：{before - after}"


def test_blank_out_adds_explanation_comment(tmp_path) -> None:
    path = _write(tmp_path)
    blank_out(path, ["IMAP_AUTH_CODE"], note="已改用加密存储")
    lines = path.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("IMAP_AUTH_CODE="))
    assert lines[index - 1].strip() == "# 已改用加密存储"


def test_blank_out_comment_is_not_duplicated(tmp_path) -> None:
    """重复调用不得重复插入说明注释（否则文件越改越乱）。"""
    path = _write(tmp_path)
    blank_out(path, ["IMAP_AUTH_CODE"], note="已改用加密存储")
    blank_out(path, ["IMAP_AUTH_CODE"], note="已改用加密存储")
    text = path.read_text(encoding="utf-8")
    assert text.count("已改用加密存储") == 1


def test_blank_out_no_keys_is_noop(tmp_path) -> None:
    path = _write(tmp_path)
    before = path.read_text(encoding="utf-8")
    blank_out(path, [])
    assert path.read_text(encoding="utf-8") == before


# ══════════════════════════════════════════════════════════════
# 原子性
# ══════════════════════════════════════════════════════════════


def test_write_is_atomic_no_temp_left(tmp_path) -> None:
    """写完后不留下 ``.tmp`` 残file。"""
    path = _write(tmp_path)
    update(path, {"A": "1"})
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_write_leaves_original_intact(tmp_path, monkeypatch) -> None:
    """写盘失败时原文件必须完好（不能出现半截文件）。"""
    import automail.envfile as envfile_module

    path = _write(tmp_path)
    original = path.read_text(encoding="utf-8")

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(envfile_module.os, "replace", _boom)
    with pytest.raises(EnvFileError):
        update(path, {"A": "1"})
    assert path.read_text(encoding="utf-8") == original


def test_creates_file_when_absent(tmp_path) -> None:
    """文件不存在时也能写（首次配置场景）。"""
    path = tmp_path / ".env"
    update(path, {"IMAP_USER": "a@b.com"})
    assert read_entries(path)["IMAP_USER"].value == "a@b.com"
