"""合成评测语料：邮件样本 + 人工标注真值。

为什么用代码生成而不是 ``.eml`` 文件：

* ``.eml`` 会被 .gitignore 排除（真实邮件不能入库），导致语料不可复现；
* 代码形式可审阅、可版本化、可扩展，且明确是**合成数据**；
* 真值标注与样本放在一起，改动样本时会立刻暴露真值未同步的问题。

语料取材于真机观察到的真实模式（Zoom 会议邀请、阿里云域名到期、香港入境处
预约、银行入账、大学迎新日等），但**所有内容均为编造**，不含真实邮件数据。

真值格式：每封邮件对应 ``GroundTruth``，列出**应被抽出的事件**。
评测按事件而非按邮件计分——因为一封邮件可能含 0 个或多个事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# ──────────────────────────────────────────────────────────────
# 真值结构
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExpectedEvent:
    """一个人工标注的应抽事件。"""

    title_contains: str
    """标题关键字（宽松匹配，避免因措辞差异误判）。"""

    start_date: date
    start_time: str | None = None
    """``HH:MM``；None 表示全天或无需精确时刻。"""

    kind: str = "event"
    """``event``（会议/活动）或 ``deadline``（截止/到期）。"""

    tolerance_minutes: int = 0
    """允许的开始时刻误差（分钟）。用于容忍合理歧义。"""


@dataclass(frozen=True, slots=True)
class MailSample:
    """一封样本邮件。"""

    name: str
    raw: bytes
    expected: tuple[ExpectedEvent, ...] = ()
    note: str = ""
    """该样本考察什么（写在评测报告里便于定位）。"""

    @property
    def has_expectation(self) -> bool:
        return bool(self.expected)


@dataclass(slots=True)
class Corpus:
    samples: list[MailSample] = field(default_factory=list)

    @property
    def total_expected(self) -> int:
        return sum(len(s.expected) for s in self.samples)

    @property
    def with_events(self) -> list[MailSample]:
        return [s for s in self.samples if s.has_expectation]

    @property
    def without_events(self) -> list[MailSample]:
        """不应抽出任何事件的邮件——用于衡量误报（噪音过滤质量）。"""
        return [s for s in self.samples if not s.has_expectation]


# ──────────────────────────────────────────────────────────────
# 构邮件工具
# ──────────────────────────────────────────────────────────────

BASE_RECEIVED = datetime(2026, 9, 14, 10, 0, 0)
"""多数样本的收信时间。相对时间表达（「下周」「明天」）以此为基准。"""


def build_mail(
    *,
    from_addr: str,
    subject: str,
    body: str,
    message_id: str,
    date: str = "Mon, 14 Sep 2026 10:00:00 +0800",
    to_addr: str = "me@163.com",
    extra_headers: str = "",
    content_type: str = "text/plain; charset=utf-8",
    html: str | None = None,
    ics: str | None = None,
) -> bytes:
    """构造一封原始邮件字节。

    参数 ``html`` 与 ``ics`` 用于构造多部分邮件；两者与 ``body`` 可共存。
    """
    if ics is not None and html is not None:
        raise ValueError("本工具不支持同时构造 html 与 ics 多部分邮件")

    headers = [
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Subject: {subject}",
        f"Message-ID: {message_id}",
        f"Date: {date}",
        "MIME-Version: 1.0",
    ]
    if extra_headers:
        headers.append(extra_headers.rstrip("\r\n"))

    if ics is not None:
        boundary = "BOUNDARY-ICS"
        headers.append(f'Content-Type: multipart/mixed; boundary="{boundary}"')
        parts = [
            f"--{boundary}",
            "Content-Type: text/plain; charset=utf-8",
            "",
            body,
            f"--{boundary}",
            "Content-Type: text/calendar; charset=utf-8; method=REQUEST",
            "",
            ics.strip(),
            f"--{boundary}--",
            "",
        ]
        return ("\r\n".join(headers) + "\r\n\r\n" + "\r\n".join(parts)).encode("utf-8")

    if html is not None:
        boundary = "BOUNDARY-ALT"
        headers.append(f'Content-Type: multipart/alternative; boundary="{boundary}"')
        parts = [
            f"--{boundary}",
            "Content-Type: text/plain; charset=utf-8",
            "",
            body,
            f"--{boundary}",
            "Content-Type: text/html; charset=utf-8",
            "",
            html,
            f"--{boundary}--",
            "",
        ]
        return ("\r\n".join(headers) + "\r\n\r\n" + "\r\n".join(parts)).encode("utf-8")

    headers.append(f"Content-Type: {content_type}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


def build_ics(
    *,
    uid: str,
    summary: str,
    dtstart: str,
    dtend: str,
    location: str = "",
    organizer: str = "organizer@example.com",
    method: str = "REQUEST",
    sequence: int = 0,
    extra: str = "",
) -> str:
    """构造一个 VCALENDAR。"""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//auto-mail test//EN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{summary}",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    if organizer:
        lines.append(f"ORGANIZER;CN=Organizer:mailto:{organizer}")
    if extra:
        lines.append(extra.strip())
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 语料：正例（应抽出事件）
# ──────────────────────────────────────────────────────────────


def _positive_samples() -> list[MailSample]:
    samples: list[MailSample] = []

    # ① ICS 会议邀请（Zoom）——最高置信来源，零 LLM
    samples.append(
        MailSample(
            name="ics_zoom_invite",
            raw=build_mail(
                from_addr="no-reply@zoom.us",
                subject="Welcoming Day for Non-Local Postgraduate Students",
                body="You are invited to the Welcoming Day.\n\nJoin Zoom Meeting",
                message_id="<zoom-1@zoom.us>",
                ics=build_ics(
                    uid="zoom-meeting-001@zoom.us",
                    summary="Welcoming Day for Non-Local Postgraduate Students",
                    dtstart="20260918T020000Z",  # 10:00 +0800
                    dtend="20260918T040000Z",
                    location="https://zoom.us/j/123456789",
                    organizer="pgso@example.edu.hk",
                ),
            ),
            expected=(
                ExpectedEvent(
                    title_contains="Welcoming Day",
                    start_date=date(2026, 9, 18),
                    start_time="10:00",
                ),
            ),
            note="ICS 直解：应零 LLM 命中，confidence 最高",
        )
    )

    # ② 域名到期（阿里云）——绝对日期、无时刻，属 deadline
    samples.append(
        MailSample(
            name="alibaba_domain_expiry",
            raw=build_mail(
                from_addr="system@notice.aliyun.com",
                subject="阿里云域名到期提醒",
                body=(
                    "尊敬的用户：\n\n"
                    "您的域名 example.com 将于 2026年10月8日 到期，"
                    "为避免影响正常使用，请及时续费。\n\n"
                    "阿里云\n"
                ),
                message_id="<aliyun-1@notice.aliyun.com>",
            ),
            expected=(
                ExpectedEvent(
                    title_contains="域名",
                    start_date=date(2026, 10, 8),
                    kind="deadline",
                ),
            ),
            note="绝对日期无时刻 → 全天截止事件",
        )
    )

    # ③ 香港入境处预约——繁体中文 + 明确时刻
    samples.append(
        MailSample(
            name="hk_immigration_appointment",
            raw=build_mail(
                from_addr="appointment_reminder@immd.gov.hk",
                subject="入境事務處預約確認通知",
                body=(
                    "敬啟者：\n\n"
                    "你已預約於 2026年9月20日 上午11時15分 前往 "
                    "入境事務處總部大樓 辦理手續。\n\n"
                    "請準時到達。\n"
                ),
                message_id="<immd-1@immd.gov.hk>",
            ),
            expected=(
                ExpectedEvent(
                    title_contains="預約",
                    start_date=date(2026, 9, 20),
                    start_time="11:15",
                ),
            ),
            note="繁体中文 + 「上午11時15分」这类非常规时刻表达",
        )
    )

    # ④ 面试通知——相对时间「下周三」
    samples.append(
        MailSample(
            name="interview_next_wednesday",
            raw=build_mail(
                from_addr="hr@company.com",
                subject="面试通知",
                body=(
                    "您好，\n\n"
                    "很高兴通知您，面试安排在下周三下午2点，地点为 B 座 302 会议室。\n\n"
                    "HR 部\n"
                ),
                message_id="<interview-1@company.com>",
            ),
            expected=(
                ExpectedEvent(
                    title_contains="面试",
                    start_date=date(2026, 9, 23),  # 2026-09-14 是周一，下周三 = 9/23
                    start_time="14:00",
                ),
            ),
            note="相对时间「下周三」：以收信时间为基准，需正确跨周",
        )
    )

    # ⑤ 报名截止——相对时间「月底前」
    samples.append(
        MailSample(
            name="registration_deadline",
            raw=build_mail(
                from_addr="events@university.edu",
                subject="[Evaluation] Welcoming Day Evaluation Form",
                body=(
                    "Dear students,\n\n"
                    "Please complete the evaluation form before 2026-09-30 23:59.\n\n"
                    "Office of Student Affairs\n"
                ),
                message_id="<eval-1@university.edu>",
            ),
            expected=(
                ExpectedEvent(
                    title_contains="Evaluation",
                    start_date=date(2026, 9, 30),
                    kind="deadline",
                    tolerance_minutes=60,
                ),
            ),
            note="英文 + ISO 日期；截止语义",
        )
    )

    # ⑥ 航班行程
    samples.append(
        MailSample(
            name="flight_itinerary",
            raw=build_mail(
                from_addr="noreply@airline.com",
                subject="您的行程单 - 9月25日 香港往上海",
                body=(
                    "尊敬的旅客：\n\n"
                    "您的航班 CX368 将于 2026年9月25日 09:30 从香港国际机场起飞。\n"
                    "请于起飞前 2 小时抵达机场。\n"
                ),
                message_id="<flight-1@airline.com>",
            ),
            expected=(
                ExpectedEvent(
                    title_contains="航班",
                    start_date=date(2026, 9, 25),
                    start_time="09:30",
                ),
            ),
            note=(
                "只标注**明确写出**的起飞时刻 09:30。"
                "「起飞前 2 小时」（07:30）是派生时间，规格不要求推算——"
                "若要求推算，会把「提前 N 分钟到场」这类提示全变成事件。"
            ),
        )
    )

    # ⑦ 账单到期
    samples.append(
        MailSample(
            name="credit_card_due",
            raw=build_mail(
                from_addr="notification@service.bank.example",
                subject="信用卡账单提醒",
                body=(
                    "尊敬的客户：\n\n"
                    "您的信用卡账单已出，最低还款额为 HKD 500，"
                    "还款到期日为 2026年9月28日。\n\n"
                    "请于到期日前完成还款。\n"
                ),
                message_id="<bank-1@bank.example>",
            ),
            expected=(
                ExpectedEvent(
                    title_contains="还款",
                    start_date=date(2026, 9, 28),
                    kind="deadline",
                ),
            ),
            note="「到期日」语义 → 截止类",
        )
    )

    # ⑧ HTML-only 邮件（无 text/plain 部分）
    samples.append(
        MailSample(
            name="html_only_meeting",
            raw=build_mail(
                from_addr="organizer@team.example",
                subject="Team Sync",
                body="",
                message_id="<team-1@team.example>",
                html=(
                    "<html><body><p>Team Sync 将于 "
                    "<b>2026年9月22日 15:00</b> 在线上举行。</p>"
                    "<p><a href='https://x.com/t?id=1'>点击加入</a></p>"
                    "</body></html>"
                ),
            ),
            expected=(
                ExpectedEvent(
                    title_contains="Sync",
                    start_date=date(2026, 9, 22),
                    start_time="15:00",
                ),
            ),
            note="HTML-only：必须转文本后再抽取",
        )
    )

    # ⑨ 更新后的 ICS（更高 SEQUENCE）——考察更新语义
    samples.append(
        MailSample(
            name="ics_rescheduled",
            raw=build_mail(
                from_addr="no-reply@zoom.us",
                subject="Updated invitation: Project Review",
                body="The meeting has been rescheduled.",
                message_id="<zoom-2@zoom.us>",
                ics=build_ics(
                    uid="zoom-meeting-002@zoom.us",
                    summary="Project Review",
                    dtstart="20260921T060000Z",  # 14:00 +0800
                    dtend="20260921T070000Z",
                    organizer="boss@example.com",
                    sequence=1,
                ),
            ),
            expected=(
                ExpectedEvent(
                    title_contains="Project Review",
                    start_date=date(2026, 9, 21),
                    start_time="14:00",
                ),
            ),
            note="带 SEQUENCE 的更新；应产生同 ics_uid 的更高 SEQUENCE 候选",
        )
    )

    # ⑩ 引用旧邮件的邮件——旧邮件里的日期必须被清洗掉
    samples.append(
        MailSample(
            name="quoted_old_date",
            raw=build_mail(
                from_addr="colleague@company.com",
                subject="Re: 会议安排",
                body=(
                    "确认一下，会议改到 2026年9月24日 上午10点。\n\n"
                    "在 2026年8月1日 写道：\n"
                    "> 原定 2026年8月15日 的会议需要调整\n"
                    "> 上次我们说的是 2025年12月20日\n"
                ),
                message_id="<reply-1@company.com>",
            ),
            expected=(
                ExpectedEvent(
                    title_contains="会议",
                    start_date=date(2026, 9, 24),
                    start_time="10:00",
                ),
            ),
            note="★ 清洗效果测试：只应抽出 9/24，绝不能抽出 8/15 或 2025/12/20",
        )
    )

    # ⑪ 无年份日期——硬门，必须进待审而不是自动入历
    samples.append(
        MailSample(
            name="no_year_date",
            raw=build_mail(
                from_addr="friend@example.com",
                subject="聚会安排",
                body="我们定在 9月20日 晚上7点 聚餐吧。\n",
                message_id="<party-1@example.com>",
            ),
            expected=(
                ExpectedEvent(
                    title_contains="聚餐",
                    start_date=date(2026, 9, 20),
                    start_time="19:00",
                ),
            ),
            note="★ 无年份 → 必须 pending（硬门），即便抽出来了也不得自动入历",
        )
    )

    return samples


# ──────────────────────────────────────────────────────────────
# 语料：反例（不应抽出任何事件）
# ──────────────────────────────────────────────────────────────


def _negative_samples() -> list[MailSample]:
    samples: list[MailSample] = []

    # 营销邮件：大量日期但都不是用户的事件
    samples.append(
        MailSample(
            name="marketing_promo",
            raw=build_mail(
                from_addr="moneyback@member.moneyback.example",
                subject="下載易賞錢App 3步即享優惠！",
                body=(
                    "優惠期至 2026年12月31日。\n\n"
                    "立即下載 App，即可獲得積分。\n"
                    "條款及細則請參閱官網。\n\n"
                    "© 2026 Example Rewards Ltd 版權所有\n"
                    "如不想收到推廣郵件，請按此退訂\n"
                ),
                message_id="<promo-1@moneyback.example>",
                extra_headers="List-Unsubscribe: <mailto:unsub@moneyback.example>",
            ),
            note="营销邮件：日期不构成用户事件，应被预筛过滤",
        )
    )

    # 自动回复
    samples.append(
        MailSample(
            name="auto_reply",
            raw=build_mail(
                from_addr="noreply@example.com",
                subject="自动回复：Re: 咨询",
                body=(
                    "您好，我目前休假中，将于 2026年9月20日 返回工作岗位。\n"
                    "如有紧急事务请联系 backup@example.com。\n"
                ),
                message_id="<autoreply-1@example.com>",
                extra_headers="Auto-Submitted: auto-replied",
            ),
            note="自动回复：虽是「返回日期」，但不是需要入历的事件",
        )
    )

    # 会议纪要（过去时间）
    samples.append(
        MailSample(
            name="meeting_minutes_past",
            raw=build_mail(
                from_addr="colleague@company.com",
                subject="上周会议纪要",
                body=(
                    "各位好，\n\n"
                    "我们于 2026年9月8日 14:00 召开的会议纪要如下：\n"
                    "1. 项目进度正常\n"
                    "2. 下阶段目标已明确\n\n"
                    "谢谢。\n"
                ),
                message_id="<minutes-1@company.com>",
            ),
            # 真值：抽出该时间点**是允许的**（它确实被明确写出），
            # 关键是它必须被标为待审（早于收信时间），绝不能自动入历。
            expected=(
                ExpectedEvent(
                    title_contains="会议纪要",
                    start_date=date(2026, 9, 8),
                    start_time="14:00",
                ),
            ),
            note=(
                "★ 过去时间：抽出后被「早于收信时间」拦截为待审。"
                "评测关注的是**它没有被自动入历**（见「危险的自动入历」一节），"
                "而不是它没被抽出。"
            ),
        )
    )

    # 验证码邮件
    samples.append(
        MailSample(
            name="verification_code",
            raw=build_mail(
                from_addr="noreply@service.example",
                subject="您的验证码",
                body="您的验证码是 123456，5分钟内有效。请勿泄露给他人。\n",
                message_id="<otp-1@service.example>",
            ),
            note="验证码：无事件时间，应过滤",
        )
    )

    # 无日期的普通通知
    samples.append(
        MailSample(
            name="plain_notice",
            raw=build_mail(
                from_addr="noreply@service.example",
                subject="服务条款更新通知",
                body=(
                    "我们更新了服务条款，主要变更如下：\n"
                    "1. 隐私政策调整\n"
                    "2. 数据处理流程优化\n\n"
                    "如有疑问请联系客服。\n"
                ),
                message_id="<terms-1@service.example>",
            ),
            note="无任何时间信息",
        )
    )

    # 含「会议」词但无具体时间
    samples.append(
        MailSample(
            name="meeting_without_time",
            raw=build_mail(
                from_addr="colleague@company.com",
                subject="想约个会",
                body="最近想找你聊聊项目进展，方便的时候约个会议吧。\n",
                message_id="<vague-1@company.com>",
            ),
            note="★ 有事件意图但无时间 → 无法入历，不应产生候选",
        )
    )

    return samples


def build_corpus() -> Corpus:
    """构造完整语料。"""
    return Corpus(samples=[*_positive_samples(), *_negative_samples()])


#: 语料中的样本总数与预期事件数（供评测断言使用）
def corpus_stats() -> dict[str, int]:
    corpus = build_corpus()
    return {
        "samples": len(corpus.samples),
        "positive": len(corpus.with_events),
        "negative": len(corpus.without_events),
        "expected_events": corpus.total_expected,
    }
