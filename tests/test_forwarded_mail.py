"""真实转发邮件暴露的 6 个缺陷的回归测试。

样本：示例大学升旗礼邀请函（被转发），正文用**中文数字**写日期时刻、
表格排版把日期与时刻分在不同段落、并含多个未来时间点。

这封邮件一次性暴露了 6 个缺陷，全部打在我一直无法验证的
「识别未来事件」能力上：

| # | 缺陷 | 后果 |
|---|---|---|
| 1 | 中文数字日期（`二零二六年十月一日`）不被识别 | 整类中文正式通知完全漏掉 |
| 2 | 中文数字时刻（`上午十時三十分`）不被识别 | 时刻全部丢失 |
| 3 | 转发头部（`发送时间: …`）未剥离 | 误导预筛器跳过 LLM，并抽出「发送时间」假事件 |
| 4 | 括号里的星期几被当成相对日期 | 产出**凭空的错误日期**（10-01 变成 09-17 的下一个周四） |
| 5 | 预筛器用「全文含日期+时刻」推断规则能覆盖 | 表格排版下规则配不上，LLM 被跳过 |
| 6 | 时刻槽按 **UTC 小时**分组 | 本地 10:00 与 10:30 同属 UTC 02 点 → LLM 抽对的事件被**静默丢弃** |
| 7 | 「短且无标点」被当作营销页脚 | 表格排版的日程行是**唯一的时刻来源**，却被整段删掉 |
| 8 | 标题派生的两处误剥（空括号、裸单字助词吃首字） | 标题读不通（`議 舉行`、`… ( ) 或之前`） |
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from automail.extract.fingerprint import title_similarity
from automail.extract.prefilter import classify
from automail.extract.rules import (
    _all_dates_in,
    _all_times_in,
    _cn_number,
    _cn_year,
    _inside_brackets,
    extract_by_rules,
)
from automail.mail.mime import clean_body

TZ = ZoneInfo("Asia/Shanghai")
RECEIVED = datetime(2026, 9, 16, 6, 40, tzinfo=TZ)


def _local_day(candidate, tz: ZoneInfo) -> str:
    """候选开始时刻的**当地**自然日（比对 UTC 字符串是缺陷 6 的成因）。"""

    from automail.db import parse_iso

    parsed = parse_iso(candidate.start_ts)
    assert parsed is not None, f"无法解析 {candidate.start_ts!r}"
    return parsed.astimezone(tz).date().isoformat()


# ══════════════════════════════════════════════════════════════
# 缺陷 1：中文数字日期
# ══════════════════════════════════════════════════════════════


def test_chinese_numeral_year_conversion() -> None:
    """年份是**逐字拼接**：二零二六 → 2026，不是按十进位读法。"""
    assert _cn_year("二零二六") == 2026
    assert _cn_year("一九九八") == 1998
    assert _cn_year("二〇二六") == 2026  # 〇 也是零
    assert _cn_year("二○二六") == 2026  # ○ 也是零


def test_chinese_numeral_number_conversion() -> None:
    """1~99 的十进位读法 + 单位数。"""
    assert _cn_number("十") == 10
    assert _cn_number("十五") == 15
    assert _cn_number("二十") == 20
    assert _cn_number("二十一") == 21
    assert _cn_number("三十") == 30
    assert _cn_number("三") == 3
    assert _cn_number("十二") == 12
    # 防御：ASCII 数字也应接受（字符串里偶尔混入）
    assert _cn_number("0") == 0
    assert _cn_number("15") == 15
    assert _cn_number("") is None


def test_chinese_numeral_full_date() -> None:
    """`二零二六年十月一日` 必须解析成 2026-10-01（实测漏掉的真实事件日）。"""
    found = _all_dates_in("謹訂於二零二六年十月一日舉行升旗禮", RECEIVED)
    assert found, "中文数字日期必须被识别"
    assert found[0][0].isoformat() == "2026-10-01"
    assert found[0][1] is True, "含年份 → has_year 为真"


def test_chinese_numeral_date_no_year() -> None:
    found = _all_dates_in("十月一日舉行", RECEIVED)
    assert found
    assert found[0][0].isoformat() == "2026-10-01"
    assert found[0][1] is False, "无年份 → has_year 为假（触发硬门待审）"


def test_chinese_numeral_date_in_extraction() -> None:
    """端到端：中文数字日期能抽出候选。"""
    result = extract_by_rules(
        text="謹訂於二零二六年十月一日舉行升旗禮。",
        subject="升旗禮邀請",
        received_at=RECEIVED,
    )
    assert len(result.hits) == 1
    assert result.hits[0].start.date().isoformat() == "2026-10-01"


# ══════════════════════════════════════════════════════════════
# 缺陷 2：中文数字时刻
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("text", "hour", "minute"),
    [
        ("上午十時三十分", 10, 30),
        ("上午十時正", 10, 0),      # 「正」= 整点
        ("下午三時", 15, 0),        # 无分钟
        ("晚上七點", 19, 0),        # 繁体「點」
        ("上午九时零五分", 9, 5),
        ("下午二時二十分", 14, 20),
    ],
)
def test_chinese_numeral_time(text: str, hour: int, minute: int) -> None:
    """中文数字时刻必须解析。

    「无分钟」那一类是实测 bug 来源：曾把 ASCII ``"0"`` 交给中文数字换算
    函数，它只认汉字、返回 None，导致 `下午三時` `晚上七點` 整条被丢弃。
    """
    found = _all_times_in(text)
    assert found, f"{text!r} 应被识别"
    assert found[0][:2] == (hour, minute)


def test_chinese_numeral_time_combines_with_date() -> None:
    """同段内的中文数字日期 + 时刻应组合成一个带时刻的事件。"""
    result = extract_by_rules(
        text="升旗禮於二零二六年十月一日上午十時三十分舉行。",
        subject="升旗禮",
        received_at=RECEIVED,
    )
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.start.strftime("%Y-%m-%d %H:%M") == "2026-10-01 10:30"
    assert not hit.all_day


# ══════════════════════════════════════════════════════════════
# 缺陷 3：转发头部
# ══════════════════════════════════════════════════════════════

FORWARDED = """________________________________
发件人: Communications Office <office@example.edu.hk>
发送时间: 2026年9月15日 10:51
收件人: ZHANG, San <s1234567@link.example.edu.hk>
主题: 示例大學 升旗禮

各位同學：

謹訂於二零二六年十月一日舉行升旗禮。

升旗禮

上午十時三十分 | 中央廣場升旗台
"""


def test_forward_header_is_stripped() -> None:
    """转发头部必须剥离——它含自己的日期时刻，会污染判断。"""
    cleaned = clean_body(FORWARDED)
    assert "发送时间" not in cleaned
    assert "发件人" not in cleaned
    assert "10:51" not in cleaned, "头部里的时刻可能被误抽成事件"
    # 正文必须完整保留（关键：转发邮件的有用内容在头部之后）
    assert "二零二六年十月一日" in cleaned
    assert "上午十時三十分" in cleaned


def test_forward_header_stripping_does_not_truncate_body() -> None:
    """头部剥离是**替换型**，不是截断型——不能连带删掉正文。

    这与「引用旧邮件」的处理不同：那种是截断型（命中标记后全丢），
    因为内容在标记**之前**。转发头部则必须只删自己。
    """
    cleaned = clean_body(FORWARDED)
    assert "各位同學" in cleaned
    assert len(cleaned) > 40


def test_forwarded_mail_does_not_yield_send_time_event() -> None:
    """转发邮件不应抽出「发送时间」这个假事件。"""
    from email import policy
    from email.message import EmailMessage

    from automail.mail.mime import parse_message
    from automail.mail.sync import parse_raw_message  # noqa: F401

    msg = EmailMessage()
    msg["From"] = "a@b.com"
    msg["Subject"] = "转发：升旗禮"
    msg["Message-ID"] = "<f@b.com>"
    msg["Date"] = "Tue, 15 Sep 2026 22:40:53 +0800"
    msg.set_content(FORWARDED)

    parsed = parse_message(
        __import__("email").message_from_bytes(
            msg.as_bytes(), policy=policy.default
        ),
        max_chars=4000,
    )
    result = extract_by_rules(
        text=parsed.body.text, subject=parsed.subject, received_at=RECEIVED
    )
    labels = [h.evidence for h in result.hits]
    assert not any("10:51" in e for e in labels), f"不应抽出发送时间：{labels}"


# ══════════════════════════════════════════════════════════════
# 缺陷 4：括号里的星期几
# ══════════════════════════════════════════════════════════════


def test_inside_brackets_detection() -> None:
    assert _inside_brackets("十月一日（星期四）", 6) is True
    assert _inside_brackets("十月一日 (星期四)", 6) is True
    assert _inside_brackets("星期四開會", 0) is False
    assert _inside_brackets("（已取消）星期四開會", 7) is False  # 括号已闭合


def test_bracketed_weekday_is_not_a_date() -> None:
    """**实测 bug**：`二零二六年十月一日（星期四）` 里的「星期四」曾被当独立
    日期，算出收信后下一个周四（2026-09-17）——一个凭空的错误日期。

    现在中文数字日期能匹配了，括号守卫进一步确保不会仅凭括号内星期几造日期。
    """
    result = extract_by_rules(
        text="日期\n\n二零二六年十月一日（星期四）",
        subject="邀請",
        received_at=RECEIVED,
    )
    days = [h.start.date().isoformat() for h in result.hits]
    assert "2026-09-17" not in days, "不得凭括号里的星期几造出日期"
    assert "2026-10-01" in days


def test_bare_weekday_still_works() -> None:
    """括号**外**的星期几仍应作为相对日期解析（不能因守卫而全禁）。"""
    result = extract_by_rules(
        text="下星期三下午三點開會。", subject="會議", received_at=RECEIVED
    )
    assert result.hits, "无括号的「下星期三」应正常解析"


# ══════════════════════════════════════════════════════════════
# 缺陷 5：预筛器的配对判据
# ══════════════════════════════════════════════════════════════

TABLE_LAYOUT = """謹訂於二零二六年十月一日舉行升旗禮，詳情如下：

日期

二零二六年十月一日（星期四）

升旗禮

上午十時三十分 | 中央廣場升旗台

敬希於二零二六年九月二十一日或之前登記。
"""


def test_prefilter_requests_llm_when_date_and_time_split() -> None:
    """**实测 bug**：表格排版把日期与时刻分在不同段落。

    预筛器原用「全文含日期 + 全文含时刻」判断「规则能覆盖」，
    但规则是**逐段**处理的——分处两段时规则永远配不上，LLM 本应被调用
    却被跳过，导致整封邮件的时刻信息全丢。
    """
    verdict = classify(subject="升旗禮邀請", text=TABLE_LAYOUT)
    assert verdict.extract is True
    assert verdict.call_llm is True, "日期与时刻分处不同段落时必须让 LLM 兜底"
    assert "分处不同段落" in verdict.reason


def test_prefilter_skips_llm_when_paired_in_same_paragraph() -> None:
    """同段配对完整时仍应跳过 LLM（省成本）。"""
    verdict = classify(subject="會議通知", text="會議定於 2026年10月20日 15:00 舉行。")
    assert verdict.extract is True
    assert verdict.call_llm is False, "同段配对完整，规则够用，不必调 LLM"


def test_prefilter_recognizes_chinese_numeral_shapes() -> None:
    """预筛器的日期/时刻形态也要认中文数字。"""
    verdict = classify(subject="邀請", text="謹訂於二零二六年十月一日舉行。")
    assert verdict.extract is True


# ══════════════════════════════════════════════════════════════
# 缺陷 6：时刻槽的时区错误
# ══════════════════════════════════════════════════════════════


def test_slot_key_uses_local_minutes_not_utc_hours() -> None:
    """**最严重的那个 bug**：时刻槽曾按 UTC 小时分组。

    香港时间 10:00 与 10:30 换算成 UTC 是 02:00Z 与 02:30Z——同属 UTC 02 点。
    原实现把它们判为同一槽、只保留一个，于是 LLM 正确抽出的
    「升旗禮 10:30」被**静默丢弃**（被同日的「迎迓 10:00」抢走）。
    """
    from automail.extract.pipeline import Candidate, _slot_key
    from automail.models import EventSource

    def cand(ts: str, title: str) -> Candidate:
        return Candidate(
            title=title, start_ts=ts, end_ts=None, all_day=False,
            source=EventSource.LLM, confidence=0.9, fingerprint=title,
        )

    # 10:00 与 10:30 本地 → UTC 都是 02 点，但必须落在不同槽
    k1 = _slot_key(cand("2026-10-01T02:00:00Z", "迎迓"), TZ)
    k2 = _slot_key(cand("2026-10-01T02:30:00Z", "升旗禮"), TZ)
    assert k1 != k2, "同一 UTC 小时内的两个本地事件不得合并"
    assert "10:00" in k1 and "10:30" in k2


def test_slot_key_uses_local_date() -> None:
    """UTC 日期可能与本地的自然日差一天，必须用本地日期。

    香港 10-01 00:30 = 09-30 16:30Z：用 UTC 日期会把它归到 09-30。
    """
    from automail.extract.pipeline import Candidate, _slot_key
    from automail.models import EventSource

    c = Candidate(
        title="午夜活动", start_ts="2026-09-30T16:30:00Z", end_ts=None,
        all_day=False, source=EventSource.LLM, confidence=0.9, fingerprint="m",
    )
    assert _slot_key(c, TZ).startswith("2026-10-01"), "应使用本地日期"


def test_all_day_dropped_when_timed_same_day_exists() -> None:
    """同一天有定时候选时，全天候选应被取代（不是并存）。

    场景：规则从「二零二六年十月一日」抽出全天（无时刻），LLM 从同一封邮件
    抽出「10:30」。二者指同一件事，LLM 的更精确。都保留会让日历出现
    「全天事件 + 具体时刻事件」两条。
    """
    from automail.extract.pipeline import Candidate, _dedupe_by_slot
    from automail.models import EventSource

    all_day = Candidate(
        title="升旗禮", start_ts="2026-09-30T16:00:00Z", end_ts=None, all_day=True,
        source=EventSource.RULES, confidence=0.85, fingerprint="d1",
    )
    timed = Candidate(
        title="升旗禮", start_ts="2026-10-01T02:30:00Z", end_ts=None, all_day=False,
        source=EventSource.LLM, confidence=0.95, fingerprint="d2",
    )
    kept = _dedupe_by_slot([all_day, timed], TZ)
    assert len(kept) == 1, "同日不应同时保留全天与定时"
    assert kept[0].all_day is False, "应保留更精确的定时版本"
    assert "同日全天" in kept[0].evidence, "被取代者的信息应并入 evidence"


def test_both_timed_events_same_day_both_kept() -> None:
    """同一天的两个不同时刻事件都必须保留（这是实测漏掉 10:30 的直接原因）。"""
    from automail.extract.pipeline import Candidate, _dedupe_by_slot
    from automail.models import EventSource

    def cand(ts: str, title: str) -> Candidate:
        return Candidate(
            title=title, start_ts=ts, end_ts=None, all_day=False,
            source=EventSource.LLM, confidence=0.9, fingerprint=title,
        )

    kept = _dedupe_by_slot(
        [
            cand("2026-10-01T02:00:00Z", "迎迓"),
            cand("2026-10-01T02:30:00Z", "升旗禮"),
        ],
        TZ,
    )
    assert len(kept) == 2, "同日的两个不同时刻是两个事件，不得合并"


# ══════════════════════════════════════════════════════════════
# 附带修正：冲突与「同日兄弟」的区分
# ══════════════════════════════════════════════════════════════


def test_siblings_are_not_reported_as_conflicts(conn) -> None:
    """同一活动的前后环节（迎迓 10:00、升旗禮 10:30）不是互相矛盾。

    把它们标成「冲突」会让使用者以为必须二选一，反而误导。只有时刻分歧
    超过阈值（无法同时成立）才算真冲突。
    """
    from automail.db import utcnow_iso
    from automail.review import ReviewQueue

    cur = conn.execute(
        """INSERT INTO messages (account, folder, uid_validity, uid, subject,
            is_canonical, stale, fetched_at) VALUES ('163','INBOX',1,1,'邀請',1,0,?)""",
        (utcnow_iso(),),
    )
    mid = int(cur.lastrowid)

    def add(ts: str, title: str) -> int:
        c = conn.execute(
            """INSERT INTO events (message_id, title, start_ts, all_day, source,
                confidence, fingerprint, status, created_at, updated_at)
               VALUES (?, ?, ?, 0, 'llm', 0.9, ?, 'pending', ?, ?)""",
            (mid, title, ts, title, utcnow_iso(), utcnow_iso()),
        )
        return int(c.lastrowid)

    a = add("2026-10-01T02:00:00Z", "迎迓")     # 10:00 本地
    b = add("2026-10-01T02:30:00Z", "升旗禮")   # 10:30 本地（相差 30 分）
    c2 = add("2026-10-01T04:00:00Z", "茶敘")    # 12:00 本地（相差 2 小时）

    items = {i.event_id: i for i in ReviewQueue(conn).list_items(limit=20)}

    # 迎迓 10:00 与 升旗禮 10:30 相差 30 分钟 → 同一活动的前后环节，是兄弟
    assert items[a].sibling_ids == [b]
    assert b not in items[a].conflicts_with
    # 茶敘 12:00 与 10:00 相差 2 小时 → 超过阈值，无法同时成立 → 真冲突
    assert c2 in items[a].conflicts_with
    # 反向也要成立：兄弟关系是对称的，冲突关系也是
    assert a in items[b].sibling_ids
    assert c2 in items[b].conflicts_with
    # 谁都不该把自己算进任何一组
    for item in items.values():
        assert item.event_id not in item.conflicts_with
        assert item.event_id not in item.sibling_ids


def test_conflict_threshold_is_used() -> None:
    """阈值必须实际生效（否则「冲突」会退化成「同日即冲突」）。"""
    from automail.review import ReviewQueue

    assert ReviewQueue.CONFLICT_MINUTES == 60


# ══════════════════════════════════════════════════════════════
# 完整样本：三个未来事件都应被抽出（LLM 路径）
# ══════════════════════════════════════════════════════════════


def test_full_sample_ground_truth_via_llm() -> None:
    """用假 LLM 返回实测中真实拿到的结果，验证流水线不丢事件。

    LLM 对这封邮件的真实返回是三个事件（已在真机确认）。这里固化：
    **流水线必须把三个都保留下来**——这正是缺陷 6 修好后才成立的。
    """
    from automail.extract.llm import LlmEvent, LlmResult
    from automail.extract.pipeline import extract_from_raw
    from tests.corpus import build_mail

    class StubLLM:
        available = True
        stats = type("S", (), {"calls": 0, "failures": 0, "skipped_budget": 0, "input_chars": 0})()

        def extract(self, **kwargs: object) -> LlmResult:
            return LlmResult(
                events=[
                    LlmEvent(title="升旗禮", start="2026-10-01T10:30:00+08:00",
                             location="中央廣場升旗台", confidence=0.95,
                             evidence="升旗禮 上午十時三十分"),
                    LlmEvent(title="迎迓", start="2026-10-01T10:00:00+08:00",
                             location="校史館", confidence=0.9,
                             evidence="迎迓 上午十時正"),
                    LlmEvent(title="網上回條登記截止", start="2026-09-21",
                             all_day=True, confidence=0.9,
                             evidence="敬希於二零二六年九月二十一日或之前"),
                ],
                ok=True,
            )

    raw = build_mail(
        from_addr="s1234567@link.example.edu.hk",
        subject="转发: 示例大學 升旗禮",
        body=TABLE_LAYOUT,
        message_id="<fwd-1@x.com>",
    )
    outcome = extract_from_raw(
        raw, received_at=RECEIVED, user_timezone="Asia/Shanghai",
        llm=StubLLM(), now=datetime(2026, 9, 20, tzinfo=UTC),
    )

    slots = {(c.start_ts or "")[:16] for c in outcome.candidates}
    assert "2026-10-01T02:30" in slots, "升旗禮 10:30 必须保留（曾被 UTC 小时分组丢弃）"
    assert "2026-10-01T02:00" in slots, "迎迓 10:00 必须保留"
    # 登记截止是全天事件，落库为当地 09-21 零点的 UTC 表示。
    # 这里按**当地自然日**断言：直接比对 UTC 字符串正是缺陷 6 的成因。
    all_day_days = {
        _local_day(c, TZ) for c in outcome.candidates if c.all_day
    }
    assert "2026-09-21" in all_day_days, "登记截止（全天）必须保留"


# ══════════════════════════════════════════════════════════════
# 缺陷 7：日程短行被当营销页脚删掉
# ══════════════════════════════════════════════════════════════


def test_schedule_lines_survive_footer_stripping() -> None:
    """**实测 bug**：邀请函把日程排成短行表格，被当成「营销页脚」整段删掉。

    ``升旗禮`` 与 ``上午十時三十分 | 中央廣場升旗台`` 又短又无标点，
    与「公司名/地址/电话」形态完全一样。原先只看「短且无标点」，
    于是**唯一的时刻来源**被当噪音丢弃——这封真实邀请函因此丢了全部时刻。
    """
    cleaned = clean_body(TABLE_LAYOUT)
    assert "上午十時三十分" in cleaned, "含时刻的行是正文，不是页脚"
    assert "升旗禮" in cleaned


def test_footer_without_marketing_word_is_not_stripped() -> None:
    """没有营销词背书时，短行不做任何剥离（宁可漏删页脚，不可误删日程）。"""
    cleaned = clean_body("會議安排如下。\n\n請準時出席\n")
    assert "請準時出席" in cleaned


def test_real_marketing_footer_still_stripped() -> None:
    """真正的营销页脚（有退订/版权词）仍必须剥掉——修正不能反向放水。"""
    cleaned = clean_body(
        "會議定於 2026年9月20日 15:00 舉行。\n\n"
        "某某科技有限公司\n© 2026 版權所有\n退訂"
    )
    assert "2026年9月20日" in cleaned
    assert "退訂" not in cleaned
    assert "版權所有" not in cleaned


def test_marketing_footer_with_schedule_inside_is_kept_whole() -> None:
    """页脚区块里若混入日程行，整块不剥——保日程优先。

    这是刻意的保守取舍：漏删一段页脚只是噪音，删掉日程则是**信息丢失**。
    """
    cleaned = clean_body(
        "詳情如下。\n\n某某公司\n查詢熱線 2026年9月20日 15:00"
    )
    assert "2026年9月20日" in cleaned


# ══════════════════════════════════════════════════════════════
# 缺陷 8：标题派生的两处误剥
# ══════════════════════════════════════════════════════════════


def test_empty_brackets_removed_after_weekday_stripped() -> None:
    """星期几被当时间剥走后，不能留下孤零零的空括号。

    实测标题曾是「敬希 二零二六年九月二十一日 ( ) 或之前…」——
    ``星期一`` 被剥走，只剩一对括号，读起来像程序出错。

    日期本身也应被剥掉：它是**时间信息**，不是事件名字。标题留下
    「敬希 或之前，填妥網上回條以作登記」比留着日期更可读。
    """
    from automail.extract.rules import derive_title

    title = derive_title(
        "敬希於二零二六年九月二十一日 (星期一) 或之前，填妥網上回條以作登記。",
        subject="升旗禮", fallback="x",
    )
    assert "( )" not in title and "()" not in title
    assert "二零二六年九月二十一日" not in title, "日期属时间信息，应从标题剥掉"
    assert "回條" in title or "登記" in title, "事件名词必须保留"


def test_title_starting_with_meeting_is_not_truncated() -> None:
    """**实测 bug**：单字助词剥除把标题首字吃掉。

    ``_TITLE_DANGLING_HEAD_RE`` 里的裸 ``已|将|將|会|會|要`` 会命中任何以
    这些字开头的标题，于是「會議定於…」被剥成「議 舉行」。中文事件标题以
    「會議」开头的极多，这是大面积的可读性损坏。
    """
    from automail.extract.rules import derive_title

    title = derive_title(
        "會議定於 2026年10月20日 15:00 舉行。", subject="會議通知", fallback="x"
    )
    assert "會議" in title, f"首字不得被剥掉：{title!r}"

    # 但真正悬挂的助词仍应剥掉
    assert derive_title(
        "會在 2026年9月20日 举行会议。", subject="通知", fallback="x"
    ) == "举行会议"


# ══════════════════════════════════════════════════════════════
# 缺陷 9：英文月份名日期（复盘功能发现）
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("text", "iso", "has_year"),
    [
        ("Aug 28, 2026 12:00 PM Hong Kong SAR", "2026-08-28", True),
        ("August 28, 2026", "2026-08-28", True),
        ("28 Aug 2026", "2026-08-28", True),
        ("28th August, 2026", "2026-08-28", True),
        ("Sep 3, 2026 3:00 PM", "2026-09-03", True),
        ("December 31, 2026", "2026-12-31", True),
        ("Aug 28", "2026-08-28", False),  # 无年份 → 硬门待审
        ("28 Aug", "2026-08-28", False),
    ],
)
def test_english_month_name_dates(text: str, iso: str, has_year: bool) -> None:
    """英文月份名日期必须解析。

    **实测漏掉**：Zoom / Google / Outlook 的邀请函写作
    ``Date & Time`` / ``Aug 28, 2026 12:00 PM Hong Kong SAR``。
    只认中文与阿拉伯数字日期时，这类邀请函**整类漏抽**——而流程照常报成功。

    这是「抽取复盘」（``automail audit``）上线后第一个抓到的真实缺陷：
    复盘指出邮件 #27 预筛放行、零候选、零事件。
    """
    found = _all_dates_in(text, RECEIVED)
    assert found, f"{text!r} 应被识别"
    assert found[0][0].isoformat() == iso
    assert found[0][1] is has_year


def test_english_date_day_first_does_not_eat_year() -> None:
    """**回归**：``28 Aug 2026`` 不得被「月在前」模式读成 ``Aug 20``。

    月在前模式若不加数字边界，会从 ``Aug`` 处匹配并**把年份前两位当日**，
    产出一个凭空的错误日期（2026-08-20）。这类错误比漏抽更危险：
    它会静默写进日历。
    """
    found = _all_dates_in("28 Aug 2026", RECEIVED)
    assert found[0][0].isoformat() == "2026-08-28"


def test_english_date_end_to_end() -> None:
    """端到端：Zoom 风格的邀请函片段能抽出带时刻的候选。"""
    result = extract_by_rules(
        text="Date & Time\n\nAug 28, 2026 12:00 PM Hong Kong SAR",
        subject="Welcoming Day",
        received_at=RECEIVED,
    )
    assert result.hits, "英文日期 + 12 小时制时刻应能配对"
    hit = result.hits[0]
    assert hit.start.strftime("%Y-%m-%d %H:%M") == "2026-08-28 12:00"
    assert not hit.all_day


# ══════════════════════════════════════════════════════════════
# 缺陷 10：预筛宣称「可解析 ICS」而实际没有 ICS 部件
# ══════════════════════════════════════════════════════════════


def test_ics_flag_without_parts_does_not_suppress_llm() -> None:
    """**实测最坏组合**：``has_ics`` 标志为真、部件却为空 → 预筛宣称
    「含 ICS 部件，可直接解析」，于是**跳过 LLM**，但实际一个部件都解析不了，
    整封邮件静默产出零候选。

    这不是假想：库里只存清洗后的正文片段（隐私设计），**从不存 ICS 原文**，
    而 ``has_ics`` 标志会保留——因此**离线重跑时每封 ICS 邮件都是这个状态**。
    实测邮件 #27/#33 均如此，复盘功能因此报出「预筛认为规则足以覆盖，
    但规则未命中任何时间」。

    修正：``classify`` 收到的应是「真的有可解析部件」，而不是标志。
    """
    from automail.extract.pipeline import extract_from_parsed
    from automail.mail.mime import BodyResult, ParsedMail

    parsed = ParsedMail(
        subject="Invitation", from_addr="a@b.com", from_name="",
        to_addrs=[], message_id="<x@y>", in_reply_to=None, references=[],
        sent_at=None,
        has_ics=True,          # 标志说「有 ICS」
        ics_parts=[],          # 但实际没有（离线重跑的常态）
        has_unsubscribe=False, unsubscribe_mailto=None, unsubscribe_links=[],
        list_id=None, auto_submitted=None,
        body=BodyResult(
            text="Date & Time\n\nAug 28, 2026 12:00 PM Hong Kong SAR",
            full_text="", sha256="",
        ),
    )
    outcome = extract_from_parsed(parsed, received_at=RECEIVED, llm=None)

    assert outcome.candidates, "没有 ICS 部件时不能只靠标志就放弃抽取"
    assert outcome.verdict.extract is True
    # 预筛理由不得再宣称「含 ICS 可直接解析」——那是假的
    assert "ICS" not in outcome.verdict.reason
    assert outcome.ics_events == 0


def test_real_ics_parts_still_short_circuit_llm() -> None:
    """真正拿到 ICS 部件时仍应短路 LLM（省成本）——修正不能反向放水。"""
    from automail.extract.prefilter import classify

    verdict = classify(subject="邀请", text="", has_ics=True)
    assert verdict.extract is True
    assert verdict.call_llm is False
    assert "ICS" in verdict.reason


# ══════════════════════════════════════════════════════════════
# 缺陷 11：英文长句正文派生出无信息量的残句标题
# ══════════════════════════════════════════════════════════════


def test_long_prose_sentence_falls_back_to_subject_title() -> None:
    """**回归**：英文邀请函的正文是长句，派生标题只能是残句。

    支持英文日期后，这类邮件从「LLM 给出干净标题」退化成
    「规则给出 ``In addition you are invited to join our…``」——因为去重
    按来源优先级保留了规则结果。标题退化成残句比漏抽更隐蔽：时间是对的，
    只是看不懂这是什么事件。

    此时应改用**主题**（它才是这件事的名字），并剥掉机器标记。
    主题本身可能也超长而需要截断——**截断的主题仍然远比截断的正文可读**。
    """
    from automail.extract.rules import derive_title

    title = derive_title(
        "In addition you are invited to join our grand Demo Centre Inauguration "
        "and Orientation Ceremony on 3 September 2026.",
        subject="[Important Reminder] Demo Centre Condition Fulfilment and Orientation "
                "Ceremony Invitation",
        fallback="x",
    )
    assert title.startswith("Demo Centre"), f"应剥掉 [Important Reminder] 标记：{title!r}"
    assert "In addition" not in title, f"不该用正文残句当标题：{title!r}"


def test_subject_title_strips_embedded_date() -> None:
    """主题里夹带的日期要剥掉（``Visa Condition Survey`` 而非 ``[For Completion ]``）。"""
    from automail.extract.rules import derive_title

    title = derive_title(
        "As emphasised in our previous emails, please complete the survey "
        "by 1 September 2026 at the earliest convenience.",
        subject="[For Completion by 10 am, 24 August 2026] Visa Condition Survey",
        fallback="x",
    )
    assert title == "Visa Condition Survey", title
    assert "[" not in title and "]" not in title


def test_short_clean_sentence_still_preferred_over_subject() -> None:
    """短而干净的句子仍优先于主题——修正不能把正常行为也改掉。

    ``會議定於 … 舉行`` 这类派生结果本身就是好标题，主题反而可能更泛。
    """
    from automail.extract.rules import derive_title

    title = derive_title(
        "產品評審會議於 2026年9月20日 舉行。",
        subject="[通知] 本週安排",
        fallback="x",
    )
    assert "產品評審會議" in title, title


def test_convoluted_subject_does_not_produce_garbage() -> None:
    """主题本身没法派生出可读标题时，退回主题原文而不是抛异常/给空串。"""
    from automail.extract.rules import derive_title

    title = derive_title(
        "We are pleased to announce that registration is now open for the "
        "upcoming programme orientation session on 3 September 2026.",
        subject="",
        fallback="(无标题)",
    )
    assert title


# ══════════════════════════════════════════════════════════════
# 反向：不得因这些修正而过度抽取
# ══════════════════════════════════════════════════════════════


def test_marketing_mail_with_chinese_dates_still_filtered() -> None:
    """含中文数字日期的营销邮件仍应被预筛拦下（不能因支持中文数字而放行噪音）。"""
    verdict = classify(
        subject="下載App 即享優惠",
        text="優惠期至二零二六年十二月三十一日。版權所有 退訂",
    )
    assert verdict.extract is False


def test_plain_text_without_any_date_not_extracted() -> None:
    verdict = classify(subject="服務條款更新", text="我們更新了服務條款，請查閱。")
    assert verdict.extract is False


def test_time_similarity_helper_unaffected() -> None:
    """附带确认：标题相似度功能未被这些改动影响。"""
    assert title_similarity("升旗禮", "升旗禮") == 1.0
    _ = timedelta(days=1)  # 保持 timedelta 导入语义
