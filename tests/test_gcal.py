"""P4 测试：OAuth 授权与 Google Calendar 后端。

**全部离线**：OAuth 流程与 API 调用都注入假实现，不触碰真实 Google。

覆盖的关键事实（均已核对官方文档）：

* ``privateExtendedProperty`` 用于幂等反查，**不能与 syncToken 同用**
* Calendar API v3 **未定义 If-Match**，只能走「get → 比对 → update」乐观流程
* ``invalid_grant`` 需重新授权，**不可重试**
* 归档（可恢复）与硬删除是两条不同路径
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from automail.calendar import auth as auth_module
from automail.calendar.auth import (
    CredentialsMissingError,
    ReauthRequiredError,
    inspect_status,
    revoke_local_token,
    validate_credentials_file,
)
from automail.calendar.backend import (
    CalendarAuthError,
    CalendarError,
    CalendarNotFoundError,
    build_event_payload,
)
from automail.calendar.gcal import (
    GoogleCalendarBackend,
    _looks_like_rate_limit,
    _status_of,
    _to_event,
)
from automail.db import MetaRepository, iso, utcnow
from automail.settings import Settings

# ──────────────────────────────────────────────────────────────
# 测试替身
# ──────────────────────────────────────────────────────────────


class FakeHttpError(Exception):
    """模拟 googleapiclient 的 HttpError。"""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.resp = type("R", (), {"status": status, "reason": f"{status} Error"})()


class FakeRequest:
    """模拟 googleapiclient 的 HttpRequest（有 ``.execute()``）。"""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.executed = 0

    def execute(self):
        self.executed += 1
        if self._error is not None:
            raise self._error
        return self._result


class FakeEventsResource:
    """模拟 ``service.events()``。"""

    def __init__(self, parent: FakeService) -> None:
        self._parent = parent

    # 每个方法都记录调用参数，供断言
    def get(self, **kwargs) -> FakeRequest:
        self._parent.calls.append(("get", kwargs))
        return self._parent.pop("get")

    def insert(self, **kwargs) -> FakeRequest:
        self._parent.calls.append(("insert", kwargs))
        return self._parent.pop("insert")

    def update(self, **kwargs) -> FakeRequest:
        self._parent.calls.append(("update", kwargs))
        return self._parent.pop("update")

    def delete(self, **kwargs) -> FakeRequest:
        self._parent.calls.append(("delete", kwargs))
        return self._parent.pop("delete")

    def list(self, **kwargs) -> FakeRequest:  # noqa: A003 - 对齐 API 命名
        self._parent.calls.append(("list", kwargs))
        return self._parent.pop("list")


class FakeService:
    """模拟 Calendar API 客户端。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._queue: list[FakeRequest] = []

    def queue(self, result=None, error: Exception | None = None) -> None:
        self._queue.append(FakeRequest(result=result, error=error))

    def pop(self, _name: str) -> FakeRequest:
        if not self._queue:
            raise AssertionError("FakeService 没有排队的响应")
        return self._queue.pop(0)

    def events(self) -> FakeEventsResource:
        return FakeEventsResource(self)


def _event_payload(event_id: str = "evt-1", **overrides) -> dict:
    payload = {
        "id": event_id,
        "etag": '"etag-1"',
        "status": "confirmed",
        "summary": "会议",
        "start": {"dateTime": "2026-10-01T10:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": "2026-10-01T11:00:00+08:00", "timeZone": "Asia/Shanghai"},
        "extendedProperties": {"private": {"auto_mail_key": "fp-1"}},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    creds = tmp_path / "credentials.json"
    creds.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test-client-id.apps.googleusercontent.com",
                    "client_secret": "test-secret",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "logs",
        google_credentials_file=creds,
        google_token_file=tmp_path / "token.json",
        calendar_backend="google",
    )


@pytest.fixture
def backend(settings: Settings, conn: sqlite3.Connection) -> GoogleCalendarBackend:
    return GoogleCalendarBackend(settings, conn=conn, service=FakeService(), retries=0)


# ──────────────────────────────────────────────────────────────
# credentials.json 校验
# ──────────────────────────────────────────────────────────────


def test_validate_credentials_accepts_desktop_app(settings: Settings) -> None:
    payload = validate_credentials_file(Path(settings.google_credentials_file))
    assert "installed" in payload


def test_validate_credentials_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CredentialsMissingError) as exc:
        validate_credentials_file(tmp_path / "nope.json")
    # 错误信息必须给出可执行的修复步骤，而不是只说"找不到"
    assert "oauth" in str(exc.value).lower() or "OAuth" in str(exc.value)


def test_validate_credentials_rejects_web_client(tmp_path: Path) -> None:
    """**常见错误**：把客户端建成了「Web 应用」类型。

    Web 类型需要手工填回调地址，只有桌面应用才能走 run_local_server 的
    自动回调。必须明确告诉使用者换类型，而不是让授权莫名失败。
    """
    path = tmp_path / "web.json"
    path.write_text(
        json.dumps({"web": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8"
    )
    with pytest.raises(CredentialsMissingError) as exc:
        validate_credentials_file(path)
    assert "桌面应用" in str(exc.value)
    assert "web" in str(exc.value)


def test_validate_credentials_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CredentialsMissingError):
        validate_credentials_file(path)


def test_validate_credentials_requires_client_fields(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps({"installed": {"client_id": "x"}}), encoding="utf-8")
    with pytest.raises(CredentialsMissingError) as exc:
        validate_credentials_file(path)
    assert "client_secret" in str(exc.value)


# ──────────────────────────────────────────────────────────────
# 授权状态检查
# ──────────────────────────────────────────────────────────────


def test_inspect_status_before_auth(settings: Settings, conn: sqlite3.Connection) -> None:
    info = inspect_status(settings, conn)
    assert info.credentials_present is True
    assert info.token_present is False
    assert info.ready is False
    assert "auth" in info.detail


def test_inspect_status_after_auth(settings: Settings, conn: sqlite3.Connection) -> None:
    Path(settings.google_token_file).write_text(
        json.dumps(
            {
                "client_id": "x",
                "client_secret": "y",
                "refresh_token": "r",
                "token": "t",
            }
        ),
        encoding="utf-8",
    )
    info = inspect_status(settings, conn)
    assert info.ready is True
    assert info.detail == "已授权"


def test_inspect_status_flags_reauth(settings: Settings, conn: sqlite3.Connection) -> None:
    Path(settings.google_token_file).write_text(
        json.dumps({"client_id": "x", "client_secret": "y", "refresh_token": "r"}),
        encoding="utf-8",
    )
    MetaRepository(conn).set(MetaRepository.NEEDS_REAUTH, "1")

    info = inspect_status(settings, conn)
    assert info.needs_reauth is True
    assert info.ready is False, "标记了需重新授权就不算就绪"


def test_inspect_status_detects_truncated_token(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """token.json 损坏（缺 refresh_token）应算作未就绪，而不是「已授权」。"""
    Path(settings.google_token_file).write_text(
        json.dumps({"client_id": "x", "client_secret": "y"}), encoding="utf-8"
    )
    info = inspect_status(settings, conn)
    assert info.ready is False
    assert "refresh_token" in info.detail


def test_revoke_removes_token(settings: Settings, conn: sqlite3.Connection) -> None:
    token = Path(settings.google_token_file)
    token.write_text("{}", encoding="utf-8")
    MetaRepository(conn).set(MetaRepository.NEEDS_REAUTH, "1")

    assert revoke_local_token(settings, conn=conn) is True
    assert not token.exists()
    assert not MetaRepository(conn).get_bool(MetaRepository.NEEDS_REAUTH)


def test_revoke_when_no_token_is_noop(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    assert revoke_local_token(settings, conn=conn) is False


# ──────────────────────────────────────────────────────────────
# 凭据加载与 invalid_grant
# ──────────────────────────────────────────────────────────────


def test_load_credentials_requires_token_file(settings: Settings) -> None:
    with pytest.raises(CredentialsMissingError) as exc:
        auth_module.load_credentials(settings)
    assert "auth" in str(exc.value)


def test_load_credentials_maps_invalid_grant_to_reauth(
    settings: Settings, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``invalid_grant`` 必须映射为「需重新授权」并**标记**，且不可重试。

    它是 refresh token 失效的信号（撤销授权、改密码、Testing 状态 7 天过期）。
    当成可重试错误会浪费调用并掩盖真实原因。
    """
    Path(settings.google_token_file).write_text(
        json.dumps(
            {
                "client_id": "x",
                "client_secret": "y",
                "refresh_token": "expired",
                "token": None,
                "expiry": "2020-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    from google.auth.exceptions import RefreshError

    def failing_refresh(self, request):  # noqa: ANN001
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    from google.oauth2.credentials import Credentials

    monkeypatch.setattr(Credentials, "refresh", failing_refresh)

    with pytest.raises(ReauthRequiredError) as exc:
        auth_module.load_credentials(settings, conn=conn)

    assert "auth" in str(exc.value)
    # 必须留下标记，供 doctor / 摘要提示
    assert MetaRepository(conn).get_bool(MetaRepository.NEEDS_REAUTH) is True


def test_is_invalid_grant_detects_both_shapes() -> None:
    class WithErrorAttr(Exception):
        error = "invalid_grant"

    assert auth_module._is_invalid_grant(WithErrorAttr("x")) is True
    assert auth_module._is_invalid_grant(Exception("invalid_grant: revoked")) is True
    assert auth_module._is_invalid_grant(Exception("server_error")) is False


def test_setup_steps_are_actionable() -> None:
    steps = auth_module.describe_setup_steps()
    assert "Calendar API" in steps
    assert "桌面应用" in steps
    assert "credentials.json" in steps


def test_scopes_are_minimal() -> None:
    """必须只用 calendar.events（最小权限），不用完整的 calendar scope。

    完整 scope 含共享日历与永久删除权限，本程序不需要；最小权限意味着
    万一令牌泄露，损害面更小。
    """
    assert auth_module.SCOPES == ("https://www.googleapis.com/auth/calendar.events",)


# ──────────────────────────────────────────────────────────────
# GoogleCalendarBackend：请求构造
# ──────────────────────────────────────────────────────────────


def test_get_event_returns_calendar_event(backend: GoogleCalendarBackend) -> None:
    backend.service.queue(result=_event_payload("evt-9"))
    event = backend.get_event("evt-9")

    assert event.event_id == "evt-9"
    assert event.etag == '"etag-1"'
    assert event.auto_mail_key == "fp-1"
    assert backend.service.calls[0][1]["eventId"] == "evt-9"


def test_get_event_not_found_raises_specific_error(
    backend: GoogleCalendarBackend,
) -> None:
    backend.service.queue(error=FakeHttpError(404, "Not Found"))
    with pytest.raises(CalendarNotFoundError):
        backend.get_event("missing")


def test_insert_requires_auto_mail_key(backend: GoogleCalendarBackend) -> None:
    """无所有权标记必须拒绝——它是我方事件的唯一凭据。"""
    with pytest.raises(ValueError) as exc:
        backend.insert_event({"summary": "无标记"})
    assert "auto_mail_key" in str(exc.value)


# ──────────────────────────────────────────────────────────────
# 全天事件的时间语义（实测校准）
# ──────────────────────────────────────────────────────────────


def test_all_day_uses_local_date_not_utc_date() -> None:
    """**实测 bug**：全天事件曾用 ``ts[:10]`` 取 UTC 日期，比当地日期早一天。

    当地 2026-09-21 零点存成 ``2026-09-20T16:00:00Z``；直接取字符串前 10 位
    会得到 09-20，于是「9-21 截止」在日历上显示成 9-20。
    """
    payload = build_event_payload(
        title="網上回條登記截止", start_ts="2026-09-20T16:00:00Z", end_ts=None,
        all_day=True, auto_mail_key="k", timezone="Asia/Shanghai",
    )
    assert payload["start"] == {"date": "2026-09-21"}, "必须用当地自然日"
    # 当地时间 23:30 的事件同样不能落到 UTC 的前一天
    payload2 = build_event_payload(
        title="晚間截止", start_ts="2026-09-21T15:30:00Z", end_ts=None,
        all_day=True, auto_mail_key="k", timezone="Asia/Shanghai",
    )
    assert payload2["start"] == {"date": "2026-09-21"}


def test_all_day_end_date_is_exclusive() -> None:
    """``end.date`` 是**开区间**：同日会存成零长度事件。

    实测：``start=03-01, end=03-01`` 被 Google 原样接受并存回同一天，
    即零长度。要表示「10-01 这一天」必须写 ``end=10-02``。
    """
    payload = build_event_payload(
        title="全天", start_ts="2026-09-20T16:00:00Z", end_ts="2026-09-20T16:30:00Z",
        all_day=True, auto_mail_key="k", timezone="Asia/Shanghai",
    )
    assert payload["start"] == {"date": "2026-09-21"}
    assert payload["end"] == {"date": "2026-09-22"}, "end 必须严格晚于 start"


def test_all_day_preserves_multi_day_span() -> None:
    """跨多天的全天事件（ICS 的 DTEND 是开区间）跨度不能丢失。"""
    payload = build_event_payload(
        title="三日展覽", start_ts="2026-09-30T16:00:00Z", end_ts="2026-10-02T16:00:00Z",
        all_day=True, auto_mail_key="k", timezone="Asia/Shanghai",
    )
    assert payload["start"] == {"date": "2026-10-01"}
    assert payload["end"] == {"date": "2026-10-03"}


def test_timed_events_keep_datetime_with_timezone() -> None:
    """定时事件仍用 dateTime + timeZone（不能被全天修正影响）。"""
    payload = build_event_payload(
        title="會議", start_ts="2026-10-01T02:30:00Z", end_ts="2026-10-01T03:00:00Z",
        all_day=False, auto_mail_key="k", timezone="Asia/Shanghai",
    )
    assert payload["start"] == {
        "dateTime": "2026-10-01T02:30:00Z", "timeZone": "Asia/Shanghai"
    }
    assert payload["end"] == {
        "dateTime": "2026-10-01T03:00:00Z", "timeZone": "Asia/Shanghai"
    }


def test_all_day_invalid_timezone_falls_back_to_utc() -> None:
    """时区名非法时退回 UTC，而不是抛异常（不能因配置笔误导致推送崩溃）。"""
    payload = build_event_payload(
        title="全天", start_ts="2026-09-20T16:00:00Z", end_ts=None,
        all_day=True, auto_mail_key="k", timezone="Not/AZone",
    )
    assert payload["start"] == {"date": "2026-09-20"}


def test_all_day_invalid_timestamp_does_not_crash() -> None:
    """无法解析的时间戳不应让推送崩溃（退回纪元日，人工审核时会看到异常）。"""
    payload = build_event_payload(
        title="全天", start_ts="not-a-date", end_ts=None,
        all_day=True, auto_mail_key="k", timezone="Asia/Shanghai",
    )
    assert payload["start"] == {"date": "1970-01-01"}


def test_insert_sends_payload_with_marker(backend: GoogleCalendarBackend) -> None:
    backend.service.queue(result=_event_payload("evt-new"))
    payload = build_event_payload(
        title="面试", start_ts="2026-10-01T02:00:00Z", end_ts="2026-10-01T03:00:00Z",
        all_day=False, auto_mail_key="fp-abc",
    )
    created = backend.insert_event(payload)
    assert created.event_id == "evt-new"

    _, kwargs = backend.service.calls[0]
    assert kwargs["body"]["extendedProperties"]["private"]["auto_mail_key"] == "fp-abc"


def test_update_preserves_marker(backend: GoogleCalendarBackend) -> None:
    """更新必须保留所有权标记，否则会「丢失」对事件的所有权。"""
    with pytest.raises(ValueError):
        backend.update_event("evt-1", {"summary": "无标记"})

    backend.service.queue(result=_event_payload("evt-1"))
    payload = build_event_payload(
        title="会议", start_ts="2026-10-01T02:00:00Z", end_ts=None,
        all_day=False, auto_mail_key="fp-1",
    )
    backend.update_event("evt-1", payload)
    _, kwargs = backend.service.calls[0]
    assert kwargs["calendarId"] == backend._calendar_id


def test_delete_missing_event_is_idempotent(backend: GoogleCalendarBackend) -> None:
    """删除已不存在的事件应视为成功——而不是报错。"""
    backend.service.queue(error=FakeHttpError(404))
    backend.delete_event("gone")  # 不应抛异常


def test_find_by_auto_mail_key_uses_extended_property(
    backend: GoogleCalendarBackend,
) -> None:
    """反查必须用 ``privateExtendedProperty=key=value`` 形式。

    这是幂等的关键：``insert`` 前先查一次，命中则回填而非重复创建。
    参数格式是 ``propertyName=value``（官方文档明确），且**不能与 syncToken
    同用**——因此这里用一次性查询。
    """
    backend.service.queue(result={"items": [_event_payload("evt-1")]})
    found = backend.find_by_auto_mail_key("fp-1")

    assert len(found) == 1
    _, kwargs = backend.service.calls[0]
    assert kwargs["privateExtendedProperty"] == "auto_mail_key=fp-1"
    assert "syncToken" not in kwargs, "该参数不能与 syncToken 同用"


def test_find_by_auto_mail_key_paginates(backend: GoogleCalendarBackend) -> None:
    """必须跟随 nextPageToken，否则会漏掉第二页的匹配事件。"""
    backend.service.queue(
        result={"items": [_event_payload("evt-1")], "nextPageToken": "page2"}
    )
    backend.service.queue(result={"items": [_event_payload("evt-2")]})

    found = backend.find_by_auto_mail_key("fp-1")
    assert len(found) == 2
    assert backend.service.calls[1][1]["pageToken"] == "page2"


def test_find_by_auto_mail_key_ignores_empty_key(
    backend: GoogleCalendarBackend,
) -> None:
    assert backend.find_by_auto_mail_key("") == []
    assert backend.service.calls == [], "空键不该发起请求"


def test_find_requests_show_deleted(backend: GoogleCalendarBackend) -> None:
    """反查必须带 showDeleted。

    否则「已归档（cancelled）」的事件会被当成不存在，重建时会重复创建。
    """
    backend.service.queue(result={"items": []})
    backend.find_by_auto_mail_key("fp-1")
    _, kwargs = backend.service.calls[0]
    assert kwargs["showDeleted"] is True


def test_list_events_skips_cancelled(backend: GoogleCalendarBackend) -> None:
    backend.service.queue(
        result={
            "items": [
                _event_payload("a"),
                _event_payload("b", status="cancelled"),
            ]
        }
    )
    events = backend.list_events()
    assert [e.event_id for e in events] == ["a"]


def test_list_events_orders_by_start_time(backend: GoogleCalendarBackend) -> None:
    backend.service.queue(result={"items": []})
    backend.list_events()
    _, kwargs = backend.service.calls[0]
    assert kwargs["orderBy"] == "startTime"
    assert kwargs["singleEvents"] is True


# ──────────────────────────────────────────────────────────────
# 错误映射与重试
# ──────────────────────────────────────────────────────────────


def test_auth_error_maps_to_calendar_auth_error(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """401/403 应映射为授权错误，提示可能需要重新 auth。"""
    svc = FakeService()
    svc.queue(error=FakeHttpError(401, "Unauthorized"))
    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=0)

    with pytest.raises(CalendarAuthError) as exc:
        backend.get_event("evt-1")
    assert "auth" in str(exc.value)


def test_rate_limit_403_is_retried(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """403 可能是配额耗尽而非权限问题，应重试。"""
    svc = FakeService()
    svc.queue(error=FakeHttpError(403, "quotaExceeded: Rate Limit Exceeded"))
    svc.queue(result=_event_payload("evt-1"))
    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=1, backoff=0)

    event = backend.get_event("evt-1")
    assert event.event_id == "evt-1"
    assert backend.stats.retries == 1


def test_server_error_is_retried(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    svc = FakeService()
    svc.queue(error=FakeHttpError(503, "Service Unavailable"))
    svc.queue(result=_event_payload("evt-1"))
    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=1, backoff=0)

    assert backend.get_event("evt-1").event_id == "evt-1"
    assert backend.stats.retries == 1


def test_server_error_exhausts_retries(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    svc = FakeService()
    for _ in range(3):
        svc.queue(error=FakeHttpError(503))
    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=2, backoff=0)

    with pytest.raises(CalendarError):
        backend.get_event("evt-1")


def test_bad_request_is_not_retried(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """400 重试无意义（参数错了），应直接失败。"""
    svc = FakeService()
    svc.queue(error=FakeHttpError(400, "Bad Request"))
    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=3, backoff=0)

    with pytest.raises(CalendarError):
        backend.get_event("evt-1")
    assert backend.stats.retries == 0


def test_conflict_409_maps_to_conflict_error(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    svc = FakeService()
    svc.queue(error=FakeHttpError(409, "Conflict"))
    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=0)

    from automail.calendar.backend import CalendarConflictError

    with pytest.raises(CalendarConflictError):
        backend.get_event("evt-1")


def test_network_error_is_retried(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    svc = FakeService()
    svc.queue(error=OSError("connection reset"))
    svc.queue(result=_event_payload("evt-1"))
    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=1, backoff=0)

    assert backend.get_event("evt-1").event_id == "evt-1"


def test_status_of_extracts_code_from_various_shapes() -> None:
    """不同版本的 google 库暴露状态码的形态不同，必须都能取到。"""
    assert _status_of(FakeHttpError(404)) == 404

    class OnlyText(Exception):
        def __str__(self) -> str:
            return "503 Service Unavailable"

    assert _status_of(OnlyText()) == 503

    class NoStatus(Exception):
        pass

    assert _status_of(NoStatus("boom")) == 0


def test_looks_like_rate_limit() -> None:
    assert _looks_like_rate_limit(FakeHttpError(403, "quotaExceeded")) is True
    assert _looks_like_rate_limit(FakeHttpError(403, "Rate Limit Exceeded")) is True
    assert _looks_like_rate_limit(FakeHttpError(403, "Forbidden")) is False


def test_require_event_id_rejects_empty(backend: GoogleCalendarBackend) -> None:
    with pytest.raises(ValueError):
        backend.get_event("")


def test_to_event_handles_missing_fields() -> None:
    event = _to_event({})
    assert event.event_id == ""
    assert event.etag is None
    assert event.auto_mail_key is None


# ──────────────────────────────────────────────────────────────
# 与 PushEngine 集成
# ──────────────────────────────────────────────────────────────


def test_push_engine_works_with_gcal_backend(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """PushEngine 的写入路径在真实后端上原样生效（接口一致）。"""
    from automail.push import PushEngine

    svc = FakeService()
    # find（反查）→ 未命中；insert → 成功
    svc.queue(result={"items": []})
    svc.queue(result=_event_payload("evt-created"))

    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=0)

    conn.execute(
        """
        INSERT INTO events (message_id, title, start_ts, end_ts, all_day, source,
            confidence, fingerprint, status, created_at, updated_at)
        VALUES (NULL, '面试', '2026-10-01T02:00:00Z', '2026-10-01T03:00:00Z', 0,
            'rules', 0.95, 'fp-gcal', 'approved', ?, ?)
        """,
        (iso(utcnow()), iso(utcnow())),
    )

    stats = PushEngine(settings, conn, backend).push_approved(apply=True)
    assert stats.created == 1

    row = conn.execute(
        "SELECT gcal_event_id, status FROM events WHERE fingerprint='fp-gcal'"
    ).fetchone()
    assert row["gcal_event_id"] == "evt-created"
    assert row["status"] == "pushed"


def test_push_engine_backfills_from_gcal(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """真实后端的幂等反查同样能回填（覆盖「创建成功但写库失败」）。"""
    from automail.push import PushEngine

    svc = FakeService()
    svc.queue(result={"items": [_event_payload("evt-existing")]})

    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=0)
    conn.execute(
        """
        INSERT INTO events (message_id, title, start_ts, end_ts, all_day, source,
            confidence, fingerprint, status, created_at, updated_at)
        VALUES (NULL, '面试', '2026-10-01T02:00:00Z', '2026-10-01T03:00:00Z', 0,
            'rules', 0.95, 'fp-backfill', 'approved', ?, ?)
        """,
        (iso(utcnow()), iso(utcnow())),
    )

    stats = PushEngine(settings, conn, backend).push_approved(apply=True)
    assert stats.backfilled == 1
    assert stats.created == 0

    row = conn.execute(
        "SELECT gcal_event_id FROM events WHERE fingerprint='fp-backfill'"
    ).fetchone()
    assert row["gcal_event_id"] == "evt-existing"


def test_archive_uses_update_not_delete(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """归档必须是 update（置 cancelled），**不能**走 delete。

    delete 是硬删除、不可恢复；归档要可恢复。
    """
    from automail.push import PushEngine

    svc = FakeService()
    svc.queue(result=_event_payload("evt-1"))          # get
    svc.queue(result=_event_payload("evt-1"))          # update
    backend = GoogleCalendarBackend(settings, conn=conn, service=svc, retries=0)

    conn.execute(
        """
        INSERT INTO events (message_id, title, start_ts, source, fingerprint,
            status, gcal_event_id, created_at, updated_at)
        VALUES (NULL, '会议', '2026-10-01T02:00:00Z', 'rules', 'fp-1',
            'pushed', 'evt-1', ?, ?)
        """,
        (iso(utcnow()), iso(utcnow())),
    )
    event_id = conn.execute("SELECT id FROM events WHERE fingerprint='fp-1'").fetchone()[0]

    stats = PushEngine(settings, conn, backend).archive(event_id, apply=True)
    assert stats.archived == 1

    methods = [name for name, _ in svc.calls]
    assert "delete" not in methods, "归档不得硬删除"
    assert "update" in methods
    _, kwargs = svc.calls[methods.index("update")]
    assert kwargs["body"]["status"] == "cancelled"
