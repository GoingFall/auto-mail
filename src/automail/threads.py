"""线程重建：从 References / In-Reply-To 重建邮件会话。

## 为什么必须客户端重建

163 **不支持服务端 THREAD 扩展**（RFC 5256，实测 capability 里没有），
因此线程只能自己算。

## 算法（取自 notmuch / mu 的实践）

1. **父节点选择**：优先 ``In-Reply-To``；否则遍历 ``References`` 链，
   选**最接近本封**的已知祖先（深度最大者）。
   （只取 References 首项是不够的——它常指向线程根，而我们要挂到最近祖先上。）
2. **幽灵节点**：子邮件的父级尚未入库时，为那个**缺失的 Message-ID**
   分配一个稳定的占位锚点，使共享同一缺失祖先的邮件聚在一起。
   真身到达后自然归入同一线程（查找键就是它自己的 Message-ID）。
3. **无 Message-ID** 的邮件（国产邮箱常见）：回退用**内容哈希**做键，
   而不是丢弃——丢弃会让这些邮件全部变成孤立线程。
4. **循环引用**：按时间顺序处理，父链上做已访问检测；真出现环时取最早那封为根。

## 为什么做全量重建而不是增量

全量重建的结果**只取决于输入**，因此不会随时间漂移，也不需要「幽灵变真身」
的专门合并逻辑——重建时真身若已在库里，自然就位。

代价是每次重建会重新分配 ``threads.id``。v1 不依赖线程 id 的持久性
（``awaiting_since`` 等字段属于 v2），因此这是划算的取舍。等 v2 需要持久化
线程状态时，再改成增量并保留 id 映射。

## 弱关联

有些客户端不维护 ``References``，但会保留主题（``Re: xxx``）。
这类只能靠**规范化主题**关联，可靠性低，因此标 ``link_strength='weak'``——
**弱关联不得作为事实呈现**（摘要里不能说「这个线程在等你回复」）。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from .db import utcnow_iso
from .extract.fingerprint import normalize_title
from .settings import Settings

logger = logging.getLogger("automail.threads")

#: 无 Message-ID 邮件用的合成键前缀
SYNTHETIC_KEY_PREFIX = "sha256:"

#: 主题里出现这些词说明是自动化通知而非对话。
#: 用于阻止「同主题弱关联」把周期性通知合成假线程（详见 ``_merge_by_subject``）。
_AUTOMATED_SUBJECT_HINTS = (
    "提醒", "通知", "预告", "告警", "账单", "回执", "確認", "确认", "預約",
    "對帳", "对账", "验证码", "驗證碼",
    "notification", "notice", "reminder", "alert", "receipt", "invoice",
    "statement", "verification", "confirm", "no-reply", "noreply",
)


@dataclass(slots=True)
class ThreadStats:
    """重建统计。"""

    messages: int = 0
    threads: int = 0
    strong: int = 0
    weak: int = 0
    ghosts: int = 0
    """锚点是「尚未入库的 Message-ID」的线程数（幽灵节点）。"""

    orphaned_by_missing_id: int = 0
    """因缺少 Message-ID 而改用内容哈希做键的邮件数。"""

    cycles_broken: int = 0
    merged_by_subject: int = 0
    """仅凭主题弱关联合并进来的孤立邮件数。"""

    dry_run: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "messages": self.messages,
            "threads": self.threads,
            "strong": self.strong,
            "weak": self.weak,
            "ghosts": self.ghosts,
            "orphaned_by_missing_id": self.orphaned_by_missing_id,
            "cycles_broken": self.cycles_broken,
            "merged_by_subject": self.merged_by_subject,
        }


@dataclass
class _Node:
    """重建期间的一封邮件的图节点。"""

    row_id: int
    message_id: str | None
    in_reply_to: str | None
    references: list[str]
    subject_norm: str
    received_at: str | None
    from_addr: str
    synthetic_key: str = ""
    auto_submitted: str | None = None
    """自动化投递标记（如 ``Auto-Submitted: auto-replied``）。

    参与「是否允许主题弱关联」的判断——自动化通知不是对话。
    """

    @property
    def anchor_key(self) -> str:
        """该邮件作为线程锚点时的键。"""
        return self.message_id or self.synthetic_key


@dataclass
class _Thread:
    """构建中的线程。"""

    root_key: str
    nodes: list[_Node] = field(default_factory=list)
    strength: str = "strong"
    is_ghost: bool = False


class ThreadBuilder:
    """重建 ``threads`` 并回填 ``messages.thread_id``。"""

    def __init__(self, settings: Settings, conn: sqlite3.Connection) -> None:
        self._settings = settings
        self._conn = conn
        self._account = settings.account
        self._user_addresses = {
            addr.strip().lower() for addr in settings.user_address_list if addr.strip()
        }
        #: 本次构建中已分配的锚点键（含幽灵锚点）。
        #: 为什么要它：构建期间数据库尚未写入，无法靠查询发现「同一批里
        #: 前面刚创建的幽灵锚点」，只能在这里记着。
        self._anchors: dict[str, _Thread] = {}

    # ── 入口 ──────────────────────────────────────────────

    def rebuild(self, *, apply: bool = False) -> ThreadStats:
        """全量重建线程。

        Args:
            apply: ``False``（默认）为 dry-run——只计算并报告，不写库。
                使用者可以先看「会分出多少线程、多少弱关联」再决定是否落库。
        """
        stats = ThreadStats(dry_run=not apply)
        self._anchors = {}

        nodes = self._load_nodes()
        stats.messages = len(nodes)
        if not nodes:
            return stats

        # 按时间顺序处理：父级先于子级，循环引用天然被打破
        nodes.sort(key=lambda n: (n.received_at or "", n.row_id))

        by_message_id: dict[str, _Node] = {}
        for node in nodes:
            if node.message_id:
                # 同 ID 多份副本时保留最早一条作为图节点
                by_message_id.setdefault(node.message_id, node)

        root_of: dict[int, str] = {}

        for node in nodes:
            root_key, strength, cycle = self._resolve(node, by_message_id, root_of)
            if cycle:
                stats.cycles_broken += 1
            if not node.message_id:
                stats.orphaned_by_missing_id += 1

            thread = self._anchors.get(root_key)
            if thread is None:
                thread = _Thread(root_key=root_key)
                self._anchors[root_key] = thread

            thread.nodes.append(node)
            # 已有强关联时不降级为弱关联
            if strength == "strong":
                thread.strength = "strong"
            root_of[node.row_id] = root_key

        # 第二轮：把「完全没有引用线索」的孤立邮件按主题做弱关联
        stats.merged_by_subject = self._merge_by_subject(stats)

        # 幽灵判定必须在所有节点收集完之后：只有此时才知道
        # 「这个锚点键是否真有一封邮件以它为 Message-ID」。
        # 在创建时判定是不准的——当时后续邮件尚未处理。
        known_ids = {n.message_id for n in nodes if n.message_id}
        for thread in self._anchors.values():
            thread.is_ghost = (
                not thread.root_key.startswith(SYNTHETIC_KEY_PREFIX)
                and thread.root_key not in known_ids
            )

        stats.threads = len(self._anchors)
        for thread in self._anchors.values():
            if thread.strength == "strong":
                stats.strong += 1
            else:
                stats.weak += 1
        stats.ghosts = sum(1 for t in self._anchors.values() if t.is_ghost)

        if not apply:
            return stats

        self._persist(stats)
        return stats

    # ── 图构建 ────────────────────────────────────────────

    def _resolve(
        self,
        node: _Node,
        by_message_id: dict[str, _Node],
        root_of: dict[int, str],
    ) -> tuple[str, str, bool]:
        """决定一封邮件归属哪个线程。

        返回 ``(线程根键, 关联强度, 是否打破循环)``。
        """
        # 1) 自己就是已知锚点（可能是「幽灵变真身」）→ 归入该线程
        if node.message_id and node.message_id in self._anchors:
            return node.message_id, "strong", False

        # 2) In-Reply-To 命中已知邮件 → 挂到它所属线程
        if node.in_reply_to:
            parent_root = self._root_for_reference(
                node.in_reply_to, by_message_id, root_of
            )
            if parent_root is not None:
                return parent_root, "strong", False

        # 3) 遍历 References，取最深（最接近本封）的已知祖先
        parent_root, cycle = self._deepest_known_ancestor(
            node.references, by_message_id, root_of
        )
        if parent_root is not None:
            return parent_root, "strong", cycle

        # 4) 没有已知祖先 → 新线程，锚点用自身 ID（或合成键）
        return node.anchor_key, "strong", False

    def _root_for_reference(
        self,
        reference: str,
        by_message_id: dict[str, _Node],
        root_of: dict[int, str],
    ) -> str | None:
        """引用键 → 它所属线程的根键。

        ``reference`` 可能指向：
        * 库中已有的邮件 → 返回它所在线程的根
        * 尚未入库的邮件，但已有线程锚定它（幽灵）→ 返回该锚点键
        * 完全未知 → ``None``
        """
        parent = by_message_id.get(reference)
        if parent is not None:
            return root_of.get(parent.row_id)

        # 幽灵：已有线程锚定在这个缺失的 Message-ID 上
        if reference in self._anchors:
            return reference

        # 未入库且无锚点 → 建立一个幽灵锚点
        thread = _Thread(root_key=reference, is_ghost=True)
        thread.strength = "strong"  # 引用关系本身是强线索
        self._anchors[reference] = thread
        return reference

    def _deepest_known_ancestor(
        self,
        references: list[str],
        by_message_id: dict[str, _Node],
        root_of: dict[int, str],
    ) -> tuple[str | None, bool]:
        """遍历 References 链，取最接近本封的已知祖先。

        notmuch 的做法：References 里的每个引用都可能在库中，取**深度最大**
        者能保证挂到最近祖先而非线程根。链条更长的一侧代表更近的关系。

        返回 ``(根键, 是否检测到循环)``。
        """
        best_root: str | None = None
        best_depth = -1
        cycle = False

        for index, reference in enumerate(references):
            parent = by_message_id.get(reference)
            if parent is None:
                # 未入库的祖先：已有锚点则并入（幽灵）；否则不动，
                # 避免为每一个未知引用都凭空造线程
                if reference in self._anchors:
                    # 靠后的引用更接近本封 → 用下标做深度近似
                    if index > best_depth:
                        best_root, best_depth = reference, index
                continue

            depth = self._ancestor_depth(parent, by_message_id, set())
            if depth < 0:
                cycle = True
                continue
            if depth > best_depth:
                best_root, best_depth = root_of.get(parent.row_id), depth

        return best_root, cycle

    def _ancestor_depth(
        self,
        node: _Node,
        by_message_id: dict[str, _Node],
        seen: set[int],
    ) -> int:
        """节点到线程根的深度；检测到环返回 -1。"""
        if node.row_id in seen:
            return -1
        seen = seen | {node.row_id}

        candidates: list[str] = []
        if node.in_reply_to:
            candidates.append(node.in_reply_to)
        candidates.extend(reversed(node.references))

        for reference in candidates:
            parent = by_message_id.get(reference)
            if parent is None:
                continue
            sub = self._ancestor_depth(parent, by_message_id, seen)
            return -1 if sub < 0 else sub + 1

        return 0

    # ── 弱关联（仅主题）────────────────────────────────────

    def _merge_by_subject(self, stats: ThreadStats) -> int:
        """把「没有任何引用线索」的孤立邮件按规范化主题做弱关联。

        只在**完全孤立**（单封、无 in_reply_to、无 references、且非自动化通知）
        的邮件间生效。已有引用线索的线程不参与——那会破坏已有的强关联。

        #### 为什么必须排除自动化通知

        实测中发现：同主题弱关联会把「阿里云域名到期提醒」这类周期性通知
        合成一个 5 封的「线程」。但它们**不是对话**——没有任何人在其中交流，
        只是同一发件人的定期推送。

        把它当线程的后果不是中性的：下游的「等回复」判断会说
        「这个线程有 5 封邮件在等你回」，而实际上无人可回。这比不合并更糟。

        因此自动化通知（``Auto-Submitted`` 标记，或主题里含通知类关键词）
        一律不参与主题合并——它们各自独立成线程，如实反映「这是一条推送」。

        合并结果标 ``weak``：主题相同**不足以**断定是同一对话
        （不同人用同一个主题名很常见），因此**不得作为事实呈现**。
        """
        def is_automated(node: _Node) -> bool:
            if node.auto_submitted:
                return True
            return any(kw in node.subject_norm for kw in _AUTOMATED_SUBJECT_HINTS)

        isolated = [
            thread
            for thread in self._anchors.values()
            if len(thread.nodes) == 1
            and not thread.is_ghost
            and not thread.nodes[0].in_reply_to
            and not thread.nodes[0].references
            and thread.nodes[0].subject_norm
            and not is_automated(thread.nodes[0])
        ]
        if len(isolated) < 2:
            return 0

        groups: dict[str, list[_Thread]] = {}
        for thread in isolated:
            groups.setdefault(thread.nodes[0].subject_norm, []).append(thread)

        merged = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            keeper = members[0]
            for loser in members[1:]:
                keeper.nodes.extend(loser.nodes)
                merged += len(loser.nodes)
                del self._anchors[loser.root_key]
            keeper.nodes.sort(key=lambda n: (n.received_at or "", n.row_id))
            keeper.strength = "weak"

        return merged

    # ── 持久化 ────────────────────────────────────────────

    def _persist(self, stats: ThreadStats) -> None:
        """写入 ``threads`` 并回填 ``messages.thread_id``。

        全量重建：先清空本账号的线程关联再重建，保证结果确定、不随运行漂移。
        """
        now = utcnow_iso()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE messages SET thread_id = NULL WHERE account = ?",
                (self._account,),
            )
            self._conn.execute("DELETE FROM threads WHERE account = ?", (self._account,))

            for thread in self._anchors.values():
                nodes = thread.nodes
                if not nodes:
                    continue
                subject_norm = nodes[0].subject_norm
                last = max(nodes, key=lambda n: (n.received_at or "", n.row_id))

                cur = self._conn.execute(
                    """
                    INSERT INTO threads (
                        account, root_message_id, subject_norm, last_direction,
                        last_message_at, link_strength, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._account,
                        thread.root_key,
                        subject_norm,
                        self._direction_of(last),
                        last.received_at,
                        thread.strength,
                        "ghost" if thread.is_ghost else "active",
                        now,
                        now,
                    ),
                )
                thread_id = int(cur.lastrowid)
                placeholders = ",".join("?" for _ in nodes)
                self._conn.execute(
                    f"UPDATE messages SET thread_id = ? WHERE id IN ({placeholders})",
                    (thread_id, *[n.row_id for n in nodes]),
                )
        finally:
            self._conn.execute("COMMIT")

    def _direction_of(self, node: _Node) -> str:
        """判断该邮件是用户发出还是外部发来。

        靠 ``from_addr`` 是否属于用户自己的地址集合。

        **未配置用户地址时一律返回 ``in``**：宁可把方向判成「收信」，
        也不要凭空声称用户发过信——后者会让「等回复」类判断全部出错。
        """
        addr = (node.from_addr or "").lower()
        if self._user_addresses and addr in self._user_addresses:
            return "out"
        return "in"

    # ── 加载 ──────────────────────────────────────────────

    def _load_nodes(self) -> list[_Node]:
        """加载参与建图的邮件。

        只取**规范记录**（``is_canonical=1``）且未标 ``stale``——
        副本与失效记录参与建图只会制造重复线程。
        """
        rows = self._conn.execute(
            """
            SELECT id, normalized_message_id, in_reply_to, references_ids,
                   subject, received_at, from_addr, body_sha256, auto_submitted
              FROM messages
             WHERE account = ? AND is_canonical = 1 AND stale = 0
             ORDER BY id
            """,
            (self._account,),
        ).fetchall()

        nodes: list[_Node] = []
        for row in rows:
            references = [r for r in (row["references_ids"] or "").split() if r]
            digest = row["body_sha256"] or f"row{row['id']}"
            nodes.append(
                _Node(
                    row_id=int(row["id"]),
                    message_id=row["normalized_message_id"],
                    in_reply_to=row["in_reply_to"],
                    references=references,
                    subject_norm=normalize_title(row["subject"]),
                    received_at=row["received_at"],
                    from_addr=row["from_addr"] or "",
                    synthetic_key=f"{SYNTHETIC_KEY_PREFIX}{digest[:16]}",
                    auto_submitted=row["auto_submitted"],
                )
            )
        return nodes
