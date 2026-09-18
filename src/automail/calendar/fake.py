"""内存日历后端（测试用）。

**刻意模拟真实服务端的"污染"行为**，否则测试会给出虚假的通过：

1. 注入 ``etag``/``id``/``htmlLink``/``created``/``updated`` 等元数据
2. **重排字段顺序**（真实服务端不保证顺序）
3. 把 ``dateTime`` 的 ``Z`` 改写成显式时区偏移（真实 Google 会做等价改写）
4. 规整文本空白（描述里的多余空格会被服务端压缩）

若规范化哈希实现有误，这些改写会让每次读取都「看起来有变化」——
测试会立刻失败，而不是等到真实环境才发现机制失效。

还提供 :meth:`simulate_user_edit` 来模拟用户在日历客户端手工改事件，
这是验证「冻结更新/删除」的唯一办法。
"""

from __future__ import annotations

import itertools
from typing import Any

from .backend import (
    CalendarEvent,
    CalendarNotFoundError,
    build_event_payload,
)

#: 模拟服务端注入的元数据字段
_SERVER_INJECTED = {
    "kind": "calendar#event",
    "htmlLink": "https://calendar.example.com/event?eid=xxx",
    "created": "2026-09-14T10:00:00.000Z",
    "updated": "2026-09-14T10:00:00.000Z",
    "creator": {"email": "me@example.com", "self": True},
    "organizer": {"email": "me@example.com", "self": True},
    "reminders": {"useDefault": True},
}


class FakeCalendar:
    """内存日历，行为贴近真实服务端。"""

    def __init__(self) -> None:
        self._events: dict[str, dict[str, Any]] = {}
        self._etags: dict[str, str] = {}
        self._counter = itertools.count(1)
        self._etag_counter = itertools.count(1)

        #: 观测点：记录调用过的方法，测试据此断言「只动了我们自己的事件」
        self.calls: list[tuple[str, str]] = []
        #: 模拟故障注入：设为 True 时 insert 抛错
        self.fail_insert = False
        self.fail_get = False

    # ── 观测 ──────────────────────────────────────────────

    @property
    def events(self) -> dict[str, dict[str, Any]]:
        return self._events

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """当前全部事件的深拷贝（用于比较副作用）。"""
        import copy

        return copy.deepcopy(self._events)

    # ── 模拟真实服务端行为 ─────────────────────────────────

    def _server_roundtrip(self, payload: dict[str, Any], event_id: str) -> dict[str, Any]:
        """模拟服务端对 payload 的处理，返回它"存下来"的样子。

        这里做的三件事正是会让朴素哈希失效的噪声源：
        字段重排、注入元数据、时间与空白的等价改写。
        """
        import copy

        stored: dict[str, Any] = {}
        # 1) 重排顺序：把 summary/start/end 放到最后，模拟字段序不保证
        ordered_keys = sorted(payload.keys(), reverse=True)
        for key in ordered_keys:
            stored[key] = copy.deepcopy(payload[key])

        # 2) 注入服务器元数据
        for key, value in _SERVER_INJECTED.items():
            stored[key] = copy.deepcopy(value)
        stored["id"] = event_id

        # 3) 时间表示改写：把 Z 后缀改成 +00:00（等价但字面不同）
        for field in ("start", "end"):
            value = stored.get(field)
            if isinstance(value, dict) and isinstance(value.get("dateTime"), str):
                dt = value["dateTime"]
                if dt.endswith("Z"):
                    value["dateTime"] = dt[:-1] + "+00:00"

        # 4) 文本规整：折叠描述里的多余空白
        if isinstance(stored.get("description"), str):
            stored["description"] = " ".join(stored["description"].split())

        stored["etag"] = self._next_etag()
        return stored

    def _next_etag(self) -> str:
        return f'"etag-{next(self._etag_counter)}"'

    def _stored_to_event(self, event_id: str) -> CalendarEvent:
        payload = self._events[event_id]
        return CalendarEvent(
            event_id=event_id, payload=dict(payload), etag=payload.get("etag")
        )

    def _bump(self, event_id: str) -> None:
        """服务端在内容变化时更新 etag。"""
        self._events[event_id]["etag"] = self._next_etag()

    # ── 手工改动模拟 ──────────────────────────────────────

    def simulate_user_edit(self, event_id: str, **changes: Any) -> None:
        """模拟用户在日历客户端手工修改事件。

        这是验证「冻结更新与删除」的唯一手段：手改之后，我方应当检测到
        ``externally_modified`` 而非静默覆盖用户的修改。
        """
        if event_id not in self._events:
            raise CalendarNotFoundError(event_id)
        self._events[event_id].update(changes)
        self._bump(event_id)

    def simulate_server_touch(self, event_id: str) -> None:
        """模拟服务端自身更新（etag 变了但**内容语义未变**）。

        三方比对必须能区分这种情况（应判为「无害演进，可安全更新」）
        与真实用户编辑（应冻结）。只改 etag 就是最小复现。
        """
        if event_id not in self._events:
            raise CalendarNotFoundError(event_id)
        self._bump(event_id)

    def simulate_server_reserialize(self, event_id: str) -> None:
        """模拟服务端重新序列化（字段重排、元数据刷新，语义完全不变）。

        这会让**朴素哈希**失效：字面内容变了，规范化后应完全一致。
        """
        if event_id not in self._events:
            raise CalendarNotFoundError(event_id)
        payload = self._events[event_id]
        reserialized = self._server_roundtrip(
            {k: v for k, v in payload.items() if k not in _SERVER_INJECTED},
            event_id,
        )
        reserialized["etag"] = self._next_etag()
        self._events[event_id] = reserialized

    def remove_externally(self, event_id: str) -> None:
        """模拟事件在外部被删除（我方随后的 get 会得到 404）。"""
        self._events.pop(event_id, None)

    # ── CalendarBackend 实现 ──────────────────────────────

    def get_event(self, event_id: str) -> CalendarEvent:
        self.calls.append(("get_event", event_id))
        if self.fail_get:
            from .backend import CalendarError

            raise CalendarError("simulated get failure")
        if event_id not in self._events:
            raise CalendarNotFoundError(event_id)
        return self._stored_to_event(event_id)

    def insert_event(self, payload: dict[str, Any]) -> CalendarEvent:
        self.calls.append(("insert_event", str(payload.get("summary"))))
        if self.fail_insert:
            from .backend import CalendarError

            raise CalendarError("simulated insert failure")

        from .normalize import extract_auto_mail_key

        key = extract_auto_mail_key(payload)
        if not key:
            raise ValueError("缺少 auto_mail_key：拒绝创建无所有权标记的事件")

        event_id = f"evt-{next(self._counter)}"
        self._events[event_id] = self._server_roundtrip(payload, event_id)
        return self._stored_to_event(event_id)

    def update_event(self, event_id: str, payload: dict[str, Any]) -> CalendarEvent:
        self.calls.append(("update_event", event_id))
        if event_id not in self._events:
            raise CalendarNotFoundError(event_id)
        if not payload.get("extendedProperties"):
            raise ValueError("更新 payload 必须保留 auto_mail_key 标记")

        self._events[event_id] = self._server_roundtrip(payload, event_id)
        return self._stored_to_event(event_id)

    def delete_event(self, event_id: str) -> None:
        self.calls.append(("delete_event", event_id))
        if event_id not in self._events:
            raise CalendarNotFoundError(event_id)
        del self._events[event_id]

    def find_by_auto_mail_key(self, key: str) -> list[CalendarEvent]:
        self.calls.append(("find_by_auto_mail_key", key))
        from .normalize import extract_auto_mail_key

        found: list[CalendarEvent] = []
        for event_id, payload in self._events.items():
            if extract_auto_mail_key(payload) == key:
                found.append(self._stored_to_event(event_id))
        return found

    def list_events(self, *, limit: int = 250) -> list[CalendarEvent]:
        self.calls.append(("list_events", ""))
        return [self._stored_to_event(eid) for eid in list(self._events)[:limit]]


def make_payload(**kwargs: Any) -> dict[str, Any]:
    """便捷构造 payload（测试常用）。"""
    return build_event_payload(**kwargs)
