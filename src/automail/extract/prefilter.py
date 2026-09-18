"""预筛器：判断一封邮件是否值得进入抽取，以及是否值得调用 LLM。

**这是省 token 的关键**：LLM 是唯一有成本、有延迟、有隐私代价的环节，
只应对「确实可能含事件」的邮件调用。

两级判定：

1. :func:`should_extract` —— 值不值得**抽取**（含规则）
2. :func:`should_call_llm` —— 值不值得**调用 LLM**（比上一级更严）

判据取自真机观察：营销邮件、自动回复、验证码、服务条款通知都会带日期，
但它们不是用户的事件。而会议邀请、面试通知、账单到期、预约确认才是。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 命中说明「可能含事件」的关键词（中英繁混合，取自真机邮件用词）
EVENT_KEYWORDS = (
    # 会议/活动
    "会议", "會議", "开会", "會面", "面试", "面試", "约见", "預約", "预约",
    "报名", "報名", "登记", "登記", "活动", "活動", "讲座", "講座", "研讨会",
    "meeting", "invite", "invitation", "registration", "webinar", "seminar",
    "appointment", "welcoming", "orientation", "schedule",
    # 截止/到期
    "截止", "到期", "逾期", "还款", "還款", "缴费", "繳費", "续费", "續費",
    "deadline", "due", "expire", "expiry", "renewal",
    # 行程
    "航班", "起飞", "起飛", "登机", "登機", "行程", "航班号", "flight",
    "itinerary", "departure", "boarding",
    # 时间指向（含中文正式通知的固定用法）
    "将于", "將於", "定于", "定於", "安排在", "scheduled", "rescheduled",
    "改期", "延期", "如期",
    # 正式邀请函/通知函的固定用语——实测漏掉它们会导致整封邮件被判「无事件线索」：
    # 「謹訂於二零二六年十月一日舉行升旗禮」里没有任何上表词汇，
    # 只有「謹訂於…舉行」这个中文公文的固定搭配。
    "謹訂於", "谨订于", "舉行", "举行", "舉辦", "举办", "誠邀", "诚邀",
    "敬邀", "敬請", "敬请", "撥冗", "拨冗", "出席", "蒞臨", "莅临",
    "回條", "回条", "報名", "登记", "截止日期",
    "ceremony", "invitation", "reception", "registration",
)

#: 命中说明「大概率不是用户事件」——直接不抽取
NOISE_KEYWORDS = (
    "验证码", "驗證碼", "校验码", "校驗碼", "verification code", "otp",
    "一次性密码", "動態密碼", "动态密码",
)

#: 命中且无事件关键词时，判为噪音（营销/推广）
MARKETING_KEYWORDS = (
    "退订", "退訂", "取消订阅", "取消訂閱", "unsubscribe",
    "優惠", "优惠", "折扣", "discount", "promotion", "推廣", "推广",
    "限时", "限時", "立即下单", "立即下單", "版權所有", "版权所有", "copyright",
    "積分", "积分", "reward", "在此下载", "下載app",
)

#: 中文数字字符集（与 rules 模块一致；用于日期/时刻形态识别）
_CN_NUM = "零〇○一二三四五六七八九十两兩"

#: 日期形态（用于「无关键词但含明确时间」的兜底判断）
#:
#: **必须包含中文数字**：中文正式通知（如某大学的邀请函）用
#: 「二零二六年十月一日」，只认阿拉伯数字会漏掉整类邮件的预筛。
#:
#: 也要包含**英文月份名**：Zoom / Google / Outlook 的邀请函写作
#: ``Aug 28, 2026 12:00 PM``，实测漏掉它会让预筛与规则双双失手。
_DATE_SHAPE_RE = re.compile(
    r"\d{4}\s*[年\-/\.]\s*\d{1,2}\s*[月\-/\.]\s*\d{1,2}"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{4}-\d{2}-\d{2}"
    rf"|[{_CN_NUM}]{{2,4}}\s*年\s*[{_CN_NUM}]{{1,3}}\s*月"
    rf"|[{_CN_NUM}]{{1,3}}\s*月\s*[{_CN_NUM}]{{1,3}}\s*[日號号]"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)

#: 时刻形态（同样需含中文数字：「上午十時三十分」）
_TIME_SHAPE_RE = re.compile(
    r"\d{1,2}:\d{2}"
    r"|\d{1,2}\s*[時时點点]"
    r"|(?:上午|下午|晚上|中午)\s*\d{1,2}"
    rf"|[{_CN_NUM}]{{1,3}}\s*[時时點点]"
)


@dataclass(slots=True)
class PrefilterVerdict:
    """预筛结论。"""

    extract: bool
    """是否值得抽取（含规则）。"""

    call_llm: bool
    """是否值得调用 LLM。比 ``extract`` 更严格。"""

    reason: str
    score: int = 0
    """启发式得分，便于调参与排查。"""

    matched: tuple[str, ...] = ()
    """命中的关键词，用于解释结论。"""


#: 段落分隔（与 rules 模块一致）
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def _date_and_time_share_paragraph(text: str) -> bool:
    """是否存在**同一段**内同时含日期与时刻。

    规则引擎逐段处理，只有同段配对才能被规则的「日期 + 时刻」逻辑组合起来。
    跨段的日期与时刻（表格排版的通知函很常见）规则无法配对，需要 LLM。
    """
    for paragraph in _PARAGRAPH_SPLIT_RE.split(text):
        if not paragraph.strip():
            continue
        if _DATE_SHAPE_RE.search(paragraph) and _TIME_SHAPE_RE.search(paragraph):
            return True
    return False


def _find(text: str, keywords: tuple[str, ...]) -> list[str]:
    lowered = text.casefold()
    return [kw for kw in keywords if kw.casefold() in lowered]


def classify(
    *,
    subject: str,
    text: str,
    auto_submitted: str | None = None,
    is_list_mail: bool = False,
    has_ics: bool = False,
) -> PrefilterVerdict:
    """判断邮件的抽取价值。

    顺序很关键：**先排除**（自动投递、验证码、营销），再判断是否有事件线索。
    先排除可避免「营销邮件里提到『活动』」这类误判。
    """
    haystack = f"{subject}\n{text}"
    matched: list[str] = []

    # ICS 是最高优先级信号：直接抽取，且**无需 LLM**（ICS 本身就是结构化数据）
    if has_ics:
        return PrefilterVerdict(
            extract=True, call_llm=False, reason="含 ICS 日历部件，可直接解析",
            score=100, matched=("ics",),
        )

    # 自动投递（自动回复、退信、列表批量）→ 不抽取
    if auto_submitted:
        return PrefilterVerdict(
            extract=False, call_llm=False,
            reason=f"自动投递邮件（{auto_submitted}）", score=-50,
        )

    # 验证码类 → 不抽取
    noise = _find(haystack, NOISE_KEYWORDS)
    if noise:
        return PrefilterVerdict(
            extract=False, call_llm=False,
            reason="验证码类邮件", score=-40, matched=tuple(noise),
        )

    event_hits = _find(haystack, EVENT_KEYWORDS)
    marketing_hits = _find(haystack, MARKETING_KEYWORDS)
    matched.extend(event_hits)

    has_date = bool(_DATE_SHAPE_RE.search(text))
    has_time = bool(_TIME_SHAPE_RE.search(text))
    # 规则引擎**逐段**处理，因此「日期与时刻在同一段」才是规则能配对的
    # 前提。邀请函常用表格排版（日期一段、时刻一段），此时规则配不上，
    # 必须交给 LLM——用全文判断会误以为规则够用而跳过 LLM。
    paired_in_paragraph = _date_and_time_share_paragraph(text)

    score = 0
    score += len(event_hits) * 3
    if has_date:
        score += 2
    if has_time:
        score += 3

    # 营销特征：有营销词**且无事件词** → 判为噪音
    if marketing_hits and not event_hits:
        matched.extend(marketing_hits)
        return PrefilterVerdict(
            extract=False, call_llm=False,
            reason=f"营销/推广类邮件（命中 {', '.join(marketing_hits[:3])}）",
            score=-20, matched=tuple(matched),
        )

    # 列表邮件（群发）若不含事件词 → 不抽取
    if is_list_mail and not event_hits:
        return PrefilterVerdict(
            extract=False, call_llm=False, reason="群发列表邮件且无事件线索", score=-10
        )

    # 需要有「事件词」或「配对完整的日期+时刻」才认为值得抽取
    if not event_hits and not paired_in_paragraph:
        return PrefilterVerdict(
            extract=False, call_llm=False,
            reason="未发现事件线索（无事件关键词，也无同段的日期+时刻）",
            score=score, matched=tuple(matched),
        )

    # 值不值得调 LLM：判据是**同一段内**日期与时刻是否配对完整。
    # 只有配对完整时规则才真的能覆盖；否则（缺一个、或分在不同段）
    # 规则会漏，值得让 LLM 看一遍。
    call_llm = bool(event_hits) and not paired_in_paragraph

    reason = "含事件线索"
    if event_hits:
        reason += f"（关键词：{', '.join(event_hits[:3])}）"
    if call_llm:
        if has_date and has_time:
            reason += "；日期与时刻分处不同段落，规则无法配对，值得 LLM 兜底"
        else:
            reason += "；时间形态不完整，值得 LLM 兜底"
    else:
        reason += "；同段内日期与时刻配对完整，规则应能覆盖"

    return PrefilterVerdict(
        extract=True,
        call_llm=call_llm,
        reason=reason,
        score=score,
        matched=tuple(dict.fromkeys(matched)),
    )
