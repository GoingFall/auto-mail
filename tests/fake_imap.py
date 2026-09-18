"""协议级假 IMAP 服务器。

为什么不用 mock 库替换 `IMAPClient`：那样只能验证「我们调用了什么」，无法验证
**真实 IMAP 会话的行为**。本模块起一个真实的 TCP 服务端，让 `imapclient` 走完整
的协议往返，因此能真正卡住这些行为：

* 只说 ``BODY[]`` 就会把邮件标成已读 —— 断言必须出现 ``BODY.PEEK[]``
* 163 的 ``Unsafe Login`` —— 断言客户端在收到它之后重发 ``ID`` 并重试
* ``UIDNEXT`` / ``UIDVALIDITY`` 的真实响应格式
* ``UID SEARCH n:*`` 在追平后仍返回最后一封（RFC 3501 §6.4.8 的边界）

服务端在后台线程里跑，测试结束自动关闭。
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field


@dataclass
class FakeFolder:
    """假邮箱文件夹。"""

    name: str
    uid_validity: int = 1
    messages: dict[int, bytes] = field(default_factory=dict)
    """UID → 原始邮件字节。"""

    flags: dict[int, set[str]] = field(default_factory=dict)

    def add(self, uid: int, raw: bytes | str, flags: set[str] | None = None) -> None:
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.messages[uid] = raw
        self.flags[uid] = set(flags or ())

    @property
    def uid_next(self) -> int:
        return (max(self.messages) + 1) if self.messages else 1


class FakeImapServer:
    """最小的 IMAP4rev1 服务端，只实现本项目用到的命令。

    支持的能力开关用于测试分支行为（例如有没有 ID、XLIST）。
    """

    def __init__(
        self,
        *,
        capabilities: tuple[str, ...] = (
            "IMAP4rev1",
            "ID",
            "UIDPLUS",
            "XLIST",
            "SPECIAL-USE",
        ),
        username: str = "user@163.com",
        password: str = "authcode",
        require_id_before_select: bool = False,
        reject_first_select: bool = False,
        advertise_uidnext: bool = False,
    ) -> None:
        self.folders: dict[str, FakeFolder] = {}
        self.capabilities = capabilities
        self.username = username
        self.password = password

        #: 若为 True：SELECT 前未收到 ID 就返回 Unsafe Login
        self.require_id_before_select = require_id_before_select
        #: 若为 True：第一次 SELECT 总是返回 Unsafe Login（用于测试重试逻辑）
        self.reject_first_select = reject_first_select
        #: 是否在 SELECT/STATUS 里返回 UIDNEXT。
        #: **默认 False，与真实 163 一致**——163 即使被显式请求也不返回 UIDNEXT，
        #: 因此同步必须走客户端过滤路径。若要测试 UIDNEXT 预判门，显式置 True。
        self.advertise_uidnext = advertise_uidnext

        # ── 观测点（测试断言用） ──
        self.commands: list[str] = []
        self.fetch_items: list[str] = []
        self.id_calls: list[str] = []
        self.select_count = 0
        self.search_criteria: list[str] = []
        self.authenticated = False
        #: 观测已读回写：每次 STORE 的目标 UID 与操作（add/remove）。
        self.store_calls: list[tuple[str, list[int]]] = []
        #: 当前选中的文件夹是否可写。**只读选中时 STORE 必须被拒绝**——
        #: 这是真实服务端的行为，也是「忘记切到写模式」的唯一防线。
        self._selected_readonly = True

        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client: socket.socket | None = None
        self.port: int | None = None

    # ── 生命周期 ──────────────────────────────────────────

    def start(self) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._client, self._sock):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> FakeImapServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ── 服务循环 ──────────────────────────────────────────

    def _serve(self) -> None:
        """接受并服务多个连接，直到 stop() 被调用。

        必须能处理多次连接：一个测试里常常先同步一次、再同步第二次，
        每次都会新建 IMAPClient。只 accept 一次会让第二次连接挂到超时。
        """
        assert self._sock is not None
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                client, _addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._client = client
            try:
                self._handle(client)
            except OSError:
                pass
            finally:
                try:
                    client.close()
                except OSError:
                    pass

    def _handle(self, client: socket.socket) -> None:
        self._send(client, f"* OK [CAPABILITY {' '.join(self.capabilities)}] ready")
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = client.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk

            # 逐条解析客户端命令。必须支持字面量：imapclient 在参数含特殊字符
            # 或非 ASCII 时会发 `{n}\r\n<n bytes>`，按行切分会把它拆坏。
            while True:
                line_end = buffer.find(b"\r\n")
                if line_end < 0:
                    break
                line = buffer[:line_end]
                literal_size = self._trailing_literal_size(line)
                if literal_size is not None:
                    body_start = line_end + 2
                    if len(buffer) < body_start + literal_size:
                        break  # 字面量还没收全，等下一个 chunk
                    literal = buffer[body_start:body_start + literal_size]
                    buffer = buffer[body_start + literal_size:]
                    command_line = (line + b" " + literal).decode(
                        "utf-8", errors="replace"
                    )
                else:
                    buffer = buffer[line_end + 2:]
                    command_line = line.decode("utf-8", errors="replace")

                if not command_line:
                    continue
                response = self._dispatch(command_line)
                if response is not None:
                    self._send(client, response)
                if response == "* BYE":
                    return

    @staticmethod
    def _trailing_literal_size(line: bytes) -> int | None:
        """若命令行以 ``{n}`` 结尾，返回 n（表示后面跟 n 字节字面量）。"""
        if not line.endswith(b"}"):
            return None
        start = line.rfind(b"{")
        if start < 0:
            return None
        try:
            return int(line[start + 1:-1])
        except ValueError:
            return None

    @staticmethod
    def _send(client: socket.socket, text: str) -> None:
        try:
            client.sendall((text + "\r\n").encode("utf-8"))
        except OSError:
            pass

    # ── 命令分发 ──────────────────────────────────────────

    def _dispatch(self, line: str) -> str | None:
        parts = line.split(" ", 2)
        if len(parts) < 2:
            return None
        tag, command = parts[0], parts[1].upper()
        rest = parts[2] if len(parts) > 2 else ""
        self.commands.append(command)
        # SELECT 与 EXAMINE 共用处理函数，需要知道当前是哪个才能正确判定
        # 只读语义（进而决定是否拒绝 STORE）。
        self._current_command = command

        handler = {
            "CAPABILITY": self._cmd_capability,
            "LOGIN": self._cmd_login,
            "AUTHENTICATE": self._cmd_authenticate,
            "ID": self._cmd_id,
            "LIST": self._cmd_list,
            "LSUB": self._cmd_list,
            "XLIST": self._cmd_xlist,
            "SELECT": self._cmd_select,
            "EXAMINE": self._cmd_select,
            "STATUS": self._cmd_status,
            "SEARCH": self._cmd_search,
            "UID": self._cmd_uid,
            "FETCH": self._cmd_fetch,
            "STORE": self._cmd_store,
            "LOGOUT": self._cmd_logout,
            "NOOP": lambda t, r: f"{t} OK NOOP completed",
        }.get(command)

        if handler is None:
            return f"{tag} BAD Unknown command {command}"
        return handler(tag, rest)

    def _cmd_capability(self, tag: str, _rest: str) -> str:
        return f"* CAPABILITY {' '.join(self.capabilities)}\r\n{tag} OK CAPABILITY completed"

    def _cmd_login(self, tag: str, rest: str) -> str:
        tokens = rest.split()
        if len(tokens) < 2:
            return f"{tag} BAD LOGIN needs user and password"
        user = tokens[0].strip('"')
        password = tokens[1].strip('"')
        if user != self.username or password != self.password:
            return f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials"
        self.authenticated = True
        return f"{tag} OK LOGIN completed"

    def _cmd_authenticate(self, tag: str, _rest: str) -> str:
        # 163/Coremail 会声明 SASL-IR 然后拒绝 inline 形式。假服务器干脆不支持，
        # 用来验证客户端不会走 SASL 路径。
        return f"{tag} NO AUTHENTICATE not supported"

    def _cmd_id(self, tag: str, rest: str) -> str:
        self.id_calls.append(rest)
        return f'{tag} OK ID completed'

    def _cmd_logout(self, tag: str, _rest: str) -> str:
        self.authenticated = False
        return "* BYE logging out"

    # ── 文件夹列举 ────────────────────────────────────────

    def _folder_lines(self, kind: str) -> list[str]:
        """列出文件夹。名字按 mUTF-7 编码（与真实 IMAP 一致）。

        imapclient 会解码回中文，因此客户端看到的是「订阅邮件」这样的名字；
        服务端存储的键也是中文，两边通过这里的编解码对上。
        """
        from imapclient.imap_utf7 import encode as utf7_encode

        lines: list[str] = []
        special = {
            "已发送": "\\Sent",
            "垃圾邮件": "\\Junk",
            "已删除": "\\Trash",
            "草稿箱": "\\Drafts",
        }
        for name in self.folders:
            marks = f"\\HasNoChildren {special.get(name, '')}".strip()
            # imapclient 的 imap_utf7.encode 要求入参是 **str**（传 bytes 会
            # 原样返回，等于没编码），返回 **bytes**，需转 ascii 再拼进协议。
            wire_name = utf7_encode(name).decode("ascii")
            lines.append(f'* {kind} ({marks}) "/" "{wire_name}"')
        return lines

    def _cmd_list(self, tag: str, _rest: str) -> str:
        lines = self._folder_lines("LIST")
        return "\r\n".join([*lines, f"{tag} OK LIST completed"])

    def _cmd_xlist(self, tag: str, _rest: str) -> str:
        lines = self._folder_lines("XLIST")
        return "\r\n".join([*lines, f"{tag} OK XLIST completed"])

    # ── 选中 ──────────────────────────────────────────────

    @staticmethod
    def _decode_folder(name: str) -> str:
        """把线上传来的文件夹名（mUTF-7）解码为本地键名。

        ``imap_utf7.decode`` 要求入参是 **bytes**；str 会原样返回。
        """
        from imapclient.imap_utf7 import decode as utf7_decode

        return utf7_decode(name.encode("ascii"))

    def _cmd_select(self, tag: str, rest: str) -> str:
        raw_name = rest.strip().strip('"').split()[0] if rest.strip() else ""
        name = self._decode_folder(raw_name)
        # EXAMINE 与 SELECT 共用此处理函数，但语义不同：EXAMINE 是只读的。
        # 客户端必须让我们知道是哪一个，否则无法正确拒绝 STORE。
        is_examine = self._current_command == "EXAMINE"
        self.select_count += 1

        if self.require_id_before_select:
            # 持续拒绝：无论客户端发多少次 ID。用于验证最终抛出
            # UnsafeLoginError，而不是被误判为凭据错误。
            return f"{tag} NO SELECT Unsafe Login. Please contact kefu@188.com for help"
        if self.reject_first_select and self.select_count == 1:
            return f"{tag} NO SELECT Unsafe Login. Please contact kefu@188.com for help"

        folder = self.folders.get(name)
        if folder is None:
            return f"{tag} NO Mailbox does not exist"

        self._selected = folder
        self._selected_readonly = is_examine
        lines = [
            f"* {len(folder.messages)} EXISTS",
            "* 0 RECENT",
            "* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)",
            f"* OK [UIDVALIDITY {folder.uid_validity}] UIDs valid",
        ]
        if self.advertise_uidnext:
            lines.append(f"* OK [UIDNEXT {folder.uid_next}] Predicted next UID")
        kind = "READ-ONLY" if is_examine else "READ-WRITE"
        verb = "EXAMINE" if is_examine else "SELECT"
        return "\r\n".join([*lines, f"{tag} OK [{kind}] {verb} completed"])

    def _cmd_store(self, tag: str, rest: str) -> str:
        """处理 STORE —— 真实服务端在只读选中时会拒绝它。"""
        folder = getattr(self, "_selected", None)
        if folder is None:
            return f"{tag} NO No mailbox selected"
        if self._selected_readonly:
            # 真实 163/Coremail 的行为：只读邮箱不能存储任何标志。
            # 这条拒绝是「忘记切到写模式」的唯一防线，必须如实模拟。
            return f"{tag} NO STORE: mailbox is read-only"

        tokens = rest.split(" ", 1)
        if len(tokens) < 2:
            return f"{tag} BAD STORE needs sequence and items"
        uid_spec, items_text = tokens[0], tokens[1]

        # 本项目只允许 \Seen（见 backend.mark_seen 的窄接口）。
        # 客户端若试图改别的标志，应当明确失败而非静默忽略。
        if "\\SEEN" not in items_text.upper():
            return f"{tag} NO only \\Seen is supported"

        # imapclient 的实际线格式：
        #     UID STORE 1,2 +FLAGS.SILENT (\Seen)      ← add_flags
        #     UID STORE 1,2 -FLAGS.SILENT (\Seen)      ← remove_flags
        # 因此按 +/- 前缀判定增删；`.SILENT` 后缀不影响判定。
        head = items_text.split("(", 1)[0].upper().replace(".SILENT", "").strip()
        adding = not head.startswith("-")

        uids = self._apply_criteria(folder, uid_spec)
        self.store_calls.append(("add" if adding else "remove", list(uids)))

        for uid in uids:
            if adding:
                folder.flags.setdefault(uid, set()).add("\\Seen")
            else:
                folder.flags.setdefault(uid, set()).discard("\\Seen")
        return f"{tag} OK STORE completed"

    def _cmd_status(self, tag: str, rest: str) -> str:
        raw_name = rest.split()[0].strip('"')
        name = self._decode_folder(raw_name)
        folder = self.folders.get(name)
        if folder is None:
            return f"{tag} NO Mailbox does not exist"

        # 真实 163 会静默丢弃 UIDNEXT：即使显式请求也只回 MESSAGES/UIDVALIDITY。
        # 这里如实复现该行为，让同步的客户端过滤路径得到真实覆盖。
        items = f"MESSAGES {len(folder.messages)}"
        if self.advertise_uidnext:
            items += f" UIDNEXT {folder.uid_next}"
        items += f" UIDVALIDITY {folder.uid_validity}"
        return (
            f'* STATUS "{raw_name}" ({items})\r\n'
            f"{tag} OK STATUS completed"
        )

    # ── 搜索 ──────────────────────────────────────────────

    def _cmd_uid(self, tag: str, rest: str) -> str:
        sub, _, args = rest.partition(" ")
        sub_upper = sub.upper()
        if sub_upper == "SEARCH":
            return self._cmd_search(tag, args, uid_mode=True)
        if sub_upper == "FETCH":
            return self._cmd_fetch(tag, args)
        if sub_upper == "STORE":
            return self._cmd_store(tag, args)
        return f"{tag} BAD Unknown UID command"

    def _cmd_search(self, tag: str, rest: str, *, uid_mode: bool = False) -> str:
        self.search_criteria.append(rest)
        folder = getattr(self, "_selected", None)
        if folder is None:
            return f"{tag} NO No mailbox selected"

        # 严格校验语法：真实服务端（163/Coremail）对 `UID SEARCH UID ALL`
        # 会返回 `BAD Parse command error`。若这里宽容放过，测试就无法发现
        # 「重复加了 UID 前缀」这类 bug —— 我们确实踩过这个坑。
        text = rest.strip()
        upper = text.upper()
        if upper.startswith("UID"):
            return f"{tag} BAD [Parse command error] unexpected UID prefix"

        if uid_mode:
            # UID SEARCH 的结果是 UID 集合
            uids = self._apply_criteria(folder, text)
        else:
            # 序列号 SEARCH：本项目不使用，如实报不支持以免误用
            return f"{tag} BAD [Parse command error] sequence search not supported"

        return f"* SEARCH {' '.join(str(u) for u in uids)}\r\n{tag} OK SEARCH completed"

    def _apply_criteria(self, folder: FakeFolder, criteria: str) -> list[int]:
        """实现本项目用到的检索条件：``ALL`` 与 sequence-set。

        sequence-set 必须支持逗号分隔的集合（``1,2,5``），因为 imapclient 对
        UID 列表发送的是逗号形式而非范围形式——只认 ``n:m`` 会让多封邮件取回
        结果为空。每个元素可以是 ``n``、``n:m`` 或 ``n:*``。
        """
        text = criteria.upper().strip()

        if text in {"ALL", ""}:
            return sorted(folder.messages)

        result: set[int] = set()
        for element in text.split(","):
            element = element.strip()
            if not element:
                continue
            result.update(self._resolve_element(folder, element))
        return sorted(result)

    @staticmethod
    def _resolve_element(folder: FakeFolder, element: str) -> list[int]:
        """解析单个 sequence-set 元素。"""
        if ":" not in element:
            try:
                single = int(element)
            except ValueError:
                return []
            return [single] if single in folder.messages else []

        start_text, _, end_text = element.partition(":")
        try:
            start = int(start_text)
        except ValueError:
            return []

        end_text = end_text.strip()
        if end_text == "*":
            # RFC 3501 §6.4.8：`n:*` 始终包含最后一封邮件，
            # 即使 n 高于任何已分配 UID。这里如实复现该行为。
            if not folder.messages:
                return []
            highest = max(folder.messages)
            if start > highest:
                return [highest]
            return [uid for uid in folder.messages if uid >= start]

        try:
            end = int(end_text)
        except ValueError:
            return []
        low, high = (start, end) if start <= end else (end, start)
        return [uid for uid in folder.messages if low <= uid <= high]

    # ── 取回 ──────────────────────────────────────────────

    def _cmd_fetch(self, tag: str, rest: str) -> str:
        folder = getattr(self, "_selected", None)
        if folder is None:
            return f"{tag} NO No mailbox selected"

        tokens = rest.split(" ", 1)
        if len(tokens) < 2:
            return f"{tag} BAD FETCH needs sequence and items"
        uid_spec, items_text = tokens[0], tokens[1]
        self.fetch_items.append(items_text)

        upper_items = items_text.upper()
        # 记录是否用了 BODY[] 而非 BODY.PEEK[]（真实副作用检测）
        self.used_non_peek_body = (
            "BODY[" in upper_items and "BODY.PEEK[" not in upper_items
        )

        uids = self._apply_criteria(folder, uid_spec)
        lines: list[str] = []
        for uid in uids:
            raw = folder.messages[uid]
            flags = folder.flags.get(uid, set())
            flag_text = " ".join(sorted(flags))
            seq = self._seq_of(folder, uid)
            wants_body = "BODY.PEEK[]" in upper_items or "BODY[]" in upper_items

            if wants_body:
                key = "BODY[]"
                if "BODY[]" in upper_items and "BODY.PEEK[" not in upper_items:
                    # 非 PEEK 会真的把邮件标为已读——如实模拟
                    folder.flags.setdefault(uid, set()).add("\\Seen")
                # 字面量格式必须严格：{长度}\r\n<原始字节>，且不使用引号
                header = (
                    f"* {seq} FETCH (UID {uid} FLAGS ({flag_text}) "
                    f"{key} {{{len(raw)}}}"
                )
                literal = raw.decode("utf-8", errors="replace")
                lines.append(f"{header}\r\n{literal})")
            else:
                lines.append(f"* {seq} FETCH (UID {uid} FLAGS ({flag_text}))")

        return "\r\n".join([*lines, f"{tag} OK FETCH completed"])

    @staticmethod
    def _seq_of(folder: FakeFolder, uid: int) -> int:
        return sorted(folder.messages).index(uid) + 1
