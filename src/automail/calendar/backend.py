"""日历后端接口与数据载体。

接口刻意保持窄，只暴露受控写入需要的能力：

* ``get_event`` / ``insert_event`` / ``update_event`` / ``delete_event``
* ``find_by_auto_mail_key`` —— **幂等反查**：create 之前先查是否已存在
  （覆盖「创建成功但写库前崩溃」的场景，规格 §8）
* ``list_events`` —— 回读（摘要需要）

**没有**批量删除、没有「按时间范围清空」这类危险接口。v1 只管理带
``auto_mail_key`` 标记的事件，绝不触碰无标记的事件（规格 §6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo


class CalendarError(Exception):
    """日历后端的基础异常。"""


class CalendarAuthError(CalendarError):
    """认证/授权失败。``invalid_grant`` 属此类，需重新授权而非重试。"""


class CalendarNotFoundError(CalendarError):
    """事件不存在（404）。去向由 ``NOT_FOUND_POLICY`` 决定。"""


class CalendarConflictError(CalendarError):
    """并发写冲突（服务端拒绝）。"""


@dataclass(slots=True)
class CalendarEvent:
    """日历侧的事件。

    ``payload`` 是原始字段字典（含 ``etag`` 等服务器注入字段），
    规范化哈希由 :mod:`automail.calendar.normalize` 计算。
    """

    event_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    etag: str | None = None

    @property
    def auto_mail_key(self) -> str | None:
        from .normalize import extract_auto_mail_key

        return extract_auto_mail_key(self.payload)


@dataclass(slots=True)
class InsertResult:
    """``insert_event`` 的结果。"""

    event: CalendarEvent
    created: bool
    """True 表示新建；False 表示命中幂等反查、回填了已存在的事件。"""


@runtime_checkable
class CalendarBackend(Protocol):
    """受控写入所需的最小接口。"""

    def get_event(self, event_id: str) -> CalendarEvent:
        """读取单个事件。

        Raises:
            CalendarNotFoundError: 事件不存在。
        """

    def insert_event(self, payload: dict[str, Any]) -> CalendarEvent:
        """创建事件。``payload`` 必须包含 ``auto_mail_key`` 标记。"""

    def update_event(self, event_id: str, payload: dict[str, Any]) -> CalendarEvent:
        """整体更新事件（不使用 patch 语义）。"""

    def delete_event(self, event_id: str) -> None:
        """硬删除事件。调用方需先确认所有权。"""

    def find_by_auto_mail_key(self, key: str) -> list[CalendarEvent]:
        """按 ``auto_mail_key`` 反查事件。

        这是**幂等**的关键：``insert`` 之前先查，命中就回填而不重复创建。
        对应 Google 侧即 ``privateExtendedProperty=auto_mail_key=<key>``
        （该参数存在但不能与 ``syncToken`` 同用，故用一次性查询）。
        """

    def list_events(self, *, limit: int = 250) -> list[CalendarEvent]:
        """列出事件（用于摘要回读）。"""


def _local_date(ts: str, timezone: str) -> date:
    """把一个 UTC 时间戳换算成**当地**自然日。

    全天事件必须用当地日期。直接用 ``ts[:10]`` 取的是 UTC 自然日，
    与当地日期可能差一天：当地 2026-10-01 零点存成 ``2026-09-30T16:00Z``，
    于是「10-01 的截止日」会出现在日历上的 09-30——**早一天**。
    """
    parsed = _parse_ts(ts)
    if parsed is None:
        return date(1970, 1, 1)
    try:
        tz = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - 时区名非法时退回 UTC，而不是崩溃
        tz = UTC  # type: ignore[assignment]
    return parsed.astimezone(tz).date()


def _parse_ts(ts: str) -> datetime | None:
    """解析 ISO8601（容忍 ``Z`` 与带偏移两种写法）。"""
    text = (ts or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def build_event_payload(
    *,
    title: str,
    start_ts: str | None,
    end_ts: str | None,
    all_day: bool,
    auto_mail_key: str,
    location: str | None = None,
    description: str | None = None,
    timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """构造日历事件的 payload。

    ``auto_mail_key`` 是必需的——它是我方所有权的唯一凭据，
    缺了它就无法区分「我们创建的事件」与「用户自己创建的事件」。

    **全天事件的时间语义**（实测校准，见 docs/）：
    ``start.date`` / ``end.date`` 都取**当地**自然日，且 ``end.date`` 是
    **开区间**（Google 只显示 ``[start, end)``）。因此:
    「10-01 的截止日」写成 ``start=10-01, end=10-02``，而不是 ``end=10-01``
    （后者会被存成零长度）。
    """
    if not auto_mail_key:
        raise ValueError("auto_mail_key 不可为空：它是我方事件所有权的唯一凭据")

    payload: dict[str, Any] = {
        "summary": title,
        "extendedProperties": {"private": {"auto_mail_key": auto_mail_key}},
    }

    if all_day:
        start_day = _local_date(start_ts, timezone) if start_ts else date(1970, 1, 1)
        end_day = _local_date(end_ts, timezone) if end_ts else start_day + timedelta(days=1)
        # 开区间：end 必须严格晚于 start，否则日历里是零长度事件
        if end_day <= start_day:
            end_day = start_day + timedelta(days=1)
        payload["start"] = {"date": start_day.isoformat()}
        payload["end"] = {"date": end_day.isoformat()}
    else:
        payload["start"] = _time_field(start_ts, all_day=False, timezone=timezone)
        payload["end"] = _time_field(end_ts or start_ts, all_day=False, timezone=timezone)

    if location:
        payload["location"] = location
    if description:
        payload["description"] = description
    return payload


def _time_field(
    ts: str | None, *, all_day: bool, timezone: str
) -> dict[str, Any]:
    """把一个时间戳转成定时事件的时间字段（``dateTime`` + ``timeZone``）。

    ``all_day`` 分支保留在此是为了兼容旧调用点：全天事件的日期换算
    需要 start/end 联动（开区间），已移到 :func:`build_event_payload`。
    """
    if not ts:
        return {"date": "1970-01-01"} if all_day else {
            "dateTime": "1970-01-01T00:00:00Z",
            "timeZone": timezone,
        }

    if all_day:
        return {"date": _local_date(ts, timezone).isoformat()}

    return {"dateTime": ts, "timeZone": timezone}
