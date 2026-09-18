"""Google OAuth 授权：把 ``credentials.json`` 换成可持续使用的 ``token.json``。

## 为什么需要这一步

Google 不允许程序用账号密码直接读写日历。它要求走 OAuth 授权流程：
**用户在浏览器里明确点「允许」**，Google 才发令牌给程序。

* ``credentials.json`` 只说明「这个客户端程序是谁」，**不含**任何授权
* ``token.json`` 才是「用户已授权」的凭据，含 refresh token

因此这一步无法省略，也无法由程序代替用户完成——必须有人点那一下。

## 授权范围（最小权限）

只用 ``calendar.events``：「查看和编辑日历事件」。

不用 ``calendar``（完整权限，含共享与永久删除）——本程序不需要共享日历，
也不做永久删除（默认归档）。最小权限意味着万一令牌泄露，损害面更小。

## 令牌失效与重新授权

``refresh token`` 会因以下原因失效，**这不是 bug**：

* 用户在 Google 账号设置里撤销了授权
* 用户改了密码（对含 Gmail scope 的应用）
* 六个月未使用
* Cloud 项目的 OAuth 同意屏处于 ``Testing`` 状态 → **约 7 天失效**
  （把发布状态改为 ``In production`` 可避免；但即使不发布，程序也能提示重新授权）

程序的处理：捕获 ``RefreshError`` 且 ``error == "invalid_grant"`` →
**不当作可重试错误**（重试再多次也不会成功），而是持久化
``needs_reauth`` 标记并提示用户重跑 ``auth``。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..db import MetaRepository

logger = logging.getLogger("automail.calendar.auth")

#: 只请求「日历事件」的读写权限（最小权限）。
#:
#: 刻意不用 ``https://www.googleapis.com/auth/calendar``——那是完整权限
#: （含共享日历与永久删除），本程序不需要。
SCOPES = ("https://www.googleapis.com/auth/calendar.events",)

#: token 文件里必须存在的字段（缺失说明文件损坏或被截断）
_REQUIRED_TOKEN_FIELDS = ("client_id", "client_secret", "refresh_token")


class AuthError(Exception):
    """授权失败。"""


class CredentialsMissingError(AuthError):
    """``credentials.json`` 不存在或格式不对。"""


class ReauthRequiredError(AuthError):
    """令牌已失效，需要用户重新授权。

    与「可重试的临时错误」区分：重试不会成功，必须走一次浏览器授权。
    """


@dataclass(slots=True)
class AuthStatus:
    """授权状态（供 doctor 与摘要展示）。"""

    credentials_file: Path
    token_file: Path
    credentials_present: bool
    token_present: bool
    needs_reauth: bool
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.credentials_present and self.token_present and not self.needs_reauth


# ──────────────────────────────────────────────────────────────
# 凭证文件校验
# ──────────────────────────────────────────────────────────────


def validate_credentials_file(path: Path) -> dict:
    """校验 ``credentials.json`` 并返回其内容。

    Raises:
        CredentialsMissingError: 文件不存在、不是合法 JSON、
            或不是「桌面应用」类型的 OAuth 客户端。
    """
    if not path.is_file():
        raise CredentialsMissingError(
            f"未找到 {path}。请到 Google Cloud Console 创建 OAuth 客户端：\n"
            "  1. 新建项目 → 启用 Google Calendar API\n"
            "  2. OAuth 同意屏幕（用户类型选「外部」）\n"
            "  3. 创建凭据 → OAuth 客户端 ID → 类型选「桌面应用」\n"
            "  4. 下载 JSON 并重命名为 credentials.json 放到项目根目录"
        )

    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise CredentialsMissingError(f"{path} 不是合法 JSON：{exc}") from exc

    if not isinstance(payload, dict):
        raise CredentialsMissingError(f"{path} 内容格式不对（应为 JSON 对象）")

    if "installed" not in payload:
        # 常见错误：建成了「Web 应用」类型。Web 类型需要手工填回调地址，
        # 桌面应用类型才能走 run_local_server 的自动回调。
        kind = next((k for k in ("web", "service_account") if k in payload), "未知")
        raise CredentialsMissingError(
            f"{path} 的客户端类型是 {kind!r}，但本程序需要「桌面应用」（installed）。\n"
            "请在 Google Cloud Console 重新创建凭据，类型选「桌面应用」。"
        )

    block = payload["installed"]
    for field in ("client_id", "client_secret"):
        if not block.get(field):
            raise CredentialsMissingError(f"{path} 缺少 {field}")

    return payload


def inspect_status(settings, conn: sqlite3.Connection | None = None) -> AuthStatus:
    """检查授权就绪状态，不触发任何网络请求。"""
    import json

    credentials = Path(settings.google_credentials_file)
    token = Path(settings.google_token_file)

    credentials_present = credentials.is_file()
    token_present = token.is_file()
    needs_reauth = False
    detail = ""

    if conn is not None:
        needs_reauth = MetaRepository(conn).get_bool(MetaRepository.NEEDS_REAUTH)

    if not credentials_present:
        detail = "缺少 credentials.json"
    elif not token_present:
        detail = "尚未授权（需要运行 automail auth）"
    elif needs_reauth:
        detail = "令牌已失效，需要重新授权（automail auth）"
    else:
        try:
            data = json.loads(token.read_text(encoding="utf-8"))
            missing = [f for f in _REQUIRED_TOKEN_FIELDS if not data.get(f)]
            if missing:
                detail = f"token.json 缺少字段：{', '.join(missing)}"
                token_present = False
            else:
                detail = "已授权"
        except (json.JSONDecodeError, OSError) as exc:
            detail = f"token.json 无法解析：{exc}"
            token_present = False

    return AuthStatus(
        credentials_file=credentials,
        token_file=token,
        credentials_present=credentials_present,
        token_present=token_present,
        needs_reauth=needs_reauth,
        detail=detail,
    )


# ──────────────────────────────────────────────────────────────
# 授权流程
# ──────────────────────────────────────────────────────────────


class _CallbackTimeoutError(Exception):
    """等待授权回调超时。"""


class _CallbackError(Exception):
    """回调本身带来了错误，或无法建立回调服务。"""


def _run_callback_server(
    *,
    flow,
    port: int,
    open_browser: bool,
    timeout_seconds: int | None,
    url_factory,
) -> str:
    """启动本地回调服务，等待浏览器带回授权码，返回完整回调 URL。

    ## 顺序很关键

    **先建服务拿到实际端口 → 设置 ``flow.redirect_uri`` → 才生成授权链接。**

    为什么：``authorization_url()`` 会把 ``redirect_uri`` 编进 URL，而该属性
    默认是 ``None``（``run_local_server`` 会自己设置它，我们不调那个方法）。
    若先生成链接，URL 里就**不含 redirect_uri**，Google 会直接拒绝请求。

    ``url_factory(redirect_uri)`` 由调用方提供，在端口确定后生成授权链接。

    ## 为什么不直接用 ``flow.run_local_server``

    它内部调用 ``wsgiref`` 的 ``handle_request()``，而那个方法**只处理一个
    请求**（其文档原话：``Handle one request, possibly blocking``）。
    于是任何**非授权的连接**都会把它消费掉，随后 ``last_request_uri`` 不存在，
    流程报「超时」——即使真正的授权回调马上就会到达。

    这类「非授权连接」在现实中很常见：

    * **端口探测**（脚本或工具检查端口是否在监听）
    * 浏览器为 ``localhost`` 建立的**预连接**（speculative connection）
    * 浏览器请求 ``/favicon.ico``，或在回调页之外尝试其它路径

    实测就踩到了这一点：一次端口探测让整个授权失败，而浏览器显示的是
    「localhost 拒绝连接」——因为服务已经退出了。

    ## 本实现的做法

    两道保险：

    1. **多线程服务**（``ThreadingHTTPServer``）。默认的 ``HTTPServer`` 是
       单线程，而 ``BaseHTTPRequestHandler`` 的读超时默认是 ``None``——
       一个**只连接、不发数据**的探测连接会**永久阻塞**那唯一的线程，
       真正的回调永远排不上队。
    2. **循环而非单次**：只有真正携带 ``code`` 或 ``error`` 的请求才结束等待，
       其余（探测/预连接/favicon）回一个无害响应后继续等。

    判据是请求里是否存在 ``code``/``error`` 参数，而不是「收到了任何请求」。
    """
    import http.server
    import threading
    import time
    import urllib.parse
    import webbrowser

    result: dict[str, str] = {}
    done = threading.Event()
    # state 由 url_factory 生成后填入（用于校验回调，防串号）
    expected_state: dict[str, str | None] = {"value": None}

    class _Handler(http.server.BaseHTTPRequestHandler):
        # 连接级读超时：防止「只连接、不发数据」的客户端占住线程不放。
        # 默认是 None（永久等待）——这正是端口探测能卡死服务的原因。
        timeout = 5

        def do_GET(self) -> None:  # noqa: N802 - 基类命名
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            # 只认携带授权码或显式错误的回调，其余（探测/预连接/favicon）忽略
            has_code = bool(params.get("code"))
            has_error = bool(params.get("error"))
            if not (has_code or has_error):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            # state 必须匹配：防止把别的请求误当授权结果
            state_value = expected_state["value"]
            if state_value and params.get("state", [None])[0] != state_value:
                self.send_response(400)
                body = b"state mismatch"
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if has_error:
                result["error"] = params["error"][0]
            else:
                # 只存**路径**（含查询串）。完整 URL 在拿到端口后另行拼接——
                # 这里拿不到端口，且 http 必须改写成 https。
                result["path"] = self.path

            body = (
                "授权完成，可以关闭此页面并回到终端。"
                if has_code
                else "授权被拒绝，请回到终端查看说明。"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args: object) -> None:
            # 静音默认的访问日志，避免污染 CLI 输出
            return

    # **必须**用 ThreadingHTTPServer：默认的 HTTPServer 是单线程，
    # 一个挂起的探测连接会卡住唯一的处理线程，真正的授权回调永远排不上队。
    #
    # 同时关闭 allow_reuse_address：它的默认值是 1，会让「端口已被占用」时
    # 仍然绑定成功（Windows 上尤其如此），于是我们不报错，而是让使用者干等到
    # 超时——那条错误信息完全指错方向（看起来像没人授权，实际是端口冲突）。
    # 该开关必须在**实例化前**通过类属性设置，构造之后再改是无效的。
    class _Server(http.server.ThreadingHTTPServer):
        allow_reuse_address = False

    try:
        server = _Server(("localhost", port), _Handler)
    except OSError as exc:
        raise _CallbackError(
            f"无法在 localhost:{port or '随机端口'} 启动本地回调服务：{exc}"
        ) from exc

    actual_port = server.server_address[1]

    # **顺序关键**：端口确定后才设置 redirect_uri，然后才生成授权链接。
    # authorization_url() 会把 redirect_uri 编进 URL，而该属性默认是 None
    # （run_local_server 会自己设置它，我们不调那个方法）。先生成链接会得到
    # 一个**不含 redirect_uri** 的 URL，Google 直接拒绝。
    redirect_uri = f"http://localhost:{actual_port}/"
    flow.redirect_uri = redirect_uri
    result["redirect_uri"] = redirect_uri
    auth_url, state_value = url_factory(redirect_uri)
    expected_state["value"] = state_value

    if open_browser:
        webbrowser.open(auth_url, new=1, autoraise=True)

    # 链接必须独占一行并 flush：使用者要盯着它操作，缓冲会让终端像卡住了
    print(_prompt_message().format(url=auth_url), flush=True)
    logger.info("本地回调服务已启动：%s", redirect_uri)

    deadline = (
        time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    )

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while not done.is_set():
            if deadline is not None and time.monotonic() > deadline:
                raise _CallbackTimeoutError(
                    "Timed out waiting for response from authorization server"
                )
            done.wait(0.5)
    finally:
        server.shutdown()
        server.server_close()

    if "error" in result:
        error = result["error"]
        raise _CallbackError(
            f"授权被拒绝（{error}）。\n" + _explain_flow_failure(Exception(error))
        )

    path = result.get("path")
    if not path:
        raise _CallbackTimeoutError(
            "Timed out waiting for response from authorization server"
        )

    # **必须构造完整的绝对 URL，且 scheme 用 https。**
    #
    # 两个坑，都踩过：
    #
    # ① HTTP 请求里的 ``self.path`` 只是路径（如 ``/?code=...&state=...``），
    #    **不是**完整 URL。若直接把它交给 ``fetch_token``，oauthlib 会因为
    #    看不出 scheme 而报 ``(insecure_transport) OAuth 2 MUST utilize https``。
    #
    # ② oauthlib 按 OAuth 2.0 规范**强制要求**授权响应走 TLS，因此本地的
    #    ``http://`` 必须改写成 ``https://``（Google 的桌面客户端流程里，
    #    loopback 回调实际是 http，这一步是官方库自己也在做的等价处理）。
    #
    # 注意只改 scheme 前缀，不要对整串做替换——授权码本身可能含 "http" 字样。
    redirect_uri = result.get("redirect_uri") or f"http://localhost:{actual_port}/"
    base = redirect_uri
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    if not base.endswith("/"):
        base += "/"
    return base.rstrip("/") + path


def _explain_flow_failure(exc: Exception) -> str:
    """把授权流程的异常翻成可执行的说明。

    为什么需要这个：Google 在授权页**直接拒绝**时（最常见的
    ``403 access_denied``），浏览器不会回跳到本地服务，因此我们这边只能
    观察到「等待回调超时」。若不解释，使用者会以为是程序坏了或网络问题，
    而真正的原因（OAuth 测试用户名单）根本没被提示。

    这是实测踩到的：`calendar.events` 属**敏感** scope，应用处于「测试」
    发布状态，而**项目所有者不会自动成为测试用户**，必须手动添加。
    """
    text = str(exc)
    lowered = text.lower()

    if "timed out" in lowered or "timeout" in lowered:
        return (
            "等待授权回调超时（本地服务未收到任何有效回调）。\n"
            "\n"
            "有两种可能，按可能性排序：\n"
            "\n"
            "**一、你还没在浏览器里完成授权**（最常见）\n"
            "   本地服务只在进程存活期间监听。若你在链接过期前没点完「允许」，\n"
            "   服务就会关闭，此时浏览器会显示「localhost 拒绝了连接」。\n"
            "   解决：重新运行 automail auth，并**在浏览器打开后立刻点允许**。\n"
            "   加 --timeout 0（默认）可让服务不限时等待。\n"
            "\n"
            "**二、Google 在授权页直接拒绝了**（页面显示「错误 403：access_denied」）\n"
            "   那种情况下浏览器不会回跳到本程序，所以这里只能看到超时——\n"
            "   真实的拒绝原因在浏览器页面上。若是这种情况，请检查：\n"
            "\n"
            "  1. Google Cloud Console → APIs 和服务 → OAuth 同意屏幕\n"
            "     （现名 Google Auth Platform）\n"
            "  2. 进入 **Audience**：\n"
            "     · 用户类型应为「外部」\n"
            "     · 发布状态应为「测试」\n"
            "     · 在 **测试用户** 里点「+ Add users」，\n"
            "       添加你**实际用来授权的那个 Google 账号**并保存\n"
            "  3. 注意：**项目所有者不会自动成为测试用户**，必须手动添加自己\n"
            "     （这是最容易忽略的一点）\n"
            "  4. 保存后，用**无痕窗口**重新打开授权链接。\n"
            "     浏览器若同时登录多个 Google 账号，可能自动选了未列入名单的\n"
            "     那个——错误页最后一行「联系开发者 <邮箱>」写的就是它。\n"
            "\n"
            "另外两点说明：\n"
            "  · 出现「Google 尚未验证此应用」是正常的，点「高级 → 继续前往」即可。\n"
            "    `calendar.events` 属敏感 scope，自建应用不会通过 Google 审核，\n"
            "    单人自用也不需要审核。\n"
            "  · 测试状态下 refresh token 约 7 天失效，届时程序会提示重新授权。\n"
            "    想避免的话需把发布状态改为「In production」（敏感 scope 可能\n"
            "    触发验证流程；单人自用可忽略该提示继续使用）。"
        )

    if "access_denied" in lowered:
        return (
            "Google 拒绝了授权请求（access_denied）。\n"
            "请检查 OAuth 同意屏的 Audience 设置：用户类型为「外部」、\n"
            "发布状态为「测试」，并把你要用来授权的 Google 账号加入「测试用户」。\n"
            "项目所有者不会自动成为测试用户。"
        )

    return f"授权失败：{text}"


def _prompt_message() -> str:
    """构造授权提示。
    ``run_local_server`` 打印该消息且**默认不 flush**；在重定向/管道场景下
    使用者会一直看不到授权链接，终端看起来像卡住了。把链接单独成行，
    并在前后加分隔线，便于复制。
    """
    bar = "=" * 72
    return "\n".join(
        [
            "",
            bar,
            "请在浏览器中打开下面的链接完成授权（若已自动打开可忽略）：",
            "",
            "{url}",
            "",
            bar,
        ]
    )


def run_authorization(
    settings,
    *,
    conn: sqlite3.Connection | None = None,
    open_browser: bool = True,
    port: int = 0,
    timeout_seconds: int | None = 300,
) -> Path:
    """执行一次浏览器授权，把令牌写入 ``token.json``。

    Args:
        open_browser: 是否自动打开浏览器。``False`` 时会打印 URL 供手工访问
            （在没有图形界面的环境里用）。
        port: 本地回调端口。``0`` 表示由系统分配空闲端口。
        timeout_seconds: 等待用户完成授权的最长秒数；``None`` 表示不限。

    Returns:
        写入的 token 文件路径。

    Raises:
        CredentialsMissingError: ``credentials.json`` 有问题。
        AuthError: 授权流程失败或超时。
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials_file = Path(settings.google_credentials_file)
    validate_credentials_file(credentials_file)

    # 代理：Google 在部分网络环境下不可直连
    settings.apply_proxy_env()

    flow = InstalledAppFlow.from_client_secrets_file(
        str(credentials_file), list(SCOPES)
    )

    # 授权链接必须在**端口确定之后**生成——见 _run_callback_server 的说明：
    # authorization_url() 把 flow.redirect_uri 编进 URL，而该属性默认是 None。
    # access_type='offline' + prompt='consent' 确保拿到 refresh token；
    # 不加 prompt='consent' 时，重新授权可能只返回短期 access token 而**不给**
    # refresh token，导致以后每次运行都要重开浏览器。
    #
    # 注意：**不要**把 redirect_uri 作为参数传给 authorization_url()——
    # oauthlib 内部已经传了它，再传会报 "got multiple values for keyword
    # argument 'redirect_uri'"。正确做法是设置 flow.redirect_uri 属性
    # （_run_callback_server 已设好）。
    def _build_auth_url(_redirect_uri: str) -> tuple[str, str | None]:
        return flow.authorization_url(access_type="offline", prompt="consent")

    try:
        callback = _run_callback_server(
            flow=flow,
            url_factory=_build_auth_url,
            port=port,
            open_browser=open_browser,
            timeout_seconds=timeout_seconds,
        )
    except _CallbackTimeoutError as exc:
        raise AuthError(_explain_flow_failure(exc)) from exc
    except _CallbackError as exc:
        raise AuthError(str(exc)) from exc

    try:
        # fetch_token 需要完整回调 URL（含 code 与 state）
        flow.fetch_token(authorization_response=callback)
    except Exception as exc:  # noqa: BLE001 - oauthlib 异常种类多
        raise AuthError(_explain_flow_failure(exc)) from exc

    credentials = flow.credentials

    token_file = Path(settings.google_token_file)
    try:
        token_file.write_text(credentials.to_json(), encoding="utf-8")
    except OSError as exc:
        raise AuthError(f"无法写入 {token_file}：{exc}") from exc

    # 清除「需要重新授权」标记
    if conn is not None:
        MetaRepository(conn).delete(MetaRepository.NEEDS_REAUTH)

    logger.info("授权成功，令牌已写入 %s", token_file)
    return token_file


def load_credentials(
    settings,
    *,
    conn: sqlite3.Connection | None = None,
):
    """加载已授权的凭据，必要时自动续期。

    Raises:
        CredentialsMissingError: 缺 credentials.json 或 token.json。
        ReauthRequiredError: refresh token 已失效（``invalid_grant``）。
            调用方应提示用户重跑 ``auth``，**不要**重试。
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = Path(settings.google_token_file)
    if not token_file.is_file():
        raise CredentialsMissingError(
            f"未找到 {token_file}——请先运行 automail auth 完成授权"
        )

    settings.apply_proxy_env()

    try:
        credentials = Credentials.from_authorized_user_file(str(token_file), list(SCOPES))
    except (ValueError, OSError) as exc:
        raise CredentialsMissingError(f"{token_file} 无法解析：{exc}") from exc

    if credentials.valid:
        return credentials

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            if _is_invalid_grant(exc):
                # **不重试**：invalid_grant 意味着 refresh token 已被撤销/过期，
                # 再试多少次都不会成功，只会浪费调用并掩盖真实原因。
                if conn is not None:
                    MetaRepository(conn).set(MetaRepository.NEEDS_REAUTH, "1")
                raise ReauthRequiredError(
                    "Google 授权已失效（invalid_grant），需要重新授权：\n"
                    "  运行 automail auth\n"
                    "可能原因：在 Google 账号中撤销了授权、修改了密码、"
                    "或 Cloud 项目的 OAuth 同意屏处于 Testing 状态（约 7 天失效）。"
                ) from exc
            raise AuthError(f"刷新令牌失败：{exc}") from exc

        # 续期成功：把新令牌写回，避免每次运行都刷新
        try:
            token_file.write_text(credentials.to_json(), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 磁盘问题
            logger.warning("无法写回续期后的令牌：%s", exc)

        if conn is not None:
            MetaRepository(conn).delete(MetaRepository.NEEDS_REAUTH)
        return credentials

    raise ReauthRequiredError(
        "令牌无效且无 refresh token，需要重新授权：运行 automail auth"
    )


def _is_invalid_grant(exc: Exception) -> bool:
    """判断是否为 ``invalid_grant``（需重新授权，不可重试）。

    错误形态在不同 google-auth 版本里略有差异，因此同时检查
    ``error`` 字段与错误文本。
    """
    error = getattr(exc, "error", None)
    if isinstance(error, str) and "invalid_grant" in error.lower():
        return True
    return "invalid_grant" in str(exc).lower()


def revoke_local_token(settings, *, conn: sqlite3.Connection | None = None) -> bool:
    """删除本地令牌文件（不撤销 Google 侧授权）。

    用于「换一个 Google 账号」的场景：删掉 token.json 后重跑 ``auth``
    即可用另一个账号授权。
    """
    token_file = Path(settings.google_token_file)
    removed = False
    if token_file.is_file():
        try:
            token_file.unlink()
            removed = True
        except OSError as exc:
            raise AuthError(f"无法删除 {token_file}：{exc}") from exc

    if conn is not None:
        MetaRepository(conn).delete(MetaRepository.NEEDS_REAUTH)
    return removed


def describe_setup_steps() -> str:
    """给使用者看的环境准备说明（doctor / auth 失败时展示）。

    第 3 步「添加测试用户」是**实测最容易遗漏**的一步：`calendar.events`
    属敏感 scope，应用处于「测试」发布状态时只有测试用户能授权，
    而**项目所有者不会自动进入该名单**——漏掉它会得到 403 access_denied。
    """
    return (
        "Google Calendar 接入需要四步：\n"
        "  1. Google Cloud Console 新建项目，启用「Google Calendar API」\n"
        "  2. OAuth 同意屏幕（现名 Google Auth Platform）→ Branding：\n"
        "     填写应用名与用户支持邮箱（缺这两项会导致 access_denied）\n"
        "  3. 同一页面的 Audience：\n"
        "     · 用户类型选「外部」\n"
        "     · 在「测试用户」里 **+ Add users** 添加你实际用来授权的\n"
        "       Google 账号（项目所有者不会自动加入，必须手动添加自己）\n"
        "     · 发布状态保持「测试」即可；改为「In production」可避免\n"
        "       refresh token 约 7 天失效（敏感 scope 可能触发验证流程）\n"
        "  4. 创建凭据 → OAuth 客户端 ID → 类型「桌面应用」→ 下载 JSON\n"
        "     重命名为 credentials.json 放到项目根目录\n"
        "然后运行：automail auth"
    )


def default_redirect_note() -> str:
    """说明回调地址从何而来（使用者常问的一个问题）。"""
    return (
        "授权时程序会临时启动 http://localhost:<随机端口> 作为回调地址，"
        "并自动打开浏览器。这就是 credentials.json 里 redirect_uris "
        "为 http://localhost 的用途——桌面应用无需手工填写回调地址。"
    )


__all__ = [
    "AuthError",
    "AuthStatus",
    "CredentialsMissingError",
    "ReauthRequiredError",
    "SCOPES",
    "describe_setup_steps",
    "default_redirect_note",
    "inspect_status",
    "load_credentials",
    "revoke_local_token",
    "run_authorization",
    "validate_credentials_file",
]

# 便于在受限环境下跳过浏览器（供测试与文档引用）
ENV_NO_BROWSER = "AUTOMAIL_NO_BROWSER"


def should_open_browser() -> bool:
    """是否自动打开浏览器（``AUTOMAIL_NO_BROWSER=1`` 可关闭）。"""
    return os.environ.get(ENV_NO_BROWSER, "").strip().lower() not in {"1", "true", "yes"}
