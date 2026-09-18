"""邮件后端协议与数据载体。

把「同步算法」与「具体 IMAP 实现」解耦，好处有二：

1. 同步算法（UIDNEXT 预判门、reactivate、补偿扫描）可以用**假后端**做完整测试，
   不需要真实网络；
2. 未来接入其它邮箱（QQ、Gmail）时只需再写一个实现。

协议刻意保持窄：只暴露同步真正需要的操作。**特别是只读**——v1 不提供任何
写操作（不标已读、不改 flag、不删邮件），因此协议里没有对应方法。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class MailError(Exception):
    """邮件后端的基础异常。"""


class MailAuthError(MailError):
    """认证失败（凭据错误/授权码失效）。"""


class UnsafeLoginError(MailError):
    """163 的 ``Unsafe Login``——需要先发 IMAP ID 再重试。

    单独成类是因为它与凭据无关，**不应**被当成密码错误处理。
    """


class MailConnectionError(MailError):
    """网络/连接层失败。"""


class MailProtocolError(MailError):
    """服务端返回了不符合预期的响应。"""


@dataclass(slots=True)
class FolderStatus:
    """``SELECT``/``STATUS`` 的关键返回值。

    ``uid_next`` 为 None 表示服务端未返回该字段——同步算法必须能处理这种情况
    （走客户端过滤兜底，见 docs/spec-imap-sync.md §2）。
    """

    uid_validity: int
    uid_next: int | None
    exists: int


@dataclass(slots=True)
class RawMessage:
    """一封取回的原始邮件。

    ``raw`` 是用 ``BODY.PEEK[]`` 取回的完整字节——**必须用 PEEK**，
    否则会触发 ``\\Seen``，把用户的邮件标记为已读。
    """

    uid: int
    raw: bytes
    flags: tuple[str, ...] = ()
    internal_date: str | None = None
    size: int | None = None


@dataclass(slots=True)
class FolderInfo:
    """文件夹元信息。"""

    name: str
    delimiter: str = "/"
    flags: tuple[str, ...] = ()
    special_use: tuple[str, ...] = field(default=())
    """如 ``\\Sent``/``\\Junk``/``\\Trash``/``\\Drafts``（来自 SPECIAL-USE/XLIST）。"""


@runtime_checkable
class MailBackend(Protocol):
    """同步算法依赖的最小接口。"""

    def connect(self) -> None:
        """建立连接并完成认证。"""

    def close(self) -> None:
        """关闭连接（幂等）。"""

    def capabilities(self) -> set[str]:
        """服务端能力集（大写）。用于判断是否支持 ID/SPECIAL-USE 等。"""

    def list_folders(self) -> list[FolderInfo]:
        """列出文件夹。"""

    def select_folder(self, folder: str, *, readonly: bool = True) -> FolderStatus:
        """选中文件夹并返回状态。

        ``readonly=True`` 走 ``EXAMINE`` 而非 ``SELECT``——只读同步不应有
        任何写权限，这既是安全姿态也能避免服务端因状态变更做额外处理。
        """

    def search_uids(self, criterion: str) -> list[int]:
        """按条件搜索 UID，结果升序。``criterion`` 例如 ``"1000:*"``。"""

    def fetch_messages(self, uids: list[int]) -> dict[int, RawMessage]:
        """按 UID 取回邮件（BODY.PEEK，不改变已读状态）。"""

    def fetch_flags(self, uids: list[int]) -> dict[int, tuple[str, ...]]:
        """只取 flags，用于刷新已存在邮件的状态。"""

    def mark_seen(self, uids: list[int], *, seen: bool = True) -> None:
        """给邮件加上（或去掉）``\\Seen``。

        **这是本项目唯一的邮箱写操作**，因此接口刻意保持极窄：只能改 ``\\Seen``
        这一个标志——不能删邮件、不能移动、不能改动其它标志。

        调用方必须已用**可写**方式选中文件夹（``readonly=False``）；只读选中
        时服务端会拒绝 STORE。
        """
