"""``.env`` 的读取与**就键改写**。

**这是本项目第一次改写已有的 ``.env``。** 在此之前程序只在文件不存在时从模板
生成一次（``portable.py``），从不触碰使用者的配置。改写别人的配置文件风险在于
**静默丢失**：注释、空行、键顺序、程序不认识的自定义键，任何一样被吞掉都是
使用者的损失，而且往往过很久才被发现。

因此这里的规则是**外科手术式**的：只替换指定键的值，其余字节原样保留。
不是"读成字典 → 改 → 重新 dump"——那样会丢掉全部注释与排版。

写盘用「临时文件 + ``os.replace``」保证原子性：读者要么看到旧文件，要么看到
新文件，不会读到写了一半的内容（计划任务可能正在读同一个文件）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("automail.envfile")

#: 匹配 ``KEY=值`` 一行。允许 ``export`` 前缀与前后空白。
#:
#: 只认行首（可能有空白）的赋值，避免把注释里提到的 ``KEY=...`` 当成真值。
_ASSIGN_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<sep>\s*=\s*)(?P<value>.*)$"
)


class EnvFileError(Exception):
    """``.env`` 读写失败（调用方应把原因显示给使用者）。"""


@dataclass(slots=True)
class EnvEntry:
    """``.env`` 中的一行赋值。"""

    key: str
    value: str
    line_index: int


@dataclass(slots=True)
class UpdateResult:
    """一次改写的结果。"""

    path: Path
    changed: dict[str, str]
    """真正被改动的键 → 新值（值不含密钥明文，调用方自行决定是否展示）。"""

    backup: Path | None = None
    """备份文件路径（未写盘时为 ``None``）。"""


def read_entries(path: Path) -> dict[str, EnvEntry]:
    """读出全部赋值行（键名大写）。

    Raises:
        EnvFileError: 同一个键出现多次。**不猜哪个生效**——真实生效的是
            pydantic-settings 的读取顺序，靠猜没用；宁可报错让人先清理。
    """
    if not path.is_file():
        return {}

    try:
        text = _read_text(path)
    except OSError as exc:
        raise EnvFileError(f"读取 {path.name} 失败：{exc}") from exc

    entries: dict[str, EnvEntry] = {}
    for index, line in enumerate(text.splitlines()):
        match = _ASSIGN_RE.match(line)
        if not match:
            continue
        key = match.group("key").upper()
        if key in entries:
            raise EnvFileError(
                f"{path.name} 中 {key} 出现了多次（第 "
                f"{entries[key].line_index + 1} 行与第 {index + 1} 行）。"
                "请先删掉重复项再保存，程序不猜哪个生效。"
            )
        entries[key] = EnvEntry(
            key=key,
            value=_unquote(match.group("value").strip()),
            line_index=index,
        )
    return entries


def read_value(path: Path, key: str, default: str = "") -> str:
    entries = read_entries(path)
    entry = entries.get(key.upper())
    return entry.value if entry else default


def _read_text(path: Path) -> str:
    """按原样读出文本，**不做换行归一化**。

    必须显式 ``newline=""``：Python 文本模式默认启用 universal newlines，会把
    ``\\r\\n`` 悄悄折成 ``\\n``。那样写回去就把使用者的 CRLF 文件全变成 LF——
    对 Git 而言是整文件改动，且会掩盖真正的修改。实测踩到过。
    """
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _unquote(value: str) -> str:
    """去掉配对的首尾引号（``KEY="v"`` → ``v``）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _quote_if_needed(value: str) -> str:
    """值含空格或 ``#`` 时加引号，否则原样。

    不加引号会导致 ``#`` 之后被当作注释、空格被截断——那属于静默改错值。
    """
    if value == "":
        return ""
    if any(ch in value for ch in (" ", "\t", "#", '"', "'")):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def update(
    path: Path,
    values: dict[str, str],
    *,
    backup_dir: Path | None = None,
    backup_keep: int = 3,
) -> UpdateResult:
    """把若干键写入 ``.env``，**其余内容逐字保留**。

    键不存在时追加到文件末尾（若原文件以注释结尾，会另起一块并加说明注释）。
    值传空串表示"清空该键"（保留 ``KEY=`` 行本身，见 :func:`blank_out`）。

    Raises:
        EnvFileError: 文件重复键、无写入权限等。
    """
    if not values:
        return UpdateResult(path=path, changed={})

    text = ""
    if path.is_file():
        try:
            text = _read_text(path)
        except (OSError, UnicodeDecodeError) as exc:
            raise EnvFileError(f"读取 {path.name} 失败：{exc}") from exc

    # 先验证（重复键在这里报错，且此时还没动过磁盘）
    entries = read_entries(path)

    lines = text.splitlines(keepends=True)
    upper_values = {key.upper(): value for key, value in values.items()}
    changed: dict[str, str] = {}
    appended: list[str] = []

    for key, value in upper_values.items():
        entry = entries.get(key)
        if entry is None:
            appended.append(f"{key}={_quote_if_needed(value)}")
            changed[key] = value
            continue

        line = lines[entry.line_index]
        # 保留原有的 ``export `` 前缀与缩进，只换值
        match = _ASSIGN_RE.match(line.rstrip("\r\n"))
        assert match is not None  # read_entries 已确认匹配
        newline = "\n"
        if line.endswith("\r\n"):
            newline = "\r\n"
        elif line.endswith("\n"):
            newline = "\n"
        elif line.endswith("\r"):
            newline = "\r"
        lines[entry.line_index] = (
            f"{match.group('prefix')}{match.group('key')}{match.group('sep')}"
            f"{_quote_if_needed(value)}{newline}"
        )
        if entry.value != value:
            changed[key] = value

    if not changed:
        return UpdateResult(path=path, changed={})

    body = "".join(lines)
    if appended:
        if body and not body.endswith(("\n", "\r")):
            body += "\n"
        if body and not body.endswith("\n\n"):
            body += "\n"
        body += "# 由 auto-mail 写入\n"
        body += "\n".join(appended) + "\n"

    backup: Path | None = None
    if path.is_file() and backup_dir is not None:
        backup = _backup(path, backup_dir=backup_dir, keep=backup_keep)

    _atomic_write(path, body)
    logger.info("已更新 %s：%s", path.name, "、".join(sorted(changed)))
    return UpdateResult(path=path, changed=changed, backup=backup)


def blank_out(
    path: Path,
    keys: list[str],
    *,
    note: str | None = None,
    backup_dir: Path | None = None,
    backup_keep: int = 3,
) -> UpdateResult:
    """把指定键的值清空，**保留 KEY= 行**，并可选在上一行加说明注释。

    为什么保留空行而不是删掉整行：使用者翻看 ``.env`` 时仍能看到这个键存在、
    以及去哪填（注释会说明）。直接删掉会让人以为程序不支持这项配置。
    """
    if not keys:
        return UpdateResult(path=path, changed={})

    result = update(
        path, {key: "" for key in keys}, backup_dir=backup_dir, backup_keep=backup_keep
    )
    if note is None or not result.changed:
        return result

    # 在刚刚清空的键上方插入说明注释（幂等：同内容注释已存在则不重复插）
    try:
        text = _read_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise EnvFileError(f"读取 {path.name} 失败：{exc}") from exc

    lines = text.splitlines(keepends=True)
    entries = read_entries(path)
    inserts: list[tuple[int, str]] = []
    for key in keys:
        entry = entries.get(key.upper())
        if entry is None:
            continue
        previous = lines[entry.line_index - 1].strip() if entry.line_index > 0 else ""
        if previous == f"# {note}":
            continue  # 已经有了，不重复插
        inserts.append((entry.line_index, f"# {note}\n"))

    if not inserts:
        return result
    for index, comment in sorted(inserts, reverse=True):
        lines.insert(index, comment)
    _atomic_write(path, "".join(lines))
    return result


def _backup(path: Path, *, backup_dir: Path, keep: int) -> Path | None:
    """写前备份，并按数量清理旧备份。

    保留份数复用 ``db_backup_keep``（默认 3）——配置被改坏时能快速回退，
    又不至于无限堆积。

    文件名带**微秒**与去重后缀：只精确到秒时，同一秒内的多次保存会撞名，
    后者把前者覆盖掉，结果是"备份好像有、其实只剩最后一份"。实测踩到过。
    """
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        from .db import utcnow

        stamp = utcnow().strftime("%Y%m%d-%H%M%S-%f")
        target = backup_dir / f"{path.name}.{stamp}.bak"
        counter = 1
        while target.exists():
            target = backup_dir / f"{path.name}.{stamp}-{counter}.bak"
            counter += 1
        shutil.copyfile(path, target)
    except OSError as exc:
        # 备份失败不该阻止保存（否则使用者改不了配置），但必须留痕
        logger.warning("备份 %s 失败：%s", path.name, exc)
        return None

    _prune(backup_dir, prefix=f"{path.name}.", keep=keep)
    return target


def _prune(backup_dir: Path, *, prefix: str, keep: int) -> None:
    if keep <= 0:
        return
    try:
        candidates = sorted(
            (p for p in backup_dir.iterdir() if p.name.startswith(prefix)),
            key=lambda p: p.name,
        )
    except OSError:
        return
    for stale in candidates[:-keep]:
        try:
            stale.unlink()
        except OSError:
            continue


def _atomic_write(path: Path, content: str) -> None:
    """原子写：临时文件 + ``os.replace``。

    直接覆盖写入时，另一个进程（计划任务里的 ``run``）可能正好读到写了一半的
    内容。``os.replace`` 在同一文件系统内是原子的，读者只会看到完整的新旧之一。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8", newline="")
        os.replace(tmp, path)
    except OSError as exc:
        raise EnvFileError(f"写入 {path.name} 失败：{exc}") from exc
