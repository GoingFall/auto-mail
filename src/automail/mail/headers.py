"""邮件头部解析：RFC 2047 解码、Message-ID 规范化、退订与线程相关头部。

设计要点
--------
* **不抛异常**：畸形头部在真实邮件里很常见（尤其国内邮箱），解析失败应降级
  为 None/空串，而不是让整封邮件处理失败。
* **Message-ID 规范化要保守**：只做「去尖括号、trim、域名小写」三件事。
  缺失或格式非法时返回 None 而不是编造一个——上层据此决定是否参与唯一约束。
"""

from __future__ import annotations

import re
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime

from ..db import iso

#: 规范化 Message-ID 时允许出现的字符集；含空白/控制字符的一律判为非法
_MESSAGE_ID_RE = re.compile(r"^[!-~]+@[!-~]+$")

#: ``List-Unsubscribe`` 中提取 mailto: 与 http(s): 目标
_ANGLE_TARGET_RE = re.compile(r"<([^>]+)>")


def decode_mime_words(raw: str | None) -> str:
    """解码 RFC 2047 编码字（``=?utf-8?B?...?=`` 形式）。

    多个片段会被拼接；无法解码的片段按出现顺序尽量保留，不丢内容。

    一个 CPython 的坑：``decode_header`` 对**未编码的片段**会执行
    ``encode("ascii", "backslashreplace")``，把裸非 ASCII 文本（如「通知」）
    变成 ``b' \\u901a\\u77e5'`` 这样的字面转义。仅当头部**同时**含编码字与
    裸非 ASCII 时才走到这条路径，因此很容易漏掉。这里显式还原。
    """
    if not raw:
        return ""
    try:
        parts = decode_header(raw)
    except Exception:  # noqa: BLE001 - 畸形头部不应中断处理
        return raw.strip()

    chunks: list[str] = []
    for payload, charset in parts:
        if isinstance(payload, bytes):
            if charset:
                from .charset import decode_bytes

                chunks.append(decode_bytes(payload, charset))
            else:
                chunks.append(_restore_unencoded(payload))
        else:
            chunks.append(payload)
    return "".join(chunks).strip()


#: ``\uXXXX`` / ``\xXX`` 形式的转义，用于判断是否需要还原
_BACKSLASH_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}|\\U[0-9a-fA-F]{8}")


def _restore_unencoded(payload: bytes) -> str:
    """还原 ``decode_header`` 对未编码片段做的 backslashreplace 转义。

    仅当字节是纯 ASCII 且确实含有转义序列时才走 unicode_escape——
    否则可能误伤正常文本里的反斜杠。
    """
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError:
        # 不是 backslashreplace 产物（含真实高位字节），交给通用解码
        from .charset import decode_bytes

        return decode_bytes(payload, None)

    if not _BACKSLASH_ESCAPE_RE.search(text):
        return text

    try:
        return text.encode("ascii").decode("unicode_escape")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def normalize_message_id(raw: str | None) -> str | None:
    """规范化 Message-ID。

    规则：去首尾空白与尖括号、域名部分小写。**返回 None 表示不可用**，
    而不是构造一个假 ID——上层据此跳过唯一约束，避免把不同邮件误判为同一封。

    格式要求：必须含 ``@``，且不含空白或控制字符。
    """
    if not raw:
        return None
    value = raw.strip()
    # 可能形如 "<a@b> (comment)" 或 "a@b"，取第一段
    value = value.split()[0] if value.split() else value
    value = value.strip().strip("<>").strip()
    if not value:
        return None

    local, sep, domain = value.partition("@")
    if not sep or not local or not domain:
        return None

    normalized = f"{local}@{domain.lower()}"
    if not _MESSAGE_ID_RE.match(normalized):
        return None
    return normalized


def parse_references(raw: str | None) -> list[str]:
    """从 ``References``/``In-Reply-To`` 头中提取 Message-ID 列表（保序）。

    保留原始尖括号形式之外的内容会被规范化：只保留能通过校验的 ID。
    """
    if not raw:
        return []
    found = re.findall(r"<[^<>]+>", raw)
    if not found:
        # 少数客户端不写尖括号，退化为按空白切分
        found = [token for token in raw.split() if "@" in token]
    result: list[str] = []
    for token in found:
        normalized = normalize_message_id(token)
        if normalized:
            result.append(normalized)
    return result


def parse_address(raw: str | None) -> tuple[str, str]:
    """解析地址头，返回 ``(addr, name)``。

    取第一个**含 ``@`` 的**地址；name 已做 RFC 2047 解码。无法解析时 addr 为空串。

    要求含 ``@`` 是必要的校验：``getaddresses`` 对 "not an address at all"
    这类垃圾输入会返回 ``('not', 'an')`` 之类的伪地址，不加校验会让脏数据
    进入 ``senders`` 表并污染「非联系人」判断。

    注意 ``email.utils.getaddresses`` 返回的是 ``(realname, email)`` ——
    **第一项是显示名，第二项才是地址**。这一点容易记反，因此这里显式命名。
    """
    if not raw:
        return "", ""
    try:
        pairs = getaddresses([raw])
    except Exception:  # noqa: BLE001
        return "", decode_mime_words(raw)

    for realname, email_addr in pairs:
        realname = (realname or "").strip()
        email_addr = (email_addr or "").strip()
        if "@" in email_addr:
            return email_addr.lower(), decode_mime_words(realname) or ""
        if "@" in realname:
            # 畸形输入：地址被放进了显示名位
            return realname.lower(), ""

    return "", decode_mime_words(raw)


def parse_address_list(raw: str | None) -> list[str]:
    """解析地址列表，仅返回含 ``@`` 的地址（小写）。"""
    if not raw:
        return []
    try:
        pairs = getaddresses([raw])
    except Exception:  # noqa: BLE001
        return []

    result: list[str] = []
    for realname, email_addr in pairs:
        candidate = (email_addr or "").strip()
        if "@" not in candidate and "@" in (realname or ""):
            candidate = realname.strip()
        if "@" in candidate:
            result.append(candidate.lower())
    return result


def parse_date(raw: str | None) -> str | None:
    """解析 ``Date`` 头为 ISO8601 UTC 字符串；失败返回 None。"""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return iso(dt)


def parse_unsubscribe(raw: str | None) -> tuple[bool, str | None, list[str]]:
    """解析 ``List-Unsubscribe``。

    返回 ``(是否存在, mailto 目标, http(s) 链接列表)``。

    **v1 只解析不访问**：链接仅用于展示，退订动作在 v2 需逐条确认
    （见 docs/spec-unsubscribe-archive.md）。
    """
    if not raw or not raw.strip():
        return False, None, []

    mailto: str | None = None
    links: list[str] = []

    for target in _extract_targets(raw):
        lowered = target.lower()
        if lowered.startswith("mailto:"):
            if mailto is None:
                # 去掉可能的查询串（?subject=unsubscribe）
                mailto = target[7:].split("?", 1)[0].strip()
        elif lowered.startswith(("http://", "https://")):
            links.append(target)

    return True, mailto, links


def _extract_targets(raw: str) -> list[str]:
    """从 ``List-Unsubscribe`` 值中提取目标串。"""
    bracketed = _ANGLE_TARGET_RE.findall(raw)
    if bracketed:
        return bracketed
    # 无尖括号时按逗号切分
    return [part.strip() for part in raw.split(",") if part.strip()]


def headers_to_dict(message: object) -> dict[str, str]:
    """把 ``email.message.Message`` 的头部收成小写键的字典。

    同名头部多次出现时以 ``", "`` 连接（``Received`` 之类除外，调用方按需处理）。
    """
    result: dict[str, str] = {}
    for key, value in getattr(message, "items", lambda: [])():
        name = key.lower()
        if name in result:
            result[name] = f"{result[name]}, {value}"
        else:
            result[name] = value
    return result


def get_header(headers: dict[str, str], name: str) -> str | None:
    """取头部值（大小写不敏感）；空串按 None 处理。"""
    value = headers.get(name.lower())
    if value is None:
        return None
    value = value.strip()
    return value or None


def is_auto_submitted(headers: dict[str, str]) -> str | None:
    """判断是否为自动投递邮件。

    返回命中的证据字符串（用于审计），否则 None。

    识别依据：
    * ``Auto-Submitted`` 头存在且不是 ``no``；
    * ``Precedence: bulk|junk|list``；
    * ``X-Autoreply``/``X-Autorespond`` 存在；
    * ``Return-Path: <>``（退信常见）。
    """
    auto = get_header(headers, "auto-submitted")
    if auto and auto.lower() != "no":
        return f"Auto-Submitted: {auto}"

    precedence = get_header(headers, "precedence")
    if precedence and precedence.lower() in {"bulk", "junk", "list"}:
        return f"Precedence: {precedence}"

    for key in ("x-autoreply", "x-autorespond", "x-auto-response-suppress"):
        if get_header(headers, key):
            return f"{key} 存在"

    return_path = get_header(headers, "return-path")
    if return_path is not None and return_path.strip() in {"<>", ""}:
        return "Return-Path 为空（退信特征）"

    return None


def is_list_mail(headers: dict[str, str]) -> bool:
    """是否为邮件列表/群发邮件。"""
    return bool(
        get_header(headers, "list-id")
        or get_header(headers, "list-post")
        or get_header(headers, "list-unsubscribe")
        or get_header(headers, "x-mailing-list")
    )
