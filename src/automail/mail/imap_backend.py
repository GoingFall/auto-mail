"""IMAP 后端实现（面向 163/Coremail 调优）。

163 的三个必须处理的行为（详见 docs/spec-imap-sync.md §1）：

1. **认证后必须发送 IMAP ``ID``**（RFC 2971），否则 ``SELECT``/``EXAMINE`` 返回
   ``NO SELECT Unsafe Login. Please contact kefu@188.com``。
2. **不能走 SASL-IR**：Coremail 先声明 ``SASL-IR`` 能力再拒绝 inline 形式。
   本实现用明文 ``LOGIN``，因此天然避开；``IMAP_USE_SASL_IR`` 仅供显式声明，
   不做 SASL 认证。
3. **无 ``IDLE``、无服务端 ``THREAD``**，只能轮询 + 客户端线程重建。

只读保证
--------
* 取正文一律用 ``BODY.PEEK[]``。**绝不能**用 ``BODY[]``——那会触发 ``\\Seen``，
  把用户未读的邮件标记为已读，是真实的副作用。
* ``select_folder`` 默认走 ``EXAMINE``（只读）。本模块不提供任何写操作。
"""

from __future__ import annotations

import logging

from ..settings import Settings
from .backend import (
    FolderInfo,
    FolderStatus,
    MailAuthError,
    MailConnectionError,
    MailProtocolError,
    RawMessage,
    UnsafeLoginError,
)

logger = logging.getLogger("automail.mail.imap")

#: 163 在 SELECT 前未收到 ID 时返回的特征串
UNSAFE_LOGIN_MARKER = "Unsafe Login"

#: 取正文用的 FETCH 项。PEEK 是关键：不改变 \Seen 状态。
BODY_FETCH_ITEM = "BODY.PEEK[]"

#: 唯一允许回写的标志。刻意只支持这一个——见 ``mark_seen``。
SEEN_FLAG = "\\Seen"

#: 可能的正文键名（PEEK 在响应里不回显，不同实现略有差异）
_BODY_KEYS = (b"BODY[]", b"BODY[]<0>", b"RFC822")


class ImapBackend:
    """基于 ``imapclient`` 的只读 IMAP 后端。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None
        self._folder_cache: list[FolderInfo] | None = None

    # ── 连接生命周期 ──────────────────────────────────────

    def connect(self) -> None:
        """建立连接、认证，并按 163 要求发送 ``ID``。"""
        from imapclient import IMAPClient

        settings = self._settings
        try:
            self._client = IMAPClient(
                settings.imap_host,
                port=settings.imap_port,
                ssl=settings.imap_use_ssl,
                timeout=settings.imap_socket_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - imapclient 抛的异常种类多
            raise MailConnectionError(f"无法连接 {settings.imap_host}：{exc}") from exc

        try:
            self._client.login(settings.imap_user, settings.imap_auth_code_value)
        except Exception as exc:  # noqa: BLE001
            self._close_quietly()
            raise MailAuthError(
                f"登录失败：{exc}；请确认使用的是 16 位客户端授权码而非网页登录密码"
            ) from exc

        # 163 要求认证后立即表明身份
        self._send_id()

    def close(self) -> None:
        self._close_quietly()

    def _close_quietly(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - 关闭失败不应影响主流程
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self) -> ImapBackend:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── ID 命令与 Unsafe Login 处理 ──────────────────────

    def _send_id(self) -> None:
        """发送 RFC 2971 ``ID``。能力不含 ID 时静默跳过。"""
        client = self._require_client()
        try:
            # 注意：imapclient 返回 bytes，必须先解码再比较，
            # 否则 `"ID" in {b"ID"}` 恒为 False，会静默跳过 ID 命令，
            # 进而在 163 上触发 Unsafe Login。
            caps = {_to_text(c).upper() for c in client.capabilities()}
        except Exception as exc:  # noqa: BLE001
            raise MailProtocolError(f"无法获取服务端能力：{exc}") from exc

        if "ID" not in caps:
            logger.debug("服务端未声明 ID 能力，跳过 ID 命令")
            return

        parameters = {
            "name": self._settings.imap_client_id_name,
            "version": self._settings.imap_client_id_version,
            "vendor": self._settings.imap_client_id_vendor,
        }
        support_email = self._settings.imap_client_id_support_email.strip()
        if support_email:
            parameters["support-email"] = support_email

        try:
            client.id_(parameters)
        except Exception as exc:  # noqa: BLE001 - ID 失败不致命，留待 SELECT 阶段暴露
            logger.warning("发送 IMAP ID 失败（将在 SELECT 时验证）：%s", exc)

    @staticmethod
    def _is_unsafe_login(exc: Exception) -> bool:
        return UNSAFE_LOGIN_MARKER.lower() in str(exc).lower()

    # ── 内部访问器 ────────────────────────────────────────

    def _require_client(self):
        if self._client is None:
            raise MailConnectionError("尚未连接；请先调用 connect()")
        return self._client

    def capabilities(self) -> set[str]:
        client = self._require_client()
        try:
            return {_to_text(c).upper() for c in client.capabilities()}
        except Exception as exc:  # noqa: BLE001
            raise MailProtocolError(f"无法获取服务端能力：{exc}") from exc

    # ── 文件夹 ────────────────────────────────────────────

    def list_folders(self) -> list[FolderInfo]:
        if self._folder_cache is not None:
            return self._folder_cache

        client = self._require_client()

        try:
            raw_folders = client.list_folders()
        except Exception as exc:  # noqa: BLE001
            raise MailProtocolError(f"列出文件夹失败：{exc}") from exc

        result: list[FolderInfo] = []
        for flags, delimiter, name in raw_folders:
            flag_texts = tuple(_to_text(f) for f in (flags or ()))
            result.append(
                FolderInfo(
                    name=name,
                    delimiter=_to_text(delimiter) or "/",
                    flags=flag_texts,
                    special_use=_special_use_from_flags(flag_texts),
                )
            )

        self._folder_cache = result
        return result

    def find_special_folder(self, marker: str) -> str | None:
        """按 ``\\Sent``/``\\Junk`` 等标记查找文件夹名。

        163 会在 ``LIST`` 响应里直接给出这些标记（因为它声明了 ``SPECIAL-USE``），
        因此无需依赖 ``XLIST``。见 :func:`_special_use_from_flags`。
        """
        wanted = marker.lower()
        for folder in self.list_folders():
            for mark in folder.special_use:
                if mark.lower() == wanted:
                    return folder.name
        return None

    # ── 选中与查询 ────────────────────────────────────────

    def select_folder(self, folder: str, *, readonly: bool = True) -> FolderStatus:
        """选中文件夹，返回 UIDVALIDITY / UIDNEXT / EXISTS。

        遇到 ``Unsafe Login`` 时按规格处理：**记录原始响应 → 重发 ID → 重试一次**。
        仍失败才抛出 :class:`UnsafeLoginError`，绝不当作凭据错误。
        """
        try:
            return self._select_once(folder, readonly=readonly)
        except Exception as exc:  # noqa: BLE001
            if not self._is_unsafe_login(exc):
                raise MailProtocolError(f"选中文件夹 {folder} 失败：{exc}") from exc
            logger.warning("遇到 Unsafe Login，记录原始响应并重发 ID 后重试：%s", exc)
            self._send_id()
            try:
                return self._select_once(folder, readonly=readonly)
            except Exception as retry_exc:  # noqa: BLE001
                raise UnsafeLoginError(
                    f"重发 ID 后仍被拒绝（{folder}）：{retry_exc}；"
                    "该账号可能被服务端限流，请稍后重试或检查是否有"
                    "「阻止了一次不安全的收信请求」告警邮件"
                ) from retry_exc

    def _select_once(self, folder: str, *, readonly: bool) -> FolderStatus:
        client = self._require_client()
        try:
            # readonly=True → EXAMINE，只读且不改变 \Recent/\Seen
            response = client.select_folder(folder, readonly=readonly)
        except Exception as exc:  # noqa: BLE001
            raise exc

        uid_validity = _first_int(response, b"UIDVALIDITY")
        if uid_validity is None:
            raise MailProtocolError(f"文件夹 {folder} 未返回 UIDVALIDITY，无法安全增量同步")
        uid_next = _first_int(response, b"UIDNEXT")
        exists = _first_int(response, b"EXISTS") or 0
        return FolderStatus(uid_validity=uid_validity, uid_next=uid_next, exists=exists)

    def search_uids(self, criterion: str) -> list[int]:
        """按条件搜索 UID；结果排序去重。

        ``criterion`` 是**条件本身**（如 ``"ALL"``、``"1000:*"``），
        **不要**再写 ``"UID"`` 前缀。

        因为 ``IMAPClient`` 以 ``use_uid=True`` 构造，``search()`` 已自动发出
        ``UID SEARCH ...`` 并把结果按 UID 返回。若再传一个 ``"UID"`` 条件，
        线路上会变成 ``UID SEARCH UID ALL`` —— 这是**非法语法**，真实服务端
        （163/Coremail）会直接返回 ``BAD Parse command error``。
        """
        client = self._require_client()
        try:
            uids = client.search([criterion])
        except Exception as exc:  # noqa: BLE001
            raise MailProtocolError(f"搜索 UID {criterion} 失败：{exc}") from exc
        return sorted({int(u) for u in uids})

    # ── 取回 ──────────────────────────────────────────────

    def fetch_messages(self, uids: list[int]) -> dict[int, RawMessage]:
        """按 UID 取回完整邮件。

        使用 ``BODY.PEEK[]``：**不改变已读状态**。这是只读同步的硬要求。
        """
        if not uids:
            return {}
        client = self._require_client()
        try:
            response = client.fetch(list(uids), [BODY_FETCH_ITEM, "FLAGS", "INTERNALDATE"])
        except Exception as exc:  # noqa: BLE001
            raise MailProtocolError(f"取回邮件失败：{exc}") from exc

        result: dict[int, RawMessage] = {}
        for uid, data in response.items():
            raw = _extract_body(data)
            if raw is None:
                # 没有正文的响应（例如只有 FLAGS 的未 solicited 更新）跳过
                continue
            result[int(uid)] = RawMessage(
                uid=int(uid),
                raw=raw,
                flags=_extract_flags(data),
                internal_date=_extract_internal_date(data),
                size=len(raw),
            )
        return result

    def fetch_flags(self, uids: list[int]) -> dict[int, tuple[str, ...]]:
        """只取 FLAGS，用于刷新已存在邮件的状态（不下载正文）。"""
        if not uids:
            return {}
        client = self._require_client()
        try:
            response = client.fetch(list(uids), ["FLAGS"])
        except Exception as exc:  # noqa: BLE001
            raise MailProtocolError(f"取回 FLAGS 失败：{exc}") from exc
        return {int(uid): _extract_flags(data) for uid, data in response.items()}

    def mark_seen(self, uids: list[int], *, seen: bool = True) -> None:
        """给邮件加/去 ``\\Seen``。

        **本项目唯一的邮箱写操作。** 只改这一个标志，不做别的任何事。

        两个必须注意的点：

        1. **文件夹必须以可写方式选中**（``select_folder(readonly=False)``）。
           只读（EXAMINE）选中时服务端会拒绝 STORE——这正好是一层保护：
           忘了切到写模式不会是静默失败，而是明确的报错。
        2. ``silent=True`` 让服务端不回送更新后的 FLAGS；本地状态由
           ``marked_read_at`` 记录，不需要这份回执，省一次往返流量。
        """
        if not uids:
            return
        client = self._require_client()
        try:
            if seen:
                client.add_flags(list(uids), [SEEN_FLAG], silent=True)
            else:
                client.remove_flags(list(uids), [SEEN_FLAG], silent=True)
        except Exception as exc:  # noqa: BLE001
            raise MailProtocolError(f"设置 \\Seen 失败：{exc}") from exc


# ──────────────────────────────────────────────────────────────
# 响应解析辅助（纯函数，便于单测）
# ──────────────────────────────────────────────────────────────

def _to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


#: 特殊用途标记（RFC 6154 + XLIST 遗留）：这些才是「这个文件夹是干什么用的」的信号
_SPECIAL_USE_MARKS = frozenset(
    {
        "\\all",
        "\\allmail",
        "\\archive",
        "\\drafts",
        "\\flagged",
        "\\important",
        "\\inbox",
        "\\junk",
        "\\sent",
        "\\spam",
        "\\starred",
        "\\trash",
    }
)

#: 结构性标记：描述树形结构而非用途，不应当作 special_use
_STRUCTURAL_MARKS = frozenset(
    {
        "\\haschildren",
        "\\hasnochildren",
        "\\noselect",
        "\\nonexistent",
        "\\subscribed",
        "\\remote",
        "\\children",
    }
)


def _special_use_from_flags(flags: tuple[str, ...]) -> tuple[str, ...]:
    """从 ``LIST`` 的 flags 中挑出特殊用途标记。

    真实 163 在 ``LIST`` 响应里直接给出 ``\\Sent``/``\\Junk``/``\\Trash``/
    ``\\Drafts``（它声明了 ``SPECIAL-USE``），因此**不需要** ``XLIST``。

    此前实现依赖 ``imap4.xlist()``，但 CPython 的 ``imaplib`` 并没有注册这个
    命令（``AttributeError: Unknown IMAP4 command: 'xlist'``），异常被吞掉后
    所有文件夹的 ``special_use`` 都是空——导致 ``find_special_folder("\\Sent")``
    永远返回 None，无法稳健定位「已发送」「垃圾邮件」等中文名文件夹。
    """
    marks: list[str] = []
    for flag in flags:
        lowered = flag.lower()
        if lowered in _SPECIAL_USE_MARKS:
            marks.append(flag)
        elif lowered in _STRUCTURAL_MARKS:
            continue
    return tuple(marks)


def _first_int(response: dict, key: bytes) -> int | None:
    """从 SELECT 响应里取整数。

    imapclient 对不同字段有不同键（``b'UIDNEXT'`` 与 ``b'UIDVALIDITY'``），
    且值可能是 tuple（多值响应）。这里统一取第一个可转成整数的值。
    """
    value = response.get(key)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            try:
                return int(item)
            except (TypeError, ValueError):
                continue
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_body(data: dict) -> bytes | None:
    """从 FETCH 响应中取出正文字节。"""
    for key in _BODY_KEYS:
        value = data.get(key)
        if isinstance(value, bytes):
            return value
    return None


def _extract_flags(data: dict) -> tuple[str, ...]:
    flags = data.get(b"FLAGS")
    if not flags:
        return ()
    if isinstance(flags, (list, tuple)):
        return tuple(_to_text(f) for f in flags)
    return (_to_text(flags),)


def _extract_internal_date(data: dict) -> str | None:
    from ..db import iso

    value = data.get(b"INTERNALDATE")
    if value is None:
        return None
    try:
        return iso(value)  # imapclient 已把 INTERNALDATE 解析为 datetime
    except Exception:  # noqa: BLE001
        return _to_text(value) or None
