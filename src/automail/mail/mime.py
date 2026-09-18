"""MIME 遍历、正文抽取与清洗。

本模块负责把一封原始邮件变成「可送抽取的清洗文本」，并收集其中的 ICS 部件。

清洗是**功能性的，不只是美化**：邮件正文里常带有被引用的旧邮件和很长的营销
页脚，其中包含过期的日期。若不清洗，规则与 LLM 都可能从那些位置抽到错误时间。
因此清洗顺序是刻意设计的，见 :func:`clean_body`。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from email.message import Message

from ..sanitize import strip_control
from .charset import decode_bytes
from .headers import decode_mime_words, get_header, headers_to_dict

#: 被视为日历部件的 MIME 类型
ICS_MIME_TYPES = frozenset({"text/calendar", "application/ics", "application/calendar+xml"})

#: 被视为日历部件的文件扩展名（兼容错误的 Content-Type）
ICS_EXTENSIONS = (".ics", ".ical")

# ── 清洗用正则（顺序敏感，见 clean_body）─────────────────────

#: 邮件客户端免责声明起始行
_DISCLAIMER_RE = re.compile(
    r"^\s*(?:"
    r"本邮件(?:及其附件)?(?:含|包含).{0,20}(?:保密|机密|专有)"
    r"|此邮件.{0,10}(?:保密|仅供)"
    r"|This (?:e-?mail|message).{0,40}(?:confidential|privileged)"
    r"|IMPORTANT:?\s*The information"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

#: 签名分隔行（RFC 3676：``-- `` 独占一行）
_SIGNATURE_RE = re.compile(r"^\s*--\s*$", re.MULTILINE)

#: 常见中文客户端签名起始
_SIGNATURE_CN_RE = re.compile(
    r"^\s*(?:发送自|发自我的|来自我的|在\s*\d{4}[-/年].{0,20}发送|"
    r"获取\s*Outlook|Sent from my)",
    re.IGNORECASE | re.MULTILINE,
)

#: 引用旧邮件的起始标记
_QUOTE_START_RE = re.compile(
    r"^\s*(?:"
    r"在\s*\d{4}\s*[-/年].{0,40}(?:写道|寫道|写：|写道：)"
    r"|_{5,}\s*(?:原始邮件|原始郵件|Original Message)"
    r"|-{3,}\s*(?:原始邮件|Original Message|Forwarded message)"
    r"|发件人[:：]\s*.{0,60}$"
    r"|From:\s*.{0,60}$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

#: 转发/回复的头部块（Outlook 风格）。
#:
#: 实测必需：转发邮件的头部含「发送时间: 2026年9月15日 10:51」，
#: 那个**日期+时刻**会误导预筛器认为「时间形态完整、规则能覆盖」，
#: 于是整封邮件跳过了 LLM 兜底——而真正的事件时间在正文后半部分。
#: 它同时也有害于抽取：会抽出一个「发送时间」事件。
_FORWARD_HEADER_RE = re.compile(
    r"^[\s_\-=*]{5,}\s*$\n"  # 分隔线
    r"(?:^[ \t]*(?:发件人|寄件者|寄件人|发送时间|寄件時間|发送日期|"
    r"收件人|收件者|抄送|副本|主题|主旨|"
    r"from|sent|to|cc|subject|date)[ \t]*[:：].*(?:\n|$))+",
    re.MULTILINE | re.IGNORECASE,
)

#: 营销页脚常见片段（长块，含退订/版权词）
_MARKETING_MARKERS = (
    "unsubscribe",
    "退订",
    "取消订阅",
    "©",
    "copyright",
    "版权所有",
    "隐私政策",
    "privacy policy",
)

#: 「这行含日程信息」的粗判据：阿拉伯或中文数字写的日期/时刻。
#:
#: 用途是给页脚剥离加一道**保护**。邀请函、通知函常把日程排成短行表格
#: （``升旗禮`` / ``上午十時三十分 | 中央廣場升旗台``），这些行又短又无标点，
#: 与「公司名/地址/电话」形态完全一样。早先的实现只按「短且无标点」判页脚，
#: 结果把这些**唯一的时刻来源**当噪音整段删掉——实测一封真实的升旗礼邀请函
#: 因此丢失了全部时刻。含日期或时刻的行一律不当页脚。
_SCHEDULE_SIGNAL_RE = re.compile(
    r"\d{1,4}\s*[年\-/\.月]"
    r"|(?<!\d)\d{1,2}\s*[:：]\s*\d{2}"
    r"|(?:上午|下午|中午|早上|早晨|凌晨|晚上|傍晚|深夜|晚間|am|pm|AM|PM)\s*"
    r"|[\d零〇○一二三四五六七八九十]\s*[時时點点]"
)

#: 引用行（以 > 开头）
_QUOTED_LINE_RE = re.compile(r"^\s*>+", re.MULTILINE)

#: 连续 3 个以上空行压成 1 个
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_LINE_TRIM_RE = re.compile(r"[ \t]+\n")


@dataclass(slots=True)
class IcsPart:
    """邮件中的一个日历部件。"""

    raw: bytes
    filename: str | None = None
    content_type: str = "text/calendar"
    inline: bool = True


@dataclass(slots=True)
class BodyResult:
    """正文抽取结果。"""

    text: str
    """清洗后的文本（供抽取与本地片段存储）。"""

    full_text: str
    """清洗前的完整纯文本，仅用于计算哈希，不落库。"""

    sha256: str
    """``full_text`` 的 SHA256，用于去重与变更检测。"""

    had_html: bool = False
    truncated: bool = False
    defects: list[str] = field(default_factory=list)
    """解析过程中发现的 MIME 缺陷（如实记录，不因此失败）。"""

    charset_used: str | None = None


# ──────────────────────────────────────────────────────────────
# 遍历与抽取
# ──────────────────────────────────────────────────────────────

def iter_parts(message: Message):
    """深度优先遍历所有 MIME 部件。

    ``walk()`` 本身已处理嵌套，但这里统一为生成器以便与 ICS 收集共用。
    """
    yield from message.walk()


def _part_is_attachment(part: Message) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    return disposition == "attachment"


def _iter_payloads(part: Message) -> list[tuple[bytes, str | None]]:
    """取出部件的解码后字节与其字符集。

    使用 ``get_payload(decode=True)``，它已处理 quoted-printable 与 base64。
    多部分部件返回空列表。
    """
    if part.is_multipart():
        return []
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - 畸形编码不应中断
        return []
    if payload is None:
        # 未编码的单部分文本
        raw = part.get_payload()
        if isinstance(raw, str):
            return [(raw.encode("utf-8", errors="replace"), part.get_content_charset())]
        return []
    return [(payload, part.get_content_charset())]


def html_to_text(html: str) -> str:
    """把 HTML 转成纯文本，保留链接文字与结构换行。

    丢弃 ``script``/``style``/``head``，块级元素补换行，避免文字粘连。
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - 依赖已声明
        return re.sub(r"<[^>]+>", "", html)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 - 解析器异常时退回正则
        return re.sub(r"<[^>]+>", "", html)

    for tag in soup(["script", "style", "head", "title", "noscript"]):
        tag.decompose()

    # 链接保留文字，去掉 URL（URL 对抽取时间无帮助且含噪声）
    for anchor in soup.find_all("a"):
        anchor.replace_with(anchor.get_text(" ", strip=True))

    for br in soup.find_all("br"):
        br.replace_with("\n")

    for block in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"]):
        block.append("\n")

    return soup.get_text()


def collect_ics_parts(message: Message) -> list[IcsPart]:
    """收集邮件中的日历部件（内联 ``text/calendar`` 或 ``.ics`` 附件）。"""
    found: list[IcsPart] = []
    for part in iter_parts(message):
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        filename = part.get_filename()
        if filename:
            filename = decode_mime_words(filename)

        is_ics_type = content_type in ICS_MIME_TYPES
        is_ics_file = bool(
            filename and filename.lower().endswith(ICS_EXTENSIONS)
        )
        if not (is_ics_type or is_ics_file):
            continue

        for payload, _charset in _iter_payloads(part):
            if not payload:
                continue
            found.append(
                IcsPart(
                    raw=payload,
                    filename=filename,
                    content_type=content_type,
                    inline=not _part_is_attachment(part),
                )
            )
    return found


def has_ics(message: Message) -> bool:
    """邮件是否携带日历部件。"""
    for part in iter_parts(message):
        if part.is_multipart():
            continue
        if (part.get_content_type() or "").lower() in ICS_MIME_TYPES:
            return True
        filename = part.get_filename()
        if filename and decode_mime_words(filename).lower().endswith(ICS_EXTENSIONS):
            return True
    return False


def extract_body(message: Message, *, max_chars: int) -> BodyResult:
    """抽取并清洗正文。

    选取策略：优先 ``text/plain``；没有则用 HTML 转文本。
    多方 ``text/plain`` 部件时取最长的一个（通常是真正的内容，其余为页脚片段）。
    """
    defects = [str(d) for d in getattr(message, "defects", []) or []]
    plain_chunks: list[tuple[str, str | None]] = []
    html_chunks: list[tuple[str, str | None]] = []

    for part in iter_parts(message):
        if part.is_multipart():
            continue
        if _part_is_attachment(part):
            continue
        content_type = (part.get_content_type() or "").lower()
        if content_type not in {"text/plain", "text/html"}:
            continue

        for payload, charset in _iter_payloads(part):
            if not payload:
                continue
            text = decode_bytes(payload, charset)
            if content_type == "text/plain":
                plain_chunks.append((text, charset))
            else:
                html_chunks.append((text, charset))

    charset_used: str | None = None
    had_html = False

    if plain_chunks:
        # 取最长片段：多部件时最短的往往是自动生成的说明文字
        full, charset_used = max(plain_chunks, key=lambda item: len(item[0]))
    elif html_chunks:
        had_html = True
        longest_html, charset_used = max(html_chunks, key=lambda item: len(item[0]))
        full = html_to_text(longest_html)
    else:
        # 完全没有文本部件（例如只有附件）：退化为空正文
        full = ""

    full_text = full
    sha256 = hashlib.sha256(full_text.encode("utf-8", errors="replace")).hexdigest()

    cleaned = clean_body(full_text)
    truncated = len(cleaned) > max_chars
    if truncated:
        cleaned = cleaned[:max_chars]

    return BodyResult(
        text=cleaned,
        full_text=full_text,
        sha256=sha256,
        had_html=had_html,
        truncated=truncated,
        defects=defects,
        charset_used=charset_used,
    )


# ──────────────────────────────────────────────────────────────
# 清洗流水线
# ──────────────────────────────────────────────────────────────

def clean_body(text: str) -> str:
    """清洗正文，**顺序不可随意调整**。

    1. 统一换行（CRLF/CR → LF）并剥离控制字符/ANSI 转义
    2. **剥掉转发/回复的头部块**（``发件人:``/``发送时间:``/…）
    3. 丢掉免责声明之后的所有内容
    4. 丢掉签名之后的所有内容
    5. 丢掉引用旧邮件之后的所有内容 ← 这一步防止抽到过期日期
    6. 删掉 ``>`` 引用行（即使没有标准引用头）
    7. 删掉营销页脚块
    8. 折叠多余空行与行尾空格

    第 3–5 步都是「截断型」：一旦命中标记，其后内容整体丢弃。理由是这些区块
    都在正文之后，且是过期信息的主要来源。

    第 2 步是**替换型**而非截断型：转发头部要**只删掉自己**，不能牵连
    后面的正文。这一点很关键——转发邮件的有用内容全在头部**之后**。

    第 1 步的控制字符剥离是**安全**要求：正文里的 ESC 序列会在终端里执行，
    造成 ANSI 注入（详见 :mod:`automail.sanitize`）。
    """
    if not text:
        return ""

    # 先剥控制字符：ANSI 序列可能插入到标记词中间，若不清洗，
    # 后续的截断正则会被绕过
    result = strip_control(text).replace("\r\n", "\n").replace("\r", "\n")

    # 转发头部先删（替换型）——它含自己的日期时刻，会污染后续判断
    result = _FORWARD_HEADER_RE.sub("", result)

    for pattern in (_DISCLAIMER_RE, _SIGNATURE_RE, _SIGNATURE_CN_RE, _QUOTE_START_RE):
        match = pattern.search(result)
        if match:
            result = result[: match.start()]

    result = _QUOTED_LINE_RE.sub("", result)
    result = _strip_marketing_footer(result)
    result = _LINE_TRIM_RE.sub("\n", result)
    result = _BLANK_LINES_RE.sub("\n\n", result)
    return result.strip()


_FOOTER_MARKER = "marker"  #: 明确命中营销词 → 可作为剥离依据
_FOOTER_HINT = "hint"  #: 只是「短且无标点」→ 仅可随同 marker 一起剥离


def _strip_marketing_footer(text: str) -> str:
    """从尾部向上找连续的营销/退订区块并丢弃。

    两个约束缺一不可：

    1. 只在**尾部连续区块**上生效（正文中间提到「退订」的正常句子不受影响）。
    2. 该区块里**必须至少命中一个营销词**（退订/©/版权所有/隐私政策…）。

    第 2 条是实测教训。原先只按「短且无标点」就判页脚，而邀请函的日程
    恰好排成这种短行（``升旗禮`` / ``上午十時三十分 | 中央廣場升旗台``），
    于是唯一的时刻来源被当噪音删掉。现在的取舍很明确：**宁可漏删页脚，
    不可误删日程**——含日期时刻的行直接视为正文，且没有营销词做背书时
    整块都不剥。
    """
    paragraphs = text.split("\n\n")
    if len(paragraphs) < 2:
        return text

    cut = len(paragraphs)
    saw_marker = False
    while cut > 1:
        kind = _footer_paragraph_kind(paragraphs[cut - 1])
        if kind is None:
            break
        if kind == _FOOTER_MARKER:
            saw_marker = True
        cut -= 1

    if not saw_marker:
        return text
    return "\n\n".join(paragraphs[:cut])


def _footer_paragraph_kind(paragraph: str) -> str | None:
    """判断段落像不像页脚：``"marker"`` / ``"hint"`` / ``None``（正文）。"""
    if not paragraph.strip():
        return _FOOTER_HINT
    lowered = paragraph.lower()
    if any(marker in lowered for marker in _MARKETING_MARKERS):
        return _FOOTER_MARKER
    # 含日期或时刻的行一律是正文——表格排版的日程信息正是这个形态
    if _SCHEDULE_SIGNAL_RE.search(paragraph):
        return None
    # 极短且无标点的行（页脚常见：公司名、地址、电话），但需要营销词背书
    stripped = paragraph.strip()
    if len(stripped) <= 40 and not re.search(r"[。！？.!?]", stripped):
        return _FOOTER_HINT
    return None


# ──────────────────────────────────────────────────────────────
# 邮件摘要（供落库）
# ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class ParsedMail:
    """一封邮件的解析结果，字段与 ``messages`` 表对应。"""

    subject: str
    from_addr: str
    from_name: str
    to_addrs: list[str]
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    sent_at: str | None
    has_ics: bool
    has_unsubscribe: bool
    unsubscribe_mailto: str | None
    unsubscribe_links: list[str]
    list_id: str | None
    auto_submitted: str | None
    body: BodyResult
    ics_parts: list[IcsPart]


def parse_message(message: Message, *, max_chars: int) -> ParsedMail:
    """把 ``email.message.Message`` 解析成 :class:`ParsedMail`。"""
    headers = headers_to_dict(message)

    unsubscribe_raw = get_header(headers, "list-unsubscribe")
    has_unsub, mailto, links = _parse_unsubscribe_safe(unsubscribe_raw)

    body = extract_body(message, max_chars=max_chars)
    from_addr, from_name = _parse_address_safe(get_header(headers, "from"))

    return ParsedMail(
        subject=decode_mime_words(get_header(headers, "subject")),
        from_addr=from_addr,
        from_name=from_name,
        to_addrs=_parse_address_list_safe(get_header(headers, "to")),
        message_id=_normalize_id_safe(get_header(headers, "message-id")),
        in_reply_to=_first_reference_safe(get_header(headers, "in-reply-to")),
        references=_parse_references_safe(get_header(headers, "references")),
        sent_at=_parse_date_safe(get_header(headers, "date")),
        has_ics=has_ics(message),
        has_unsubscribe=has_unsub,
        unsubscribe_mailto=mailto,
        unsubscribe_links=links,
        list_id=get_header(headers, "list-id"),
        auto_submitted=_auto_submitted_safe(headers),
        body=body,
        ics_parts=collect_ics_parts(message),
    )


# 薄包装：把 headers 模块的解析函数收敛到「绝不抛异常」的边界上。
# 这些包装让 parse_message 面对任何畸形输入都能返回一个可用对象。

def _parse_unsubscribe_safe(raw: str | None) -> tuple[bool, str | None, list[str]]:
    from .headers import parse_unsubscribe

    try:
        return parse_unsubscribe(raw)
    except Exception:  # noqa: BLE001
        return bool(raw), None, []


def _parse_address_safe(raw: str | None) -> tuple[str, str]:
    from .headers import parse_address

    try:
        return parse_address(raw)
    except Exception:  # noqa: BLE001
        return "", ""


def _parse_address_list_safe(raw: str | None) -> list[str]:
    from .headers import parse_address_list

    try:
        return parse_address_list(raw)
    except Exception:  # noqa: BLE001
        return []


def _normalize_id_safe(raw: str | None) -> str | None:
    from .headers import normalize_message_id

    try:
        return normalize_message_id(raw)
    except Exception:  # noqa: BLE001
        return None


def _parse_references_safe(raw: str | None) -> list[str]:
    from .headers import parse_references

    try:
        return parse_references(raw)
    except Exception:  # noqa: BLE001
        return []


def _first_reference_safe(raw: str | None) -> str | None:
    refs = _parse_references_safe(raw)
    return refs[0] if refs else None


def _parse_date_safe(raw: str | None) -> str | None:
    from .headers import parse_date

    try:
        return parse_date(raw)
    except Exception:  # noqa: BLE001
        return None


def _auto_submitted_safe(headers: dict[str, str]) -> str | None:
    from .headers import is_auto_submitted

    try:
        return is_auto_submitted(headers)
    except Exception:  # noqa: BLE001
        return None
