"""数据模型与状态枚举。

本模块是规格的「单一事实来源」：所有状态字符串与其合法取值都定义在这里，
数据库层的 CHECK 约束、CLI 的过滤参数、状态机的转移规则都引用这些枚举，
避免出现同一状态在不同模块有不同拼写的漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum  # Python 3.11+
from typing import Any

# ──────────────────────────────────────────────────────────────
# 事件状态机（规格 §4）
# ──────────────────────────────────────────────────────────────

class EventStatus(StrEnum):
    """事件状态。``events.status`` 是事件的唯一状态源。

    ``scheduled_pushes`` 表只做调度队列，不参与本状态机。
    """

    PENDING = "pending"
    """抽取得到候选，等待人工批准。"""

    APPROVED = "approved"
    """已批准，等待推送（自动白名单事件也会先落到 approved）。"""

    PUSHED = "pushed"
    """已成功写入 Google 日历。"""

    PUSH_FAILED = "push_failed"
    """推送失败，按退避重试；超 PUSH_MAX_ATTEMPTS 后置 needs_attention。"""

    UNCERTAIN = "uncertain"
    """反查/list 本身报错，无法判定 Google 侧是否已存在该事件；下轮复核。"""

    EXTERNALLY_MODIFIED = "externally_modified"
    """远端内容与非我方快照不一致且本地无改动 → 疑似用户手改，冻结更新与删除。"""

    CONFLICT = "conflict"
    """远端与本地都相对快照有改动 → 拒绝一切写入，需 adopt 或 reject 裁决。"""

    MISSING = "missing"
    """Google 侧已不存在该事件（404）。去向由 NOT_FOUND_POLICY 决定。"""

    REJECTED = "rejected"
    """终态：人工否决，不再提示。"""

    IGNORED = "ignored"
    """终态：忽略，同 fingerprint 不再入队。"""

    CANCELLED = "cancelled"
    """归档式取消：事件在 Google 侧被标记为 cancelled 而非硬删除。"""

    SUPERSEDED = "superseded"
    """终态：被同 ics_uid 的更高 SEQUENCE 更新取代。"""

    @classmethod
    def terminal(cls) -> frozenset[EventStatus]:
        return frozenset({cls.REJECTED, cls.IGNORED, cls.SUPERSEDED})

    @classmethod
    def frozen(cls) -> frozenset[EventStatus]:
        """冻结态：禁止对其发起 update/delete。"""
        return frozenset({cls.EXTERNALLY_MODIFIED, cls.CONFLICT})


class EventSource(StrEnum):
    """事件抽取来源。合并冲突时优先级 ICS > RULES > LLM。"""

    ICS = "ics"
    RULES = "rules"
    LLM = "llm"


class ScheduledPushState(StrEnum):
    """``scheduled_pushes`` 表的调度状态（不是事件状态）。"""

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    CANCELLED = "cancelled"
    FAILED = "failed"


# ──────────────────────────────────────────────────────────────
# 抽取与同步状态
# ──────────────────────────────────────────────────────────────

class ExtractStatus(StrEnum):
    """``messages.extract_status``：extract 层的单一状态位。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ProcessedMailStatus(StrEnum):
    """``processed_mail.status``：只管 sync 层（抓取/清洗入库）。"""

    SYNCED = "synced"
    FETCH_FAILED = "fetch_failed"


class NotFoundPolicy(StrEnum):
    """Google 侧事件消失（404）时的策略。"""

    PENDING = "pending"
    RECREATE = "recreate"
    FAIL = "fail"


class AmbiguousDatePolicy(StrEnum):
    """同一文本内多个候选日期的取舍。无年份日期不受此影响（硬门恒 pending）。"""

    PENDING = "pending"
    EARLIEST = "earliest"


class TodoStatus(StrEnum):
    """``todos.status``（v2）。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReadinessStatus(StrEnum):
    """doctor 单项检查结果。"""

    OK = "ok"
    MISSING = "missing"
    """缺少配置/凭据，不判整体失败。"""
    SKIPPED = "skipped"
    """本轮未执行的检查（例如离线模式下跳过联网项）。"""
    ERROR = "error"
    """检查失败且属于致命问题。"""


# ──────────────────────────────────────────────────────────────
# 轻量记录（dataclass）
# ──────────────────────────────────────────────────────────────
#
# 说明：这些 dataclass 故意保持「薄」，只承载跨模块传递的字段。
# 真正的持久化约束以 migrations/*.sql 的 CHECK 为准，两者需保持一致。


@dataclass(slots=True)
class ReadinessItem:
    """doctor 的单项检查结果。"""

    name: str
    status: ReadinessStatus
    detail: str = ""
    fatal: bool = False
    """即使 status=ERROR，也只有在 fatal=True 时整体退出码才升到 2。"""


@dataclass(slots=True)
class RunRecord:
    """``runs`` 表的一行。"""

    run_id: str
    command: str
    started_at: str
    ended_at: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    ok: bool | None = None
    error: str | None = None
    exit_code: int | None = None


@dataclass(slots=True)
class SyncState:
    """``sync_state`` 表的一行（每个 account+folder 一条）。"""

    account: str
    folder: str
    uid_validity: int
    highest_uid: int
    syncs_since_full: int = 0
    last_sync_at: str | None = None
    last_full_sync_at: str | None = None


@dataclass(slots=True)
class ExtractCandidate:
    """抽取层产出的候选事件（尚未落库）。

    ``evidence`` 恒存抽取依据的原文片段，供审核界面展示；
    ``start_ts``/``end_ts`` 为 ISO8601 字符串（UTC 或带偏移）。
    """

    title: str
    start_ts: str | None
    end_ts: str | None
    all_day: bool
    source: EventSource
    confidence: float
    evidence: str = ""
    tz: str | None = None
    location: str | None = None
    organizer: str | None = None
    ics_uid: str | None = None
    ics_sequence: int | None = None
    ics_recurrence_id: str | None = None
    ics_rrule: str | None = None
    fingerprint: str = ""
    requires_review: bool = False
    review_reason: str = ""
