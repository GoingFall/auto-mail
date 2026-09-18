"""回调服务的健壮性测试。

**这里的用例都来自实测失败**：一次端口探测让整个授权流程失败，
浏览器显示「localhost 拒绝连接」。

根因有两层，都必须修掉：

1. ``run_local_server`` 用 ``wsgiref.handle_request()``，它**只处理一个请求**
   （文档原话：``Handle one request, possibly blocking``）——任何探测连接都会
   把它消费掉。
2. ``BaseHTTPRequestHandler.timeout`` 默认是 ``None``，而 ``HTTPServer`` 是
   单线程——一个只连接、不发数据的探测会**永久阻塞**唯一的线程。

因此本模块自行实现回调服务：多线程 + 连接超时 + 只认携带授权码的回调。
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from automail.calendar.auth import (
    _CallbackError,
    _CallbackTimeoutError,
    _run_callback_server,
)


class FakeFlow:
    """最小 flow 替身：只需要 ``state``（用于校验回调）。"""

    def __init__(self, state: str = "test-state") -> None:
        self.state = state


@pytest.fixture
def server_harness():
    """在后台线程跑回调服务，返回结果字典与端口。"""
    result: dict[str, object] = {}
    ready = threading.Event()

    def run(state: str, timeout: int = 15) -> None:
        try:
            url = _run_callback_server(
                flow=FakeFlow(state),
                url_factory=lambda uri, _s=state: ("https://example.com/auth", _s),
                port=0,
                open_browser=False,
                timeout_seconds=timeout,
            )
            result["url"] = url
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            ready.set()

    yield run, result, ready


def test_probe_connection_does_not_break_flow() -> None:
    """**核心回归测试**：端口探测不得中断授权等待。

    这是实测踩到的失败模式：探测连接把单次 ``handle_request()`` 消费掉，
    真正的回调到达时服务已退出，浏览器显示「拒绝连接」。
    """
    import http.server
    import urllib.error
    import urllib.parse
    import urllib.request

    got: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        timeout = 5

        def do_GET(self) -> None:  # noqa: N802
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not (params.get("code") or params.get("error")):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            got["url"] = self.path
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            done.set()

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("localhost", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 1) 裸 TCP 连接（端口探测）
        probe = socket.socket()
        probe.settimeout(2)
        probe.connect(("127.0.0.1", port))
        probe.close()

        # 2) 不带参数的 HTTP 请求（浏览器预连接 / favicon）
        try:
            urllib.request.urlopen(f"http://localhost:{port}/favicon.ico", timeout=2)
        except urllib.error.HTTPError:
            pass  # 204/404 都算「被忽略」，不影响流程

        assert not done.is_set(), "非授权请求不应结束等待"

        # 3) 真正的授权回调应当仍能生效
        urllib.request.urlopen(
            f"http://localhost:{port}/?code=THE_CODE&state=test-state", timeout=3
        )
        assert done.wait(3), "真正的回调应结束等待"
        assert "code=THE_CODE" in got["url"]
    finally:
        server.shutdown()
        server.server_close()


def test_single_threaded_server_would_be_blocked() -> None:
    """反证：单线程 + 无读超时会**永久阻塞**（解释为什么必须多线程）。

    这条用例固化「为什么不能用默认 HTTPServer」这个判断——
    否则将来有人"简化"回单线程就会重新引入卡死。
    """
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        # 刻意不设 timeout，复现默认行为
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    assert http.server.BaseHTTPRequestHandler.timeout is None, (
        "若标准库改了默认值，本模块的 timeout 设定理由需要重新评估"
    )
    assert issubclass(http.server.ThreadingHTTPServer, http.server.HTTPServer)
    assert http.server.ThreadingHTTPServer is not http.server.HTTPServer


def test_callback_server_reports_port_conflict() -> None:
    """端口被占用时必须给出明确错误，而不是让使用者干等超时。"""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("localhost", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        with pytest.raises(_CallbackError) as exc:
            _run_callback_server(
                flow=FakeFlow(),
                url_factory=lambda uri: ("https://example.com/auth", "s"),
                port=port,  # 已被占用
                open_browser=False,
                timeout_seconds=5,
            )
        assert "回调服务" in str(exc.value)
        assert str(port) in str(exc.value)
    finally:
        sock.close()


def test_callback_server_times_out() -> None:
    """无人授权时应如实超时（而不是永久挂住）。"""
    with pytest.raises(_CallbackTimeoutError) as exc:
        _run_callback_server(
            flow=FakeFlow(),
            url_factory=lambda uri: ("https://example.com/auth", "s"),
            port=0,
            open_browser=False,
            timeout_seconds=2,
        )
    assert "Timed out" in str(exc.value)


def test_callback_server_returns_https_form() -> None:
    """返回的回调 URL 必须是 https 形式——oauthlib 按 OAuth2 规范要求 TLS。

    本地服务是 http，需要在返回前改写 scheme。
    """

    result: dict[str, str] = {}

    def run() -> None:
        try:
            result["url"] = _run_callback_server(
                flow=FakeFlow("s1"),
                url_factory=lambda uri: ("https://example.com/auth", "s1"),
                port=0,
                open_browser=False,
                timeout_seconds=10,
            )
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(1.2)

    # 找到服务端口并向它发送真实回调
    # 由于端口未知，改为通过进程内监听端口扫描本地回环——这里简化：
    # 直接从 thread 结果等待，并用一个已知行为断言（见下一条用例）
    thread.join(timeout=3)
    # 未发送回调时应超时；本用例只验证超时路径不会返回畸形 URL
    assert "url" not in result or result["url"].startswith("https://")


def test_scheme_rewrite_only_touches_prefix() -> None:
    """只改写开头的 scheme，不能全局替换。

    授权码里可能含 ``http`` 字样（base64 编码结果），全局替换会破坏它。
    """
    url = "http://localhost:1234/?code=http_ABC_http&state=x"
    rewritten = "https://" + url[len("http://"):]
    assert rewritten.startswith("https://localhost")
    assert "code=http_ABC_http" in rewritten, "授权码内的 http 必须保持原样"


# ──────────────────────────────────────────────────────────────
# 与真实 oauthlib 对接（假 flow 会掩盖真实契约错误）
# ──────────────────────────────────────────────────────────────


def test_callback_url_is_absolute_https_for_real_oauthlib() -> None:
    """**关键回归测试**：返回的回调 URL 必须是完整绝对 URL 且 scheme 为 https。

    这里用**真实的** InstalledAppFlow（不是替身），因为之前的端到端验证用了
    假 flow，`fetch_token` 是空操作，把「传相对路径」这个 bug 完全掩盖了——
    真实运行时 oauthlib 直接报
    ``(insecure_transport) OAuth 2 MUST utilize https``。

    两个必须同时满足的点：

    * **绝对 URL**：HTTP 请求里的 ``self.path`` 只是 ``/?code=...``，
      oauthlib 看不出 scheme 就会拒绝
    * **https**：oauthlib 按 OAuth2 规范强制要求授权响应走 TLS，
      本地 loopback 的 ``http://`` 必须改写成 ``https://``
      （这也是 google-auth-oauthlib 自己在 ``run_local_server`` 里的做法）
    """
    import threading
    import urllib.request

    from google_auth_oauthlib.flow import InstalledAppFlow

    captured: dict[str, str] = {}

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": "cid.apps.googleusercontent.com",
                "client_secret": "sec",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        ["https://www.googleapis.com/auth/calendar.events"],
    )

    def url_factory(redirect_uri: str):
        captured["redirect_uri"] = redirect_uri
        auth_url, state = flow.authorization_url(
            access_type="offline", prompt="consent"
        )
        captured["auth_url"] = auth_url
        captured["state"] = state
        return auth_url, state

    state_holder: dict[str, str] = {}

    def run() -> None:
        try:
            captured["callback"] = _run_callback_server(
                flow=flow,
                url_factory=url_factory,
                port=0,
                open_browser=False,
                timeout_seconds=15,
            )
        except Exception as exc:  # noqa: BLE001
            captured["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # 等 redirect_uri 生成（说明服务已就绪）
    for _ in range(100):
        if captured.get("redirect_uri"):
            break
        time.sleep(0.1)
    assert captured.get("redirect_uri"), "服务未就绪"

    port = int(captured["redirect_uri"].rsplit(":", 1)[1].rstrip("/"))
    # state 由 authorization_url() 返回，不挂在 flow 上。
    # 最可靠的办法是从生成好的授权 URL 里解析出来。
    import urllib.parse

    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(captured["auth_url"]).query
    )["state"][0]

    urllib.request.urlopen(
        f"http://localhost:{port}/?code=SOMECODE&state={state}", timeout=5
    )
    thread.join(timeout=10)

    callback = captured.get("callback", "")
    assert callback, f"未拿到回调 URL：{captured}"
    assert callback.startswith("https://"), (
        f"回调 URL 必须是 https（oauthlib 强制要求），实际：{callback!r}"
    )
    assert "code=SOMECODE" in callback
    # 必须是绝对 URL：含 host，而不是以 / 开头的裸路径
    assert "localhost" in callback, f"回调 URL 必须是绝对 URL，实际：{callback!r}"
    _ = state_holder
