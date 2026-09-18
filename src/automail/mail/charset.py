"""字节解码与字符集兜底。

针对国内邮箱的真实情况设计：

* **声明 GB2312 但实际是 GBK 字节**——这是 163 一类国产邮箱最常见的缺陷。
  GBK 是 GB2312 的超集，因此声明 gb2312 时优先按 gbk/gb18030 解码。
* 声明 ``latin-1`` 类字符集时**不会**失败（latin-1 能解码任意字节），
  直接使用会得到乱码。因此这类"万能字符集"要让位给 utf-8 先试。
* 全部失败时用 ``charset-normalizer`` 统计检测，最后才用 replace 兜底——
  宁可留下替代字符，也不要因解码失败而丢掉整封邮件。
"""

from __future__ import annotations

#: 声明这些字符集时不能直接信——它们能解码任意字节，会掩盖真实的编码
_PERMISSIVE_CHARSETS = frozenset(
    {"ascii", "us-ascii", "latin-1", "latin1", "iso-8859-1", "windows-1252", "cp1252",
     "ansi_x3.4-1968", "none", "unknown", "binary", "8bit"}
)

#: GB2312 的常见超集，按优先级排列
_GB_FALLBACKS = ("gbk", "gb18030")

_DEFAULT_CANDIDATES = ("utf-8", "gb18030", "big5", "shift_jis", "euc-kr")


def normalize_charset(charset: str | None) -> str | None:
    """规范化字符集名：小写、下划线转连字符、去空白。"""
    if not charset:
        return None
    name = charset.strip().lower().replace("_", "-")
    return name or None


def candidate_charsets(declared: str | None) -> list[str]:
    """给出解码候选顺序（去重且保序）。

    这是一个纯函数，便于直接测试顺序是否符合预期。
    """
    normalized = normalize_charset(declared)
    ordered: list[str] = []

    if normalized and normalized not in _PERMISSIVE_CHARSETS:
        # gb2312 声明常见于 GBK 字节：先试超集
        if normalized in {"gb2312", "gb2312-80", "gb-2312", "csgb2312", "gbk"}:
            ordered.extend(_GB_FALLBACKS)
        ordered.append(normalized)

    ordered.extend(_DEFAULT_CANDIDATES)

    if normalized and normalized in _PERMISSIVE_CHARSETS:
        # 兜底字符集放在最后：它一定能成功，但结果可能是乱码
        ordered.append(normalized)

    seen: dict[str, None] = {}
    for name in ordered:
        seen.setdefault(name, None)
    return list(seen)


def decode_bytes(data: bytes, declared: str | None = None) -> str:
    """按候选顺序解码字节，返回解码后的文本。

    永不对合法输入抛异常——解码失败会逐级降级，最后用 ``replace``。
    """
    if not data:
        return ""
    for charset in candidate_charsets(declared):
        try:
            return data.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue

    detected = _detect_with_normalizer(data)
    if detected is not None:
        return detected

    return data.decode("utf-8", errors="replace")


def _detect_with_normalizer(data: bytes) -> str | None:
    """用 charset-normalizer 做统计检测；不可用时静默跳过。"""
    try:
        from charset_normalizer import from_bytes
    except ImportError:  # pragma: no cover - 依赖已声明，仅防御
        return None
    try:
        best = from_bytes(data).best()
    except Exception:  # noqa: BLE001 - 检测器内部错误不应影响主流程
        return None
    if best is None:
        return None
    try:
        return str(best)
    except Exception:  # noqa: BLE001
        return None
