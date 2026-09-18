"""规则引擎：确定性时间抽取 + 可解释的置信度打分。

设计要点
--------
* **确定性**：同一输入恒得同一输出，可解释、可测试。这是 LLM 兜底之前的第一道。
* **置信度来自模式权重**，不是凭感觉赋值——见 :data:`_Pattern` 的 ``weight``
  与 :func:`score_confidence`。
* **相对时间以收信时间为基准**（规格 §8）。
* **无年份日期是硬门**：即便抽出也标 ``requires_review``，且**不受**
  ``AMBIGUOUS_DATE_POLICY`` 影响（优先级：无年份门 > 冲突门 > 该策略）。
* **早于收信时间 → 待审**：解析出的开始时间早于 ``received_at`` 即需人工确认
  （规格 §0-A，已删除 grace 参数——「已开始/已过期」不该被自动放行）。

时刻词表覆盖简体、繁体与英文，因为真机邮件里这三种混用（实测有
「入境事務處」「上午11時15分」这类繁体表达）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..models import EventSource
from .fingerprint import compute_fingerprint

logger = logging.getLogger("automail.extract.rules")

# ──────────────────────────────────────────────────────────────
# 模式定义与权重
# ──────────────────────────────────────────────────────────────

#: 基础权重（规格 §5）：绝对日期+显式时刻 0.95 / 绝对日期无时刻 0.85 /
#: 相对日期 0.75。自动入历门槛是**严格大于** 0.85，因此实际只有 0.95 档
#: 能自动入历——这消除了「3月5日 年会」这类无时刻事件压线放行的风险。
W_ABSOLUTE_WITH_TIME = 0.95
W_ABSOLUTE_NO_TIME = 0.85
W_RELATIVE = 0.75

#: 模糊词系数：出现「左右/前后/大概/约」时打折
VAGUE_FACTOR = 0.6

#: 特异性系数：含年份更确定
SPECIFICITY_WITH_YEAR = 1.0
SPECIFICITY_WITHOUT_YEAR = 0.9

#: 一致性系数：单匹配确定；多候选需人工裁决
CONSISTENCY_SINGLE = 1.0
CONSISTENCY_MULTI = 0.8

VAGUE_WORDS = ("左右", "前后", "大概", "大约", "约", "差不多", "roughly", "around", "about")

# ── 绝对日期 ──────────────────────────────────────────────────

#: 2026年9月20日 / 2026-09-20 / 2026/9/20
_ABS_DATE_CN = re.compile(
    r"(?P<year>\d{4})\s*[年\-/\.]\s*(?P<month>\d{1,2})\s*[月\-/\.]\s*(?P<day>\d{1,2})\s*日?"
)
#: 9月20日 / 9/20（无年份）
_ABS_DATE_NO_YEAR = re.compile(
    r"(?<![\d/\.\-])(?P<month>\d{1,2})\s*[月\-/]\s*(?P<day>\d{1,2})\s*日"
)

#: 英文月份名 → 月号。Zoom / Google / Outlook 的邀请函与提醒都用这个格式。
_EN_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_EN_MONTH_ALT = "|".join(sorted(_EN_MONTHS, key=len, reverse=True))

#: 带年份：Aug 28, 2026 / August 28 2026（月在前）
#:
#: ``(?!\d)`` 不可省：缺了它，``28 Aug 2026`` 会被月在前模式从 ``Aug`` 处
#: 匹配成「Aug 20」（把年份 2026 的前两位当成日）——一个**凭空的错误日期**。
_ABS_DATE_EN_MD = re.compile(
    rf"\b(?P<month>{_EN_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s*(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)
#: 带年份：28 Aug 2026 / 28th August, 2026（日在前）
_ABS_DATE_EN_DM = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_EN_MONTH_ALT})\.?\s*,?\s*(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)
#: 无年份：Aug 28 / 28 Aug（年份由收信时间推断）
_ABS_DATE_EN_MD_NO_YEAR = re.compile(
    rf"\b(?P<month>{_EN_MONTH_ALT})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?![\d,])",
    re.IGNORECASE,
)
_ABS_DATE_EN_DM_NO_YEAR = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_EN_MONTH_ALT})\.?(?![\d,])",
    re.IGNORECASE,
)

# ── 中文数字（实测必需）───────────────────────────────────────
#
# 中文正式通知大量使用中文数字日期，例如某大学的邀请函：
#   「謹訂於二零二六年十月一日舉行…」「上午十時三十分」
# 不支持它就会**整封漏掉**——这正是实测踩到的：真实事件日 2026-10-01
# 完全没被抽出，而括号里的「（星期四）」反倒被当成了日期。

_CN_DIGITS = {
    "零": 0, "〇": 0, "○": 0,
    "一": 1, "壹": 1,
    "二": 2, "兩": 2, "两": 2, "貳": 2,
    "三": 3, "叁": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陸": 6, "陆": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}
_CN_TEN = "十拾"
#: 中文数字字符集（用于正则字符类）
_CN_NUM_CLASS = "零〇○一二三四五六七八九十壹貳貳两兩叁肆伍陸陆柒捌玖拾"


def _cn_year(text: str) -> int | None:
    """中文数字年份 → 整数。``二零二六`` → 2026。

    年份是**逐字拼接**（二零二六 = 2,0,2,6），不是按十进位读法。
    """
    digits = [_CN_DIGITS.get(ch) for ch in text]
    if any(d is None for d in digits):
        return None
    value = int("".join(str(d) for d in digits))
    return value if 1900 <= value <= 2999 else None


def _cn_number(text: str) -> int | None:
    """中文数字 → 整数，支持 1~99 的十进位读法。

    * ``十`` → 10、``十五`` → 15、``二十`` → 20、``二十一`` → 21
    * ``三`` → 3（单位数）
    """
    if not text:
        return None
    # 纯阿拉伯数字：宽容接受（字符串里偶尔混入 ASCII 数字）
    if text.isdigit():
        return int(text)
    # 含「十」时按十进位解析：<tens>十<units>
    for ten in _CN_TEN:
        if ten in text:
            head, _, tail = text.partition(ten)
            tens = 1 if not head else _CN_DIGITS.get(head)
            units = 0 if not tail else _CN_DIGITS.get(tail)
            if tens is None or units is None:
                return None
            return tens * 10 + units
    # 纯单位数
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    # 多个数字字符但无「十」：逐字拼接（如「一二」少见，按拼接处理）
    digits = [_CN_DIGITS.get(ch) for ch in text]
    if any(d is None for d in digits):
        return None
    return int("".join(str(d) for d in digits))


#: 二零二六年十月一日 / 二零二六年九月二十一日
_ABS_DATE_CN_NUMERAL = re.compile(
    rf"(?P<year>[{_CN_NUM_CLASS}]{{2,4}})\s*年\s*"
    rf"(?P<month>[{_CN_NUM_CLASS}]{{1,3}})\s*月\s*"
    rf"(?P<day>[{_CN_NUM_CLASS}]{{1,3}})\s*[日號号]"
)
#: 十月一日 / 九月二十一日（无年份）
_ABS_DATE_CN_NUMERAL_NO_YEAR = re.compile(
    rf"(?<![{_CN_NUM_CLASS}])(?P<month>[{_CN_NUM_CLASS}]{{1,3}})\s*月\s*"
    rf"(?P<day>[{_CN_NUM_CLASS}]{{1,3}})\s*[日號号]"
)

# ── 时刻 ─────────────────────────────────────────────────────

#: 上午11时15分 / 下午3点 / 晚上19点30分（简体+繁体「時/點」）
_CN_TIME = re.compile(
    r"(?P<period>上午|下午|中午|早上|早晨|凌晨|晚上|傍晚|深夜|晚間|上午間)?\s*"
    r"(?P<hour>\d{1,2})\s*[時时點点]\s*(?:(?P<minute>\d{1,2})\s*分?)?"
)
#: 上午十時三十分 / 下午三時正 / 晚上七點（中文数字时刻）
#:
#: 「正」「整」表示整点，视为 0 分。实测样本里的「上午十時正」就是 10:00。
_CN_NUMERAL_TIME = re.compile(
    rf"(?P<period>上午|下午|中午|早上|早晨|凌晨|晚上|傍晚|深夜|晚間)?\s*"
    rf"(?P<hour>[{_CN_NUM_CLASS}]{{1,3}})\s*[時时點点]\s*"
    rf"(?:(?P<minute>[{_CN_NUM_CLASS}]{{1,3}})\s*分|(?P<exact>正|整))?"
)

#: 15:00 / 15:00:30
_COLON_TIME = re.compile(r"\b(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)(?::[0-5]\d)?\b")
#: 3pm / 3:30pm
_EN_TIME = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>[0-5]\d))?\s*(?P<ampm>am|pm|a\.m\.|p\.m\.)\b",
    re.IGNORECASE,
)

# ── 相对日期 ──────────────────────────────────────────────────

_RELATIVE_DAYS = {
    "今天": 0, "今日": 0, "today": 0,
    "明天": 1, "明日": 1, "tomorrow": 1,
    "后天": 2, "後天": 2,
    "大后天": 3,
}

_WEEKDAYS_CN = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}

#: 下周X / 下週X / 这周X / 本週X
_WEEK_OFFSET_RE = re.compile(
    r"(?P<scope>下|下個|下个|本|这|這|此)?\s*(?:周|週|星期|礼拜|禮拜)\s*(?P<day>[一二三四五六日天1-7])"
)

# ── 截止语义 ──────────────────────────────────────────────────

#: 命中即判为 deadline（全天事件），而非会议时刻
_DEADLINE_KEYWORDS = (
    "截止", "到期", "逾期", "还款", "缴费", "截止日期", "deadline", "due",
    "expires", "expiry", "expire", "before", "前为止", "最後", "最后",
)

#: 命中说明是取消语义
_CANCEL_KEYWORDS = ("取消", "cancelled", "canceled", "取消預約", "已取消")

#: 免责声明／脚注段落的起始标记。
#:
#: **真实数据暴露的噪音源**：银行邮件的脚注形如
#: ``*作为香港首间数字银行，截至 2025 年 12 月 31 日，用户人数…``
#: 以及 ``^截至 2025 年 12 月 15 日的 App Store 评分。``
#: 这些日期是**营销宣称**，不是用户的事件，且每封邮件都带同一段，
#: 导致同一指纹反复出现（实测 7 次），把审核队列和摘要淹没。
#:
#: 判据：段落以脚注/免责标记开头。这类段落的日期一律不抽取。
_FOOTNOTE_MARKERS = ("*", "^", "†", "‡", "※", "注：", "註：", "注:", "註:")

#: 段落里出现这些词说明是免责声明／条款说明，其日期不构成用户事件
_DISCLAIMER_HINTS = (
    "资料来源", "資料來源", "仅供参考", "僅供參考", "以实际", "以實際",
    "详见", "詳見", "条款", "條款", "保留最终解释权", "保留最終解釋權",
    "source:", "for reference", "terms apply", "subject to",
)


def is_footnote_paragraph(paragraph: str) -> bool:
    """判断段落是否为免责声明／脚注。

    这类段落里的日期不是用户事件。实测中它们是审核队列的主要噪音源：

    * 银行邮件脚注含「截至 2025 年 12 月 15 日的 App Store 评分」
    * 同一段出现在多封邮件里，抽出同一指纹的多条候选（实测重复 7 次）
    * 用户面对 25 条几乎相同的待审项，无法判断哪些才是真事件

    判据分两类：**以脚注标记开头**，或**含免责声明用语**。
    第二类较宽松，因此只在段落较短（像脚注而不像正文）时才采信，
    避免误伤正文里提到「详见条款」的正常句子。
    """
    stripped = paragraph.strip()
    if not stripped:
        return False

    if stripped.startswith(_FOOTNOTE_MARKERS):
        return True

    lowered = stripped.casefold()
    if any(hint in lowered for hint in _DISCLAIMER_HINTS):
        # 脚注通常很短；长段落更可能是正文
        return len(stripped) < 200

    return False


@dataclass(slots=True)
class RuleHit:
    """一次规则命中。"""

    title: str
    start: datetime
    end: datetime | None
    all_day: bool
    evidence: str
    """命中的原文片段——审核界面靠它让用户判断是否可信。"""

    has_time: bool
    has_year: bool
    is_deadline: bool
    vague: bool
    kind: str

    title_from_subject: bool = False
    """标题是否退回了邮件主题。

    为真表示规则只识别出时间、说不出这是什么事件（正文句子不适合当标题）。
    调用方据此在去重时采用更具体的 LLM 标题。
    """


@dataclass(slots=True)
class RuleResult:
    """一封邮件的规则抽取结果。"""

    hits: list[RuleHit]
    ambiguous: bool = False
    """同一文本内出现多个候选日期 → 需人工裁决。"""

    def best(self) -> RuleHit | None:
        """取唯一候选；有多个时返回 None（交由上层判为待审）。"""
        if len(self.hits) != 1:
            return None
        return self.hits[0]


# ──────────────────────────────────────────────────────────────
# 时间解析
# ──────────────────────────────────────────────────────────────

def _apply_period(hour: int, period: str | None) -> int:
    """把「下午3点」这类表达转成 24 小时制。"""
    if not period:
        return hour
    if period in {"下午", "晚上", "傍晚", "晚間"} and hour < 12:
        return hour + 12
    if period == "中午" and hour < 12:
        return 12 if hour == 12 else hour + 12
    if period == "凌晨" and hour == 12:
        return 0
    return hour


def _apply_ampm(hour: int, ampm: str) -> int:
    marker = ampm.lower().replace(".", "")
    if marker == "pm" and hour < 12:
        return hour + 12
    if marker == "am" and hour == 12:
        return 0
    return hour


def _parse_time_in(text: str) -> tuple[int, int, str] | None:
    """从文本中找一个时刻，返回 ``(hour, minute, evidence)``。

    优先级：中文式（含时段词）> 冒号式 > 英文 am/pm。
    中文式优先是因为「下午3点」里的数字若先用冒号规则处理会误判。
    """
    for pattern in (_CN_TIME, _COLON_TIME, _EN_TIME):
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        hour = int(groups.get("hour") or 0)
        minute = int(groups.get("minute") or 0)

        if "period" in groups:
            hour = _apply_period(hour, groups.get("period"))
        if "ampm" in groups and groups.get("ampm"):
            hour = _apply_ampm(hour, groups["ampm"])

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        return hour, minute, match.group(0).strip()
    return None


def _resolve_month_day(received: datetime, month: int, day: int) -> date | None:
    """把「无年份的月日」落到具体年份。

    先按收信年试；若该日期已明显过去（超过 30 天），认为指的是下一年。
    注意返回的仍是「无年份」来源，由上层按**硬门**规则要求人工确认。
    """
    for year in (received.year, received.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= (received - timedelta(days=30)).date():
            return candidate
    return None


def _parse_absolute_date(text: str, received: datetime) -> tuple[date, bool, str] | None:
    """解析绝对日期，返回 ``(date, has_year, evidence)``。"""
    match = _ABS_DATE_CN.search(text)
    if match:
        try:
            value = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            return None
        return value, True, match.group(0).strip()

    match = _ABS_DATE_NO_YEAR.search(text)
    if match:
        month = int(match.group("month"))
        day = int(match.group("day"))
        # 无年份：先按收信年试；若该日期已明显过去（超过 30 天），
        # 才认为指的是下一年。极端歧义留给「无年份硬门」处理。
        for year in (received.year, received.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
            if candidate >= (received - timedelta(days=30)).date():
                return candidate, False, match.group(0).strip()
        return None

    return None


def _parse_relative_date(text: str, received: datetime) -> tuple[date, str] | None:
    """解析相对日期（今天/明天/下周X）。

    「周X」的跨周规则：
    * 带「下」→ 先算本周目标日，再 +7
    * 带「本/这」→ 本周目标日
    * 不带限定词 → 本周目标日；若目标日就是今天（差 0 天），视为下一次出现

    例（收信日 2026-09-14 为周一，目标周三）：
    「下周三」→ 9/23、「本周三」→ 9/16、「周三」→ 9/16
    """
    lowered = text.casefold()
    for word, delta in _RELATIVE_DAYS.items():
        if word in lowered or word in text:
            return (received + timedelta(days=delta)).date(), word

    # 括号里的星期几通常是对**前文日期的注解**，不是独立的相对日期。
    # 实测：「二零二六年十月一日（星期四）」里的「星期四」曾被算成
    # 收信后的下一个周四（2026-09-17）——一个凭空的错误日期。
    # 现在中文数字日期能匹配了，这里再加一道守卫，避免没有具体日期时
    # 仅凭括号内的星期几就凭空造日期。
    match = _WEEK_OFFSET_RE.search(text)
    if match and _inside_brackets(text, match.start()):
        return None
    if match:
        scope = match.group("scope") or ""
        target_weekday = _WEEKDAYS_CN.get(match.group("day"))
        if target_weekday is None:
            return None

        days_ahead = (target_weekday - received.weekday()) % 7

        if scope in {"下", "下個", "下个"}:
            days_ahead += 7
        elif scope in {"本", "这", "這", "此"}:
            pass
        elif days_ahead == 0:
            # 无限定词且目标日就是今天 → 指下一次
            days_ahead = 7

        return (received + timedelta(days=days_ahead)).date(), match.group(0).strip()

    return None


#: 中英文括号对
_BRACKETS = (("(", ")"), ("（", "）"), ("[", "]"), ("【", "】"), ("〔", "〕"))


def _inside_brackets(text: str, position: int) -> bool:
    """判断某个位置是否落在括号内。

    用途：括号里的星期几往往是注解（「十月一日（星期四）」），
    不应被当作独立的相对日期。判断方式是看该位置之前是否有未闭合的左括号。
    """
    for left, right in _BRACKETS:
        before = text[:position]
        if before.count(left) > before.count(right):
            return True
    return False


def _is_deadline(text: str, title: str) -> bool:
    haystack = f"{title}\n{text}".casefold()
    return any(kw in haystack for kw in _DEADLINE_KEYWORDS)


def _is_vague(text: str) -> bool:
    return any(w in text for w in VAGUE_WORDS)


#: 标题最大长度
TITLE_MAX_CHARS = 40

_WHITESPACE_RE = re.compile(r"\s+")

#: 从句子中剥掉的成分：时间表达本身、以及常见的引导词与标点
_TITLE_STRIP_RES = (
    _ABS_DATE_CN,
    _ABS_DATE_NO_YEAR,
    _COLON_TIME,
    _CN_TIME,
    _EN_TIME,
    _WEEK_OFFSET_RE,
    # 中文数字日期/时刻也必须剥——否则像「二零二六年十月一日」这种日期会
    # 原样留在标题里。英文月份名同理：漏掉它们时标题会退化成
    # 「20 August 2026」这种**只有日期**的串，读不出这是什么事件。
    _ABS_DATE_CN_NUMERAL,
    _ABS_DATE_CN_NUMERAL_NO_YEAR,
    _CN_NUMERAL_TIME,
    _ABS_DATE_EN_MD,
    _ABS_DATE_EN_DM,
    _ABS_DATE_EN_MD_NO_YEAR,
    _ABS_DATE_EN_DM_NO_YEAR,
)

#: 相对日期词（剥时间表达时一并去掉）
_TITLE_RELATIVE_RE = re.compile("|".join(re.escape(w) for w in _RELATIVE_DAYS))

#: 被剥掉的日期/时刻**前面的英文介词**（``commence on 7 September 2026 at 10:00``）。
#:
#: 只剥「紧跟数字」的介词：``on 7 …`` 里的 ``on`` 属于时间短语，剥掉日期后
#: 它就成了悬挂词（实测标题退化成 ``… will commence on at``）。
#:
#: 限定「后面必须跟数字」是刻意的——否则 ``Check in`` 这类正常标题会被
#: 剥成 ``Check``。这个限定在本例中恰好排除了 ``at 10:00`` 之外的所有误伤。
_TITLE_PREP_BEFORE_NUMBER_RE = re.compile(
    r"\b(?:on|at|by|from|until|before|after|since|starting)\b(?=\s*\d)",
    re.IGNORECASE,
)

#: 剥掉时刻后残留的裸上下午标记（``12:00 PM`` 被剥掉数字后只剩 ``PM``）。
_TITLE_BARE_MERIDIEM_RE = re.compile(r"\b(?:am|pm|a\.m\.|p\.m\.)\b", re.IGNORECASE)

#: 剥掉时间后残留的连接词／助词。这些词单独留着会让标题读不通
#: （例如「您的航班 CX368 将于 从香港起飞」里的「将于」）。
#:
#: 注意：**只剥真正的虚词**。「預約」「會議」这类是实义名词，剥掉会让标题失去
#: 关键信息（曾经把「預約於…辦理手續」剥成「前往…辦理手續」）。
_TITLE_CONNECTIVE_RE = re.compile(
    r"(?:将于|將於|定于|定於|安排在|安排於|发生于|發生於|于|於|为|為|是)\s*"
)

#: 标题开头常见的称谓／引导
_TITLE_LEAD_RE = re.compile(r"^\s*(?:我们|您的|您|你的|我|亲爱的|敬啟者|尊敬的)\s*[，,：:]?\s*")

#: 需要清理的悬挂标点与空白
#: 需要清理的悬挂标点与空白。含 ASCII 句号与全角句点：剥掉句尾的时间表达后，
#: 常常在原地留下一个孤立句点（``… will commence .``）。
_TITLE_TAIL_RE = re.compile(r"[\s，,。．.；;：:、！!？?]+$")
_TITLE_GAP_RE = re.compile(r"[\s，,。．.；;：:、]{2,}")

#: 剥掉时间后可能挂在末尾的动词/连接词（「会议改到」→「会议」）
_TITLE_DANGLING_TAIL_RE = re.compile(
    r"(?:改到|改為|改为|将于|將於|定于|定於|安排在|安排於|预约于|預約於|"
    r"发生于|發生於|是在|就在|是|在|于|於|到|为|為)\s*$"
)

#: 悬挂开头的助词（「你已 前往 …」里的「已」）。
#:
#: 单字形式（已/将/將/会/會/要）**必须带后缀限定**：不限定的话，
#: 「會議定於 2026年10月20日 15:00 舉行」会被剥成「議 舉行」——
#: 因为它把标题首字当成了助词。实测中文事件标题以「會議」「將…」开头的
#: 极多，这种误剥会让审核队列里出现读不通的残句。
_TITLE_DANGLING_HEAD_RE = re.compile(
    r"^(?:你已|您已)\s*"
    r"|^(?:已|将|將|会|會|要)\s*(?:在|于|於|到|和|跟|与|與)\s*"
)

#: 剥掉时间表达后留下的**空括号对**。
#:
#: 实测：`二零二六年九月二十一日 (星期一) 或之前登記` 里的 `星期一` 被
#: :data:`_WEEK_OFFSET_RE` 当作时间剥走，留下一个孤零零的 `( )`，
#: 标题就成了「敬希 二零二六年九月二十一日 ( ) 或之前…」。括号里原本
#: 只是对前文日期的注解，注解没了括号也不该留。
_TITLE_EMPTY_BRACKETS_RE = re.compile(
    r"[（(\[【〔]\s*(?:[|·、\-—,，]|\s)*\s*[）)\]】〕]"
)


def derive_title(paragraph: str, *, subject: str, fallback: str) -> str:
    """从命中时间的那句话里派生事件标题。

    为什么不用邮件主题当标题：主题常描述**邮件本身**而非事件。
    例如主题「您的行程单 - 9月25日 香港往上海」，而真正的事件是「航班 CX368 起飞」。
    直接把主题塞进日历会得到「您的行程单 - 9月25日 香港往上海」这种不合格的标题。

    做法：取包含时间的那个句子，剥掉时间表达、连接词与悬挂词；
    若结果不像一个完整标题（过短、或仍是残句），退回主题——
    主题虽然可能不够精确，但至少读得通。
    """
    return derive_title_ex(paragraph, subject=subject, fallback=fallback)[0]


def derive_title_ex(
    paragraph: str, *, subject: str, fallback: str
) -> tuple[str, bool]:
    """同 :func:`derive_title`，但额外返回「标题是否退回了主题」。

    调用方需要这个信号来判断**规则是否真的命名了这件事**。退回主题意味着
    规则只知道时间、说不出是什么事，此时若有 LLM 给出的具体标题，应当采用
    后者（见 :func:`~automail.extract.pipeline._dedupe_by_slot`）。
    """
    # 先按句末标点切句，找含日期/时刻形态的那句
    sentences = re.split(r"[。！？!?；;\n]+", paragraph)
    target = ""
    for sentence in sentences:
        stripped = sentence.strip()
        if not stripped:
            continue
        if any(pattern.search(stripped) for pattern in _TITLE_STRIP_RES):
            target = stripped
            break
    if not target:
        target = paragraph.strip()

    cleaned = _clean_fragment(target)

    if not _looks_like_title(cleaned):
        return _subject_title(subject) or subject or fallback, True

    if len(cleaned) > TITLE_MAX_CHARS:
        # 需要截断说明这多半是**正文里的一句话**而不是事件名。
        # 截断成「In addition you are invited to join our…」这种片段毫无信息量，
        # 而主题通常正是这件事的名字（「…Orientation Ceremony Invitation」）。
        # 实测：英文邀请函的日期一旦能被规则解析，整句话就会被当作标题，
        # 反而把此前由 LLM 给出的干净标题挤掉了。
        from_subject = _subject_title(subject)
        if from_subject:
            return from_subject, True
        cleaned = cleaned[:TITLE_MAX_CHARS].rstrip() + "…"
    return cleaned, False


def _clean_fragment(text: str) -> str:
    """把一句话剥成可能的标题片段（剥时间表达、连接词、悬挂标点）。"""
    cleaned = text
    # 英文时间短语的介词必须在日期被剥掉**之前**先行断言——它们靠
    # 「后面跟数字」来识别（见 _TITLE_PREP_BEFORE_NUMBER_RE）
    cleaned = _TITLE_PREP_BEFORE_NUMBER_RE.sub(" ", cleaned)
    for pattern in (*_TITLE_STRIP_RES, _TITLE_RELATIVE_RE):
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _TITLE_BARE_MERIDIEM_RE.sub(" ", cleaned)
    cleaned = _TITLE_LEAD_RE.sub("", cleaned)
    cleaned = _TITLE_CONNECTIVE_RE.sub(" ", cleaned)
    cleaned = _TITLE_DANGLING_HEAD_RE.sub("", cleaned)
    # 空括号对要在折叠空白之前清掉，否则 `( )` 里的空格会挡住识别
    cleaned = _TITLE_EMPTY_BRACKETS_RE.sub(" ", cleaned)
    cleaned = _TITLE_GAP_RE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    cleaned = _TITLE_TAIL_RE.sub("", cleaned)
    # 反复剥末尾悬挂词（「会议改到」→「会议」）
    while True:
        stripped = _TITLE_DANGLING_TAIL_RE.sub("", cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned


#: 主题开头的机器标记：``[Important Reminder]`` ``【提醒】`` ``Invitation:`` 等。
#:
#: 这些是**邮件层的标记**，不是事件名的一部分，放进日历标题只是噪音。
_TITLE_SUBJECT_DECOR_RE = re.compile(
    r"^\s*(?:\[[^\]]{0,30}\]|【[^】]{0,30}】|\([^)]{0,30}\))\s*"
    r"|^\s*(?:invitation|reminder|重要提醒|提醒|通知|邀请|邀請)\s*[:：\-–—]\s*",
    re.IGNORECASE,
)


def _subject_title(subject: str | None) -> str | None:
    """从邮件主题派生标题（剥掉机器标记与其中夹带的日期），失败返回 ``None``。

    为什么需要它：``derive_title`` 优先用含时间的那句话派生标题，这对
    「會議定於 9月20日 15:00 舉行」这类短句很好。但英文邀请函的正文是长句
    （``In addition you are invited to join our grand …``），派生结果只能是
    截断的残句，而**主题往往正是这件事的名字**。

    实测：英文日期能被规则解析之后，这类邮件从「LLM 给出干净标题」退化成
    「规则给出残句标题」，因为去重按来源优先级保留了规则结果。
    """
    if not subject:
        return None
    cleaned = _strip_subject_decor(subject.strip())
    cleaned = _clean_fragment(cleaned)
    # 再剥一次：括号里的日期被 _clean_fragment 拿掉后，原本过长的括号标记
    # 可能已经短到可识别（``[For Completion by 10 am, 24 August 2026]``
    # → ``[For Completion ]`` → 只剩噪音）。
    cleaned = _strip_subject_decor(cleaned)
    if not _looks_like_title(cleaned):
        return None
    if len(cleaned) > TITLE_MAX_CHARS:
        return cleaned[:TITLE_MAX_CHARS].rstrip() + "…"
    return cleaned


def _strip_subject_decor(text: str) -> str:
    """反复剥掉主题开头的机器标记（可能叠加：「[Reminder] Invitation: …」）。"""
    cleaned = text
    while True:
        stripped = _TITLE_SUBJECT_DECOR_RE.sub("", cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned


def _looks_like_title(text: str) -> bool:
    """判断派生结果是否像个能读通的标题。

    太短（剥完只剩两三个字）或仍以连接词结尾的，都不适合当标题——
    此时邮件主题通常是更好的选择。
    """
    if len(text) < 4:
        return False
    # 仍以标点或连接词结尾 → 是残句
    if _TITLE_TAIL_RE.search(text) or _TITLE_DANGLING_TAIL_RE.search(text):
        return False
    return True


# ──────────────────────────────────────────────────────────────
# 抽取
# ──────────────────────────────────────────────────────────────

def _all_dates_in(text: str, received: datetime) -> list[tuple[date, bool, str]]:
    """找出一段文本中的**所有**日期（不只第一个）。

    必要性：同一段里可能出现多个日期，例如「9月15日有一场，9月17日有一场」。
    只取第一个会静默丢掉其它时间，而且**不会**触发「多候选 → 待审」——
    用户会看到一个貌似确定的错误结果。找出全部才能如实标记为歧义。
    """
    found: list[tuple[date, bool, str]] = []

    # 绝对日期（中文数字）：二零二六年十月一日
    for match in _ABS_DATE_CN_NUMERAL.finditer(text):
        year = _cn_year(match.group("year"))
        month = _cn_number(match.group("month"))
        day = _cn_number(match.group("day"))
        if year is None or month is None or day is None:
            continue
        try:
            found.append((date(year, month, day), True, match.group(0).strip()))
        except ValueError:
            continue

    if found:
        return found

    # 绝对日期（阿拉伯数字）：2026年9月20日 / 2026-09-20
    for match in _ABS_DATE_CN.finditer(text):
        try:
            value = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            continue
        found.append((value, True, match.group(0).strip()))

    if found:
        return found

    # 无年份（中文数字）：十月一日
    for match in _ABS_DATE_CN_NUMERAL_NO_YEAR.finditer(text):
        month = _cn_number(match.group("month"))
        day = _cn_number(match.group("day"))
        if month is None or day is None:
            continue
        resolved = _resolve_month_day(received, month, day)
        if resolved is not None:
            found.append((resolved, False, match.group(0).strip()))

    if found:
        return found

    for match in _ABS_DATE_NO_YEAR.finditer(text):
        month = int(match.group("month"))
        day = int(match.group("day"))
        resolved_date = _resolve_month_day(received, month, day)
        if resolved_date is not None:
            found.append((resolved_date, False, match.group(0).strip()))

    if found:
        return found

    # 英文月份名：``Aug 28, 2026 12:00 PM``（Zoom / Google / Outlook 邀请函的写法）。
    # 实测漏掉它会整类英文邀请函抽不出日期——预筛放行、规则空手而归，
    # 而流程照常报成功。
    #
    # 带年份的模式**先**匹配：它们更具体，且能避免把年份误当成日。
    for pattern in (_ABS_DATE_EN_MD, _ABS_DATE_EN_DM):
        for match in pattern.finditer(text):
            month = _EN_MONTHS.get(match.group("month").lower().rstrip("."))
            if month is None:
                continue
            try:
                found.append(
                    (date(int(match.group("year")), month, int(match.group("day"))),
                     True, match.group(0).strip())
                )
            except ValueError:
                continue
    if found:
        return found

    for pattern in (_ABS_DATE_EN_MD_NO_YEAR, _ABS_DATE_EN_DM_NO_YEAR):
        for match in pattern.finditer(text):
            month = _EN_MONTHS.get(match.group("month").lower().rstrip("."))
            if month is None:
                continue
            try:
                day = int(match.group("day"))
            except (TypeError, ValueError):
                continue
            resolved = _resolve_month_day(received, month, day)
            if resolved is not None:
                found.append((resolved, False, match.group(0).strip()))
    if found:
        return found

    relative = _parse_relative_date(text, received)
    if relative is not None:
        event_date, evidence = relative
        found.append((event_date, True, evidence))

    return found


def _all_times_in(text: str) -> list[tuple[int, int, str]]:
    """找出一段文本中的所有时刻，按出现顺序去重。"""
    positions: list[tuple[int, int, int, str]] = []
    # 中文数字时刻优先于阿拉伯数字：模式互斥，但先匹配能给出更完整的证据串
    for pattern in (_CN_NUMERAL_TIME, _CN_TIME, _COLON_TIME, _EN_TIME):
        for match in pattern.finditer(text):
            groups = match.groupdict()
            raw_hour = groups.get("hour") or "0"
            raw_minute = groups.get("minute") or "0"
            # 中文数字模式给出的是汉字，需要用换算函数；阿拉伯数字直接 int()
            if pattern is _CN_NUMERAL_TIME:
                # 缺省的分应为 0；注意不能把 ASCII "0" 交给中文数字换算函数，
                # 它只认汉字，会返回 None 导致整条时刻被丢弃
                # （实测：『下午三時』『晚上七點』曾因此完全识别不出来）。
                hour = _cn_number(raw_hour)
                if groups.get("exact"):
                    minute = 0
                elif groups.get("minute"):
                    minute = _cn_number(groups["minute"])
                else:
                    minute = 0
            else:
                hour = int(raw_hour)
                minute = int(raw_minute)
            if hour is None or minute is None:
                continue
            if "period" in groups:
                hour = _apply_period(hour, groups.get("period"))
            if "ampm" in groups and groups.get("ampm"):
                hour = _apply_ampm(hour, groups["ampm"])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                continue
            positions.append((match.start(), hour, minute, match.group(0).strip()))

    if not positions:
        return []

    # 按出现位置排序；同一位置（不同正则都命中）只留一个
    positions.sort(key=lambda item: item[0])
    result: list[tuple[int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    for _pos, hour, minute, evidence in positions:
        if (hour, minute) in seen:
            continue
        seen.add((hour, minute))
        result.append((hour, minute, evidence))
    return result


def extract_by_rules(
    *,
    text: str,
    subject: str,
    received_at: datetime,
    user_timezone: str = "Asia/Shanghai",
    default_duration_minutes: int = 30,
) -> RuleResult:
    """从清洗后的正文与标题中按规则抽取事件候选。

    ``received_at`` 是相对时间与无年份日期的基准（规格 §8：以**收信时间**为基准，
    不是发件人当地时间）。

    逐段扫描：一段通常描述一件事，避免跨段把不同日期与时刻错配。但**段内**
    可能出现多个日期，因此会逐个产出，并据此触发「多候选 → 待审」。
    """
    tz = ZoneInfo(user_timezone)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    received_local = received_at.astimezone(tz)

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()] if text.strip() else []

    deadline = _is_deadline(text, subject)
    hits: list[RuleHit] = []
    seen: set[str] = set()
    #: 每段解析出的日期数量，用于判断「同段多候选」
    dates_per_paragraph: list[int] = []

    for paragraph in paragraphs:
        # 免责声明／脚注里的日期不是用户事件。实测这是审核队列的主要噪音源
        # （银行邮件脚注含「截至…App Store 评分」，同一指纹重复出现 7 次）。
        if is_footnote_paragraph(paragraph):
            continue

        dates = _all_dates_in(paragraph, received_local)
        dates_per_paragraph.append(len(dates))
        if not dates:
            continue

        times = _all_times_in(paragraph)

        for index, (event_date, has_year, evidence_text) in enumerate(dates):
            # 时刻与日期的配对：按序对齐——第一个日期配第一个时刻，
            # 第二个配第二个。数量不等时，多出的日期视为「无时刻」，
            # 多出的时刻归到最后一个日期。这是启发式，因此多个日期时
            # 整体会被标为歧义（见 RuleResult.ambiguous）。
            if index < len(times):
                hour, minute, time_evidence = times[index]
                has_time = True
                start = datetime.combine(event_date, time(hour, minute), tzinfo=tz)
            else:
                hour = minute = None
                has_time = False
                time_evidence = ""
                start = datetime.combine(event_date, time(0, 0), tzinfo=tz)

            slot = f"{hour:02d}:{minute:02d}" if has_time else ""
            dedupe_key = f"{event_date.isoformat()}|{slot}"
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            parts = [p for p in (evidence_text, time_evidence) if p]
            evidence = " + ".join(parts) if parts else evidence_text

            title, title_from_subject = derive_title_ex(
                paragraph, subject=subject, fallback=subject or "(无标题)"
            )
            hits.append(
                RuleHit(
                    title=title,
                    title_from_subject=title_from_subject,
                    start=start,
                    end=None if deadline else start + timedelta(minutes=default_duration_minutes),
                    all_day=deadline or not has_time,
                    evidence=evidence,
                    has_time=has_time,
                    has_year=has_year,
                    is_deadline=deadline,
                    vague=_is_vague(paragraph),
                    kind="deadline" if deadline else "event",
                )
            )

    # ``ambiguous`` 只表示「**同一段内**出现多个候选日期」——那才真的需要
    # 人工裁决哪个才是本段所指。
    #
    # 不能用「全文有多个命中」：一封正常邮件常含多件独立事件
    # （实测：升旗禮 10-01 + 登记截止 09-21），把它们整体标为「互相冲突」
    # 会让所有候选都进待审，等于把正常情况当成异常。
    ambiguous = len(hits) > 1 and any(count > 1 for count in dates_per_paragraph)
    return RuleResult(hits=hits, ambiguous=ambiguous)

def base_weight(hit: RuleHit) -> float:
    """按模式类型给基础权重（规格 §5）。"""
    if hit.is_deadline:
        # 截止类：有明确日期即算「绝对日期无时刻」档；有具体时刻则升到最高档
        return W_ABSOLUTE_WITH_TIME if hit.has_time else W_ABSOLUTE_NO_TIME
    if hit.has_time:
        return W_ABSOLUTE_WITH_TIME
    return W_ABSOLUTE_NO_TIME


def score_confidence(hit: RuleHit, *, multiple_candidates: bool) -> float:
    """计算规则命中的置信度。

    ``w × 特异性系数 × 一致性系数``，模糊词再乘折扣（规格 §5）。
    阈值 0.85 是**严格大于**，因此只有 0.95 档（绝对日期 + 显式时刻、
    非模糊）才能自动入历。
    """
    weight = base_weight(hit)
    specificity = SPECIFICITY_WITH_YEAR if hit.has_year else SPECIFICITY_WITHOUT_YEAR
    consistency = CONSISTENCY_MULTI if multiple_candidates else CONSISTENCY_SINGLE

    score = weight * specificity * consistency
    if hit.vague:
        score *= VAGUE_FACTOR
    return round(min(score, 1.0), 4)


def evaluate_hit(
    hit: RuleHit,
    *,
    received_at: datetime,
    user_timezone: str,
    confidence_auto_push_threshold: float,
    ambiguous_date_policy: str = "pending",
    multiple_candidates: bool = False,
    now: datetime | None = None,
) -> tuple[float, bool, str]:
    """评估一个命中，返回 ``(confidence, requires_review, review_reason)``。

    强制待审的情形（规格 §4）：

    * **时间早于当前时刻**（不再是「未来要发生的事」）
    * **时间早于收信时间**（规格 §0-A）
    * 无年份日期（**硬门**，不受 ambiguous_date_policy 影响）
    * 模糊时间表达
    * 多候选冲突
    * 置信度不达标

    ``now`` 用于「早于当前时刻」判定，默认真实当前时间。**这条判据是实测补上的**：
    首次对一个有历史邮件的邮箱做同步时，大量早已过去的「事件」（例如两个月前的
    域名到期日）会被抽出来。它们语义上没错，但把过去的时间写进日历纯属污染——
    日历是用来规划未来的。真实数据上测出「4 个可自动入历候选全是过去时间」，
    自动入历准确率因此从 100% 掉到 0%。
    """
    confidence = score_confidence(hit, multiple_candidates=multiple_candidates)

    tz = ZoneInfo(user_timezone)
    received_local = (
        received_at.astimezone(tz) if received_at.tzinfo else received_at.replace(tzinfo=tz)
    )
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    current_local = current.astimezone(tz)

    # 已是过去的事件 → 待审。放在最前面：无论置信度多高、来源多可靠，
    # 过去的事件都不该自动写进日历。
    if hit.start < current_local:
        return confidence, True, "时间已过（早于当前时刻，不建议入历）"

    if not hit.has_year:
        # 硬门优先于一切：无年份意味着可能指错误的年份
        return confidence, True, "无年份日期（硬门：需确认年份）"

    if multiple_candidates:
        # ambiguous_date_policy=earliest 时由上层取最早；这里仍标记需裁决
        if ambiguous_date_policy == "earliest":
            pass
        else:
            return confidence, True, "同一文本内存在多个候选时间"

    if hit.vague:
        return confidence, True, "时间表达模糊（含「左右/前后」等）"

    if hit.start < received_local:
        # 规格 §0-A：早于收信时间一律待审，无 grace 例外
        return confidence, True, "时间早于收信时间（可能已过期）"

    if confidence <= confidence_auto_push_threshold:
        return confidence, True, f"置信度 {confidence} 未超过自动入历门槛"

    return confidence, False, ""


def hit_to_candidate_fields(hit: RuleHit) -> dict[str, object]:
    """把命中转成 ``events`` 表字段（供 pipeline 组装候选）。"""
    return {
        "title": hit.title,
        "start_ts": hit.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_ts": (
            hit.end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if hit.end else None
        ),
        "all_day": hit.all_day,
        "location": None,
        "organizer": None,
        "source": EventSource.RULES,
        "fingerprint": compute_fingerprint(
            title=hit.title,
            start_ts=hit.start.astimezone(UTC).isoformat(),
            end_ts=hit.end.astimezone(UTC).isoformat() if hit.end else None,
            organizer=None,
            location=None,
        ),
    }
