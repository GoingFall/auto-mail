"""ICS 直解：置信度最高、零 LLM 成本。

支持矩阵见 docs/spec-event-state-machine.md §7 / §9：

* 多 ``VEVENT`` —— 逐个产出候选
* ``METHOD:REQUEST`` —— 自动推送白名单（还需 organizer 非空 + 起止可解析）
* ``METHOD:CANCEL`` —— **强制人工**，绝不静默删除
* ``METHOD:REPLY`` —— 仅记录，不入历
* ``UID`` / ``SEQUENCE`` —— 去重与更新匹配键
* ``RECURRENCE-ID`` —— 同 UID 的实例例外，独立候选（**不撞母事件**）
* ``RRULE`` —— v1 **不展开**，按单次写入 + 标题标注「(重复)」，原 RRULE 存字段
* ``TZID`` / 浮动 ``DTSTART`` —— 按 TZID 解析；无 TZID 视为浮动，用用户时区
* 全天事件（``DATE`` 值）—— 置 ``all_day``

v1 明确不承诺：完整重复事件管理、RSVP 跟踪、与会者同步。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from ..models import EventSource
from .fingerprint import compute_ics_fingerprint

logger = logging.getLogger("automail.extract.ics")

#: v1 不展开重复规则，但仍要把规则原文保留下来
RRULE_NOTE = "(重复)"


@dataclass(slots=True)
class IcsEvent:
    """从 ICS 解出的一个事件。"""

    uid: str | None
    summary: str
    start_ts: str | None
    end_ts: str | None
    all_day: bool
    sequence: int | None
    recurrence_id: str | None
    rrule: str | None
    location: str | None
    organizer: str | None
    method: str | None
    status: str | None
    evidence: str
    fingerprint: str
    is_cancellation: bool
    needs_review: bool = False
    review_reason: str = ""

    @property
    def source(self) -> EventSource:
        return EventSource.ICS


def _iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_iso(value: object, tz: ZoneInfo) -> tuple[str | None, bool]:
    """把 icalendar 的日期/时间属性转成 ISO8601 UTC。

    返回 ``(iso, all_day)``。全天事件（``date`` 值）不加时刻。
    """
    if value is None:
        return None, False
    # icalendar 对 DATE 值给 date，对 DATE-TIME 给 datetime
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return _iso_utc(value), False
    if isinstance(value, date):
        # 全天：用当天 00:00 本地时间表示，"all_day" 标记由调用方保留
        naive = datetime.combine(value, time(0, 0))
        return _iso_utc(naive.replace(tzinfo=tz)), True
    return None, False


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _rrule_text(value: object) -> str | None:
    """把 ``RRULE`` 属性还原成 RFC 5545 原始文本。

    ``icalendar`` 会把 RRULE 解析成内部的 ``vRecur`` 对象，``str()`` 得到的是
    ``vRecur({'FREQ': ['WEEKLY'], 'COUNT': [10]})`` 这种 Python 表示，而不是
    ``FREQ=WEEKLY;COUNT=10``。规格要求「描述里保留原始 RRULE」，因此必须
    用 ``to_ical()`` 取回原始形式。
    """
    if value is None:
        return None
    to_ical = getattr(value, "to_ical", None)
    if callable(to_ical):
        try:
            raw = to_ical()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            text = str(raw).strip()
            if text:
                return text
        except Exception:  # noqa: BLE001 - 取原始形式失败时退回 str()
            pass
    text = str(value).strip()
    return text or None


def parse_ics(
    raw: bytes,
    *,
    user_timezone: str = "Asia/Shanghai",
    default_duration_minutes: int = 30,
) -> list[IcsEvent]:
    """解析一份 ICS 内容，返回其中所有事件。

    解析失败返回空列表并记日志——**不抛异常**，因为日历部件损坏不应让整封
    邮件的处理失败。
    """
    try:
        from icalendar import Calendar
    except ImportError:  # pragma: no cover - 依赖已声明
        return []

    tz = ZoneInfo(user_timezone)

    try:
        calendar = Calendar.from_ical(raw)
    except Exception as exc:  # noqa: BLE001 - 畸形 ICS 不该拖垮流程
        logger.warning("ICS 解析失败：%s", exc)
        return []

    method = _text(calendar.get("METHOD"))
    events: list[IcsEvent] = []

    for component in calendar.walk("VEVENT"):
        try:
            events.append(
                _build_event(
                    component, method=method, tz=tz,
                    default_duration_minutes=default_duration_minutes,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单个 VEVENT 失败不影响其它
            logger.warning("VEVENT 解析失败：%s", exc)
            continue

    return events


def _build_event(
    component: object,
    *,
    method: str | None,
    tz: ZoneInfo,
    default_duration_minutes: int,
) -> IcsEvent:
    get = component.get  # type: ignore[attr-defined]

    uid = _text(get("UID"))
    summary = _text(get("SUMMARY")) or "(无标题)"
    location = _text(get("LOCATION"))
    sequence_raw = get("SEQUENCE")
    sequence = int(sequence_raw) if sequence_raw is not None else None
    rrule = _rrule_text(get("RRULE"))
    status = _text(get("STATUS"))

    # RECURRENCE-ID 可能带 TZID／可能是 DATE 值；统一转成 ISO 字符串作为匹配键
    recurrence_raw = get("RECURRENCE-ID")
    recurrence_id: str | None = None
    if recurrence_raw is not None:
        rec_iso, _ = _to_iso(getattr(recurrence_raw, "dt", None), tz)
        recurrence_id = rec_iso or _text(recurrence_raw)

    # ORGANIZER 可能是 vCalAddress（带 CN 参数）
    organizer_raw = get("ORGANIZER")
    organizer = _text(organizer_raw)
    if organizer and ":" in organizer:
        # "mailto:x@y.com" → "x@y.com"
        organizer = organizer.split(":", 1)[1].strip() or organizer

    start_raw = getattr(get("DTSTART"), "dt", None)
    end_raw = getattr(get("DTEND"), "dt", None)
    start_ts, all_day = _to_iso(start_raw, tz)
    end_ts, _ = _to_iso(end_raw, tz)

    # 无 DTEND 时用默认时长兜底；全天事件不加时长
    needs_review = False
    review_reason = ""

    if start_ts and not end_ts and not all_day:
        from datetime import timedelta

        parsed = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        end_ts = _iso_utc(parsed + timedelta(minutes=default_duration_minutes))

    if not start_ts:
        needs_review = True
        review_reason = "ICS 缺少可解析的 DTSTART"

    display_summary = f"{summary} {RRULE_NOTE}" if rrule else summary

    # METHOD:CANCEL 或 STATUS:CANCELLED 都是取消语义 → 强制人工
    is_cancellation = (method or "").upper() == "CANCEL" or (
        status or ""
    ).upper() == "CANCELLED"
    # METHOD:REPLY 只是回应，不入历
    is_reply = (method or "").upper() == "REPLY"
    if is_reply:
        needs_review = True
        review_reason = "METHOD:REPLY 仅记录，不入历"

    fingerprint = compute_ics_fingerprint(
        ics_uid=uid, ics_recurrence_id=recurrence_id, organizer=organizer
    ) or ""

    evidence_parts = [f"ICS UID={uid}", f"SUMMARY={summary}"]
    if start_ts:
        evidence_parts.append(f"DTSTART={start_ts}")
    if rrule:
        evidence_parts.append(f"RRULE={rrule}(v1 不展开)")
    if is_cancellation:
        evidence_parts.append("METHOD/STATUS 表示取消")

    return IcsEvent(
        uid=uid,
        summary=display_summary,
        start_ts=start_ts,
        end_ts=end_ts,
        all_day=all_day,
        sequence=sequence,
        recurrence_id=recurrence_id,
        rrule=rrule,
        location=location,
        organizer=organizer,
        method=method,
        status=status,
        evidence=" | ".join(evidence_parts),
        fingerprint=fingerprint,
        is_cancellation=is_cancellation,
        needs_review=needs_review,
        review_reason=review_reason,
    )
