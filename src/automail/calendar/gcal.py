"""Google Calendar 后端：实现 :class:`~automail.calendar.backend.CalendarBackend`。

## 与 FakeCalendar 的关系

两者满足同一接口，因此 ``PushEngine`` 的写入路径、幂等反查、三方比对、
冻结逻辑在真实 Google 上**原样生效**——这些机制已在 P3 用 FakeCalendar
充分测试过（包括模拟服务端的字段重排、时区改写、空白折叠）。

## 本实现必须遵守的 Google API 事实（均已核对官方文档）

**① ``privateExtendedProperty`` 用于幂等反查**

```
events.list(calendarId, privateExtendedProperty="auto_mail_key=<fp>")
```

格式是 ``propertyName=value``，可重复传入（多个条件之间是 **AND**）。
但它**不能与 ``syncToken`` 同用**，因此本模块用一次性 list，不做增量同步。

**② Calendar API v3 未定义 ``If-Match`` / 条件请求**

官方 discovery 文档与 ``events.get``/``update``/``delete`` 参考页里都没有
``If-Match``，也没有 412 契约。因此**不能**依赖原子条件请求，只能走
「get → 比对 → update」的乐观流程，接受一个极小的竞态窗口。
三方比对（快照/远端/本地规范化哈希）就是这个流程的安全网。

**③ 删除是硬删除**

``events.delete`` 不可恢复。因此归档（置 ``status=cancelled``）走
``update_event``，而 ``PushEngine`` 只在显式 ``--hard-delete`` 时才调用
本模块的 ``delete_event``。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from .auth import AuthError, CredentialsMissingError, ReauthRequiredError
from .backend import (
    CalendarAuthError,
    CalendarConflictError,
    CalendarError,
    CalendarEvent,
    CalendarNotFoundError,
)

logger = logging.getLogger("automail.calendar.gcal")

#: 可重试的 HTTP 状态码：限流与瞬时服务端错误
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: 重试次数与基础退避（秒）
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 1.0

#: 单次 list 的页大小（Google 默认 250，上限 2500）
DEFAULT_PAGE_SIZE = 250


@dataclass(slots=True)
class GcalCallStats:
    """调用统计（用于观测配额消耗与重试）。"""

    calls: int = 0
    retries: int = 0
    not_found: int = 0


class GoogleCalendarBackend:
    """Google Calendar API v3 后端。

    ``service`` 可注入（测试用），不传则由 :func:`~automail.calendar.auth.load_credentials`
    构造真实客户端。
    """

    def __init__(
        self,
        settings,
        *,
        conn=None,
        service: Any | None = None,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._service = service
        self._calendar_id = settings.google_calendar_id or "primary"
        self._retries = max(0, retries)
        self._backoff = max(0.0, backoff)
        self.stats = GcalCallStats()

    # ── 客户端构造 ────────────────────────────────────────

    @property
    def service(self):
        """惰性构造并缓存 API 客户端。"""
        if self._service is None:
            from googleapiclient.discovery import build

            credentials = self._load_credentials()
            self._service = build(
                "calendar", "v3", credentials=credentials, cache_discovery=False
            )
        return self._service

    def _load_credentials(self):
        """加载凭据，把授权类异常统一映射到 ``CalendarAuthError``。"""
        from .auth import load_credentials

        try:
            return load_credentials(self._settings, conn=self._conn)
        except ReauthRequiredError as exc:
            # 需重新授权：不可重试，交给调用方提示用户
            raise CalendarAuthError(str(exc)) from exc
        except CredentialsMissingError as exc:
            raise CalendarAuthError(str(exc)) from exc
        except AuthError as exc:
            raise CalendarAuthError(str(exc)) from exc

    # ── 重试与错误映射 ────────────────────────────────────

    def _execute(self, request_factory, *, action: str, allow_not_found: bool = False):
        """执行 API 请求，带 429/5xx 指数退避重试。

        ``request_factory`` 是**可调用对象**（不是已构造的 request），
        每次尝试都重新构造。原因：某些传输层的 ``HttpRequest`` 在失败后
        内部状态已污染，复用同一个对象重试不会真正重发请求。

        重试是必要的：日历 API 有配额（限流），且偶发 5xx。
        但**不重试** 4xx（除 429 或 403 限流）——参数错了重试无意义。

        **错误识别用鸭子类型而非 isinstance(HttpError)**：google-api-python-client
        在不同版本、以及不同传输层（httplib2 / requests / 自定义 HttpMock）
        抛出的异常类型并不一致。若把识别绑死在某个具体类上，其他形态的 HTTP 错误
        会落到兜底分支，被笼统包装成「失败」，从而**丢掉状态码语义**——404 变成
        普通失败、401 不再提示重新授权、限流不再重试。
        """
        attempt = 0
        while True:
            try:
                self.stats.calls += 1
                return request_factory().execute()
            except (CalendarError, CalendarNotFoundError, CalendarConflictError,
                    CalendarAuthError):
                # 已映射过的异常直接冒泡
                raise
            except OSError as exc:
                # 网络层问题（DNS、连接重置、超时）
                if attempt < self._retries:
                    self._sleep(attempt, action, "network")
                    attempt += 1
                    continue
                raise CalendarError(f"{action} 网络失败：{_brief(exc)}") from exc
            except Exception as exc:  # noqa: BLE001 - google 库的异常种类多
                status = _status_of(exc)

                if status == 404:
                    self.stats.not_found += 1
                    if allow_not_found:
                        return None
                    raise CalendarNotFoundError(
                        f"{action}：事件不存在（404）"
                    ) from exc

                if status in {401, 403}:
                    # 403 可能是配额耗尽而非权限问题；用错误文本区分
                    if status == 403 and _looks_like_rate_limit(exc):
                        if attempt < self._retries:
                            self._sleep(attempt, action, status)
                            attempt += 1
                            continue
                    else:
                        raise CalendarAuthError(
                            f"{action}：授权被拒绝（{status}）——"
                            "可能需要重新运行 automail auth；"
                            f"原文：{_brief(exc)}"
                        ) from exc

                if status == 409:
                    raise CalendarConflictError(f"{action}：服务端报告冲突（409）") from exc

                if status in RETRYABLE_STATUS:
                    if attempt < self._retries:
                        self._sleep(attempt, action, status)
                        attempt += 1
                        continue
                    raise CalendarError(
                        f"{action} 失败（HTTP {status}，已重试 {attempt} 次）：{_brief(exc)}"
                    ) from exc

                if status:
                    raise CalendarError(f"{action} 失败（HTTP {status}）：{_brief(exc)}") from exc

                raise CalendarError(f"{action} 失败：{_brief(exc)}") from exc

    def _sleep(self, attempt: int, action: str, status: object) -> None:
        """指数退避 + 抖动。

        抖动是必要的：计划任务可能与其他实例同时被限流，
        无抖动会导致它们同步重试、再次撞上限流。
        """
        delay = self._backoff * (2**attempt) + random.uniform(0, 0.5)
        self.stats.retries += 1
        logger.warning(
            "%s 遇到 %s，%.1fs 后重试（第 %s/%s 次）",
            action, status, delay, attempt + 1, self._retries,
        )
        time.sleep(delay)

    # ── CalendarBackend 实现 ──────────────────────────────

    def get_event(self, event_id: str) -> CalendarEvent:
        self._require_event_id(event_id)
        payload = self._execute(
            lambda: self.service.events().get(
                calendarId=self._calendar_id, eventId=event_id
            ),
            action=f"读取事件 {event_id}",
        )
        return _to_event(payload)

    def insert_event(self, payload: dict[str, Any]) -> CalendarEvent:
        from .normalize import extract_auto_mail_key

        key = extract_auto_mail_key(payload)
        if not key:
            # 所有权标记是唯一凭据，缺了就无法区分「我们创建的」与「用户的」
            raise ValueError("insert_event 需要 payload 带 auto_mail_key 标记")

        created = self._execute(
            lambda: self.service.events().insert(
                calendarId=self._calendar_id, body=payload
            ),
            action="创建事件",
        )
        return _to_event(created)

    def update_event(self, event_id: str, payload: dict[str, Any]) -> CalendarEvent:
        self._require_event_id(event_id)
        if not payload.get("extendedProperties"):
            raise ValueError("update_event 的 payload 必须保留 auto_mail_key 标记")

        # 用 update（全量）而非 patch：我们本来就有完整内容，
        # 且 patch 的语义在 Calendar API 里有歧义。
        updated = self._execute(
            lambda: self.service.events().update(
                calendarId=self._calendar_id, eventId=event_id, body=payload
            ),
            action=f"更新事件 {event_id}",
        )
        return _to_event(updated)

    def delete_event(self, event_id: str) -> None:
        """**硬删除**（不可恢复）。

        归档（可恢复）应通过 ``update_event`` 把 ``status`` 置为 ``cancelled``，
        ``PushEngine.archive`` 就是这么做的。本方法只在显式要求硬删除时调用。
        """
        self._require_event_id(event_id)
        # 目标已不存在时视为删除成功（幂等），而不是报错
        self._execute(
            lambda: self.service.events().delete(
                calendarId=self._calendar_id, eventId=event_id
            ),
            action=f"删除事件 {event_id}",
            allow_not_found=True,
        )

    def find_by_auto_mail_key(self, key: str) -> list[CalendarEvent]:
        """按 ``auto_mail_key`` 反查事件——**幂等的关键**。

        用途：``insert`` 之前先查一次。命中说明上次「创建成功但写库失败」
        （进程崩溃），此时应回填 ``gcal_event_id`` 而非重复创建。

        注意 ``privateExtendedProperty`` **不能与 syncToken 同用**，
        因此这里用一次性查询。
        """
        if not key:
            return []

        found: list[CalendarEvent] = []
        page_token: str | None = None

        while True:
            response = self._execute(
                lambda token=page_token: self.service.events().list(
                    calendarId=self._calendar_id,
                    privateExtendedProperty=f"auto_mail_key={key}",
                    maxResults=DEFAULT_PAGE_SIZE,
                    pageToken=token,
                    # 已取消的事件也要能查到——否则「归档过」的事件会被当成不存在，
                    # 导致重建时重复创建。
                    showDeleted=True,
                    singleEvents=True,
                ),
                action="按 auto_mail_key 反查",
            ) or {}

            for item in response.get("items", []) or []:
                found.append(_to_event(item))

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return found

    def list_events(self, *, limit: int = DEFAULT_PAGE_SIZE) -> list[CalendarEvent]:
        """列出事件（供摘要回读）。

        按开始时间升序、只取单次事件（不展开重复事件的实例化），
        并跳过已取消的——摘要里不该出现被取消的安排。
        """
        events: list[CalendarEvent] = []
        page_token: str | None = None
        page_size = min(max(1, limit), 250)

        while len(events) < limit:
            response = self._execute(
                lambda token=page_token: self.service.events().list(
                    calendarId=self._calendar_id,
                    maxResults=page_size,
                    pageToken=token,
                    singleEvents=True,
                    orderBy="startTime",
                    showDeleted=False,
                ),
                action="列出事件",
            ) or {}

            for item in response.get("items", []) or []:
                if item.get("status") == "cancelled":
                    continue
                events.append(_to_event(item))
                if len(events) >= limit:
                    break

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return events

    # ── 辅助 ──────────────────────────────────────────────

    @staticmethod
    def _require_event_id(event_id: str) -> None:
        if not event_id:
            raise ValueError("event_id 不可为空")


def _to_event(payload: dict[str, Any]) -> CalendarEvent:
    """把 API 响应转成 :class:`CalendarEvent`。

    ``etag`` 单独取出便于比较，但**也保留在 payload 里**——规范化时会把它
    列进黑名单，因此不影响内容哈希。
    """
    return CalendarEvent(
        event_id=str(payload.get("id") or ""),
        payload=dict(payload),
        etag=payload.get("etag"),
    )


def _status_of(exc: Exception) -> int:
    """从 HttpError 里取 HTTP 状态码。

    不同版本的 google-api-python-client 暴露形态不同：``resp.status``、
    ``resp.status_code``、或只在 ``error_details`` 里。逐个尝试。
    """
    response = getattr(exc, "resp", None)
    if response is not None:
        for attr in ("status", "status_code"):
            value = getattr(response, attr, None)
            if isinstance(value, int):
                return value
        # 某些版本把状态放在 reason 字符串的开头，如 "404 Not Found"
        reason = getattr(response, "reason", None)
        if isinstance(reason, str) and reason[:3].isdigit():
            return int(reason[:3])

    text = str(exc)
    prefix = text[:3]
    if prefix.isdigit():
        return int(prefix)
    return 0


def _looks_like_rate_limit(exc: Exception) -> bool:
    """403 既可能是权限问题也可能是配额耗尽，用文本区分。"""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("ratelimit", "rate limit", "quota", "userrateLimitExceeded".lower())
    )


def _brief(exc: Exception) -> str:
    from ..sanitize import sanitize_error

    return sanitize_error(exc)
