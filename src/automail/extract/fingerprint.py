"""指纹与规范化：事件去重的唯一依据。

``fingerprint`` 用于**去重**（同一封邮件重跑不得产生重复事件），
**不用于更新匹配**——更新匹配走 ``(ics_uid, ics_recurrence_id, organizer)``，
因为同一 ``ics_uid`` 的实例例外（``RECURRENCE-ID``）与母事件共享 UID，
用指纹匹配会误合并（见 docs/spec-gcal-ownership.md §6）。

组成按规格：标题 + 开始 + 结束 + 组织者/发件人 + 地点。只用标题+时间不够——
两个不同会议完全可能同名且同一时间。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: 归一化标题时丢弃的装饰性前缀
_TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:re|fw|fwd|回复|转发|答复)\s*[:：]\s*"
    r"|(?:updated invitation|invitation|accepted|declined|cancelled)\s*[:：]\s*"
    r"|【[^】]{1,12}】\s*"
    r"|\[[^\]]{1,12}\]\s*"
    r")+",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_title(title: str | None) -> str:
    """归一化标题：去装饰前缀、折叠空白、统一大小写折叠、去零宽字符。

    目的是让「Re: 会议通知」与「会议通知」、「【重要】面试」与「面试」
    产生同一指纹——它们是同一件事的不同表示，不该产生两条事件。
    """
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    # 反复剥离前缀（可能叠加，如 "[fwd] Re: xxx"）
    previous = None
    while previous != text:
        previous = text
        text = _TITLE_PREFIX_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.casefold()


def normalize_ts(value: str | None) -> str:
    """归一化时间戳为可比字符串。

    只保留到分钟并去掉时区表示差异——指纹用于「同一事件」判断，
    秒级差异或 ``+08:00``/``Z`` 的表示差异不应产生不同指纹。
    """
    if not value:
        return ""
    text = value.strip()
    # 去掉冒号形式的时区偏移与 Z 后缀的表示差异，保留本地时刻
    text = re.sub(r"([+-]\d{2}):?(\d{2})$", r"\1\2", text)
    text = text.replace("T", " ")
    # 截到分钟
    match = re.match(r"(\d{4}-\d{2}-\d{2})[ ](\d{2}:\d{2})", text)
    if match:
        return f"{match.group(1)} {match.group(2)}"
    return text


def normalize_identity(value: str | None) -> str:
    """归一化组织者/发件人：取邮箱部分并小写。

    ``"Boss <boss@example.com>"`` 与 ``"boss@example.com"`` 应等价。
    """
    if not value:
        return ""
    text = value.strip().casefold()
    match = re.search(r"<([^>]+)>", text)
    if match:
        return match.group(1).strip()
    return text


def normalize_location(value: str | None) -> str:
    """归一化地点：折叠空白、去常见前缀。"""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = _WHITESPACE_RE.sub(" ", text).strip().casefold()
    return text


def compute_fingerprint(
    *,
    title: str | None,
    start_ts: str | None,
    end_ts: str | None,
    organizer: str | None,
    location: str | None,
) -> str:
    """计算事件指纹（sha256 前 32 位十六进制）。

    五个成分都参与：只用「标题 + 开始时间」会让两个不同会议（同名同时）
    产生相同指纹而被误判为同一事件。
    """
    parts = [
        normalize_title(title),
        normalize_ts(start_ts),
        normalize_ts(end_ts),
        normalize_identity(organizer),
        normalize_location(location),
    ]
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def compute_ics_fingerprint(
    *, ics_uid: str | None, ics_recurrence_id: str | None, organizer: str | None
) -> str | None:
    """ICS 来源的稳定指纹：``ics_uid`` + 实例例外 + 组织者。

    比内容指纹更适合 ICS——即使摘要或时间被更新，同一 ``ics_uid`` 仍是同一
    事件，这样「更新」不会变成「新增」。
    """
    if not ics_uid:
        return None
    parts = [
        ics_uid.strip(),
        (ics_recurrence_id or "").strip(),
        normalize_identity(organizer),
    ]
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ──────────────────────────────────────────────────────────────
# 标题相似度（仅用于「可能重复」提示，不用于任何自动合并）
# ──────────────────────────────────────────────────────────────

#: 视为有意义的 token 最小长度（英文）
_MIN_TOKEN_LEN = 3


def title_tokens(title: str | None) -> set[str]:
    """把标题拆成用于比较的 token 集合。

    英文按词切（长度 ≥3），中文按 2 字滑窗——因为中文标题常是长串，
    整块比较会完全不匹配，而滑窗能捕捉「开学典礼」与「开学典礼仪式」的重合。
    """
    if not title:
        return set()
    text = unicodedata.normalize("NFKC", title).casefold()
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)

    out: set[str] = set()
    for word in text.split():
        if re.search(r"[\u4e00-\u9fff]", word):
            # 中文：2 字滑窗（单字太易误配，整串太严）
            out.update(word[i : i + 2] for i in range(max(1, len(word) - 1)))
        elif len(word) >= _MIN_TOKEN_LEN:
            out.add(word)
    return out


def title_similarity(left: str | None, right: str | None) -> float:
    """标题相似度 = 较短标题的 token 被对方覆盖的比例（0~1）。

    用「包含度」而非 Jaccard：邮件主题常附带前缀（``[Reminder]``、
    ``Invitation:``），Jaccard 会因这些噪声显著压低分数，而包含度不受影响。

    **仅用于给出「可能重复」提示**，绝不用于自动合并——实测中
    同一发件人的多笔独立交易（ZA Card 消费、不同八达通卡操作）标题高度相似，
    自动合并会静默丢掉真实信息。
    """
    a, b = title_tokens(left), title_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))
