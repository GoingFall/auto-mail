"""主流程编排：把 sync → extract → push [→ digest] 串成一次运行。

这是 ``automail run`` 的实现，供 Windows 任务计划调用。

## 关键设计：阶段之间互不阻塞

每个阶段独立成败。**同步失败不该阻止抽取处理本地已有的邮件**；抽取失败
不该阻止推送已批准的事件。理由很实际：

* 邮箱服务端风控断开 → 同步失败，但本地已有 89 封没抽取，不抽取纯属浪费
* LLM 不可用 → 抽取降级，但已批准的事件照样该写入日历
* 日历 API 限流 → 推送失败，但下次运行会重试（``push_attempts`` 已记录）

因此单个阶段失败只记录、不中断后续。整体退出码取**最严重**的那个阶段。

## 单实例锁

两个计划任务重叠运行会：

* 同时同步同一邮箱 → 触发风控，且可能重复入库
* 同时推送同一事件 → 可能产生重复日历事件

因此用 ``locks`` 表做 TTL 抢占式互斥。拿不到锁时**不是错误**——那说明
上一次运行还在进行，属于正常情况，因此退出码为「部分缺失」而非「致命」。

## 为什么不用进程锁文件

``locks`` 表与数据库同事务，具备 TTL 抢占能力：崩溃留下的锁会在 TTL 后
自动失效，不需要人工清理。文件锁做不到这点（残留文件会永久阻塞）。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .db import LockRepository, utcnow_iso
from .exits import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from .extract.runner import ExtractRunner, ExtractStats
from .mail.backend import MailAuthError, MailError, UnsafeLoginError
from .mail.imap_backend import ImapBackend
from .mail.sync import SyncEngine, SyncStats
from .progress import ProgressReporter
from .push import PushEngine, PushStats
from .read_state import MarkReadStats
from .sanitize import sanitize_error
from .settings import Settings

logger = logging.getLogger("automail.pipeline")

#: 单实例锁的名字。``doctor`` 也用它报告「是否有运行在进行中」。
RUN_LOCK = "automail.run"

#: 各阶段在整体退出码里的严重度（数值越大越严重）
_SEVERITY_ORDER = (EXIT_OK, EXIT_PARTIAL, EXIT_FATAL)


@dataclass(slots=True)
class StageResult:
    """单个阶段的结果。"""

    name: str
    exit_code: int = EXIT_OK
    skipped: bool = False
    skip_reason: str = ""
    error: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK and not self.skipped

    @property
    def label(self) -> str:
        if self.skipped:
            return "跳过"
        return {EXIT_OK: "成功", EXIT_PARTIAL: "部分完成", EXIT_FATAL: "失败"}.get(
            self.exit_code, "未知"
        )


@dataclass(slots=True)
class PipelineStats:
    """一次完整运行的结果。"""

    run_id: str = ""
    apply: bool = False
    lock_acquired: bool = True
    lock_conflict: str | None = None
    lock_expires_at: str | None = None
    stages: list[StageResult] = field(default_factory=list)
    started_at: str = ""
    ended_at: str | None = None

    @property
    def exit_code(self) -> int:
        """取最严重阶段的退出码。"""
        if not self.lock_acquired:
            return EXIT_PARTIAL
        worst = EXIT_OK
        for stage in self.stages:
            if _SEVERITY_ORDER.index(stage.exit_code) > _SEVERITY_ORDER.index(worst):
                worst = stage.exit_code
        return worst

    def stage(self, name: str) -> StageResult | None:
        for item in self.stages:
            if item.name == name:
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "apply": self.apply,
            "lock_acquired": self.lock_acquired,
            "lock_conflict": self.lock_conflict,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "stages": [
                {
                    "name": s.name,
                    "exit_code": s.exit_code,
                    "skipped": s.skipped,
                    "skip_reason": s.skip_reason,
                    "error": s.error,
                    **s.stats,
                }
                for s in self.stages
            ],
        }


class Pipeline:
    """串起各阶段的主流程。"""

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        *,
        run_id: str = "",
        reporter: ProgressReporter | None = None,
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._run_id = run_id
        self._owner = run_id or utcnow_iso()
        # 进度上报。默认 None → 全部空操作，行为与引入前完全一致。
        self._reporter = reporter or ProgressReporter()

    # ── 锁 ────────────────────────────────────────────────

    def acquire_lock(self, *, ttl_seconds: int | None = None) -> tuple[bool, str]:
        """获取单实例锁，返回 ``(是否获得, 说明)``。"""
        ttl = ttl_seconds or self._settings.run_lock_ttl_seconds
        locks = LockRepository(self._conn)
        # 顺手清理过期锁，避免残留影响判断
        locks.purge_expired()

        if locks.acquire(RUN_LOCK, self._owner, ttl_seconds=ttl):
            row = locks.peek(RUN_LOCK)
            return True, (row["expires_at"] if row else "")

        holder = locks.peek(RUN_LOCK)
        holder_id = holder["owner_run_id"] if holder else "未知"
        expires = holder["expires_at"] if holder else "未知"
        return False, (
            f"已有运行在进行中（run_id={holder_id[:8]}，锁到期 {expires}）。"
            "这是正常的重叠调度，本轮跳过。"
        )

    def release_lock(self) -> None:
        LockRepository(self._conn).release(RUN_LOCK, self._owner)

    # ── 各阶段 ────────────────────────────────────────────

    def stage_sync(self, *, apply: bool, limit: int | None = None) -> StageResult:
        """同步邮件。缺凭据时**跳过而非失败**。"""
        settings = self._settings
        if not settings.imap_user.strip() or not settings.imap_auth_code_value:
            return StageResult(
                name="sync",
                skipped=True,
                skip_reason="未配置 163 凭据（IMAP_USER / IMAP_AUTH_CODE）",
                exit_code=EXIT_PARTIAL,
            )

        try:
            with ImapBackend(settings) as backend:
                engine = SyncEngine(
                    settings, self._conn, backend, reporter=self._reporter
                )
                result: SyncStats = engine.sync(apply=apply, limit=limit)
        except MailAuthError as exc:
            return StageResult(
                name="sync",
                exit_code=EXIT_FATAL,
                error=sanitize_error(exc),
            )
        except UnsafeLoginError as exc:
            # 被服务端限流：不是凭据问题，因此不算致命（下次可成功）
            return StageResult(
                name="sync",
                exit_code=EXIT_PARTIAL,
                error=sanitize_error(exc),
            )
        except (MailError, OSError) as exc:
            # 网络/服务端问题：下次运行会重试
            return StageResult(
                name="sync",
                exit_code=EXIT_PARTIAL,
                error=sanitize_error(exc),
            )

        # 文件夹级失败（风控、单封取回失败）不算致命，但必须反映在退出码里——
        # 否则「89 封里 69 封失败」会被报成完全成功。
        return StageResult(
            name="sync",
            exit_code=EXIT_PARTIAL if result.has_errors else EXIT_OK,
            stats=result.as_dict(),
        )

    def stage_extract(
        self, *, apply: bool, limit: int | None = None, llm: object | None = None
    ) -> StageResult:
        """抽取事件候选。LLM 不可用时降级，不视为失败。"""
        try:
            runner = ExtractRunner(
                self._settings, self._conn, llm=llm, reporter=self._reporter
            )  # type: ignore[arg-type]
            result: ExtractStats = runner.run(apply=apply, limit=limit)
        except Exception as exc:  # noqa: BLE001 - 抽取失败不该阻断推送
            return StageResult(
                name="extract",
                exit_code=EXIT_PARTIAL,
                error=sanitize_error(exc),
            )

        # LLM 降级是「配置缺失」而非故障 → 记为部分完成
        partial = result.llm_unavailable or result.failed > 0
        return StageResult(
            name="extract",
            exit_code=EXIT_PARTIAL if partial else EXIT_OK,
            stats=result.as_dict(),
        )

    def stage_push(self, *, apply: bool, calendar: object) -> StageResult:
        """推送已批准事件，并处理到点的延迟窗口项。"""
        try:
            engine = PushEngine(self._settings, self._conn, calendar)  # type: ignore[arg-type]
            approved: PushStats = engine.push_approved(apply=apply)
            due: PushStats = engine.dispatch_due(apply=apply)
        except Exception as exc:  # noqa: BLE001 - 日历问题不该让整体致命
            return StageResult(
                name="push",
                exit_code=EXIT_PARTIAL,
                error=sanitize_error(exc),
            )

        merged = _merge_push_stats(approved, due)
        # 冻结意味着「检测到用户手改，已停止写入」——这是**正确行为**，
        # 但需要人处理，因此记为部分完成而不是成功
        partial = bool(merged["frozen"] or merged["failed"])
        return StageResult(
            name="push",
            exit_code=EXIT_PARTIAL if partial else EXIT_OK,
            stats=merged,
        )

    def stage_digest(self, *, calendar: object | None = None) -> StageResult:
        """生成摘要（写本地文件，不发邮件）。"""
        from .digest import DigestBuilder

        try:
            builder = DigestBuilder(self._settings, self._conn, calendar=calendar)
            path, content = builder.build()
        except Exception as exc:  # noqa: BLE001
            return StageResult(
                name="digest",
                exit_code=EXIT_PARTIAL,
                error=sanitize_error(exc),
            )
        return StageResult(
            name="digest",
            stats={"path": str(path), "bytes": len(content.encode("utf-8"))},
        )

    def stage_mark_read(self, *, apply: bool, limit: int | None = None) -> StageResult:
        """把处理完的邮件标为已读（**本项目唯一的邮箱写操作**）。

        必须放在 extract 之后：判定「处理完」依赖抽取结果与事件状态，
        在抽取之前跑会用上一轮的旧状态做决定。

        未开启（``MARK_READ_POLICY=off``，默认）时**明确报告跳过**，
        而不是静默什么都不做——否则使用者会以为功能坏了。
        """
        settings = self._settings
        if not settings.mark_read_enabled:
            return StageResult(
                name="mark-read",
                skipped=True,
                skip_reason="未开启（MARK_READ_POLICY=off，默认关闭）",
            )
        if not settings.imap_user.strip() or not settings.imap_auth_code_value:
            return StageResult(
                name="mark-read",
                skipped=True,
                skip_reason="未配置 163 凭据（IMAP_USER / IMAP_AUTH_CODE）",
                exit_code=EXIT_PARTIAL,
            )

        from .read_state import mark_read

        try:
            with ImapBackend(settings) as backend:
                result: MarkReadStats = mark_read(
                    settings, self._conn, backend, apply=apply, limit=limit
                )
        except (MailError, OSError) as exc:
            # 邮箱问题不该让整轮致命；下次运行会重试
            return StageResult(
                name="mark-read",
                exit_code=EXIT_PARTIAL,
                error=sanitize_error(exc),
            )

        return StageResult(
            name="mark-read",
            exit_code=EXIT_PARTIAL if result.has_errors else EXIT_OK,
            stats=result.as_dict(),
        )

    # ── 主入口 ────────────────────────────────────────────

    def run(
        self,
        *,
        apply: bool = False,
        limit: int | None = None,
        with_digest: bool = False,
        llm: object | None = None,
        calendar: object | None = None,
        skip_sync: bool = False,
        skip_extract: bool = False,
        skip_push: bool = False,
    ) -> PipelineStats:
        """执行完整流程。

        Args:
            apply: ``False``（默认）为 dry-run，所有阶段只报告不写入。
            limit: 传给同步阶段的本轮上限。
            with_digest: 是否在最后生成摘要。
            skip_sync / skip_extract / skip_push: 跳过指定阶段
                （用于「只重试推送」这类场景，避免无谓的邮箱往返）。
        """
        stats = PipelineStats(
            run_id=self._run_id, apply=apply, started_at=utcnow_iso()
        )

        # ── 锁 ──
        acquired, detail = self.acquire_lock()
        stats.lock_acquired = acquired
        if not acquired:
            stats.lock_conflict = detail
            stats.ended_at = utcnow_iso()
            logger.warning("未获取到运行锁：%s", detail)
            return stats
        stats.lock_expires_at = detail

        try:
            # 阶段总数用于「第几/共几个」的显示。mark-read 恒被调用（内部可能
            # 自己报跳过），因此它始终计入。**顺序只在这里定义一次**——
            # 图形界面只接收通知，不自己拼阶段序列，否则两处编排迟早分叉。
            planned = 4 + (1 if with_digest else 0)
            self._reporter.stage("sync", 1, planned)
            if skip_sync:
                stats.stages.append(
                    StageResult(name="sync", skipped=True, skip_reason="按要求跳过")
                )
            else:
                stats.stages.append(self.stage_sync(apply=apply, limit=limit))

            self._reporter.stage("extract", 2, planned)
            if skip_extract:
                stats.stages.append(
                    StageResult(name="extract", skipped=True, skip_reason="按要求跳过")
                )
            else:
                stats.stages.append(
                    self.stage_extract(apply=apply, limit=limit, llm=llm)
                )

            self._reporter.stage("push", 3, planned)
            if skip_push:
                stats.stages.append(
                    StageResult(name="push", skipped=True, skip_reason="按要求跳过")
                )
            else:
                stats.stages.append(self.stage_push(apply=apply, calendar=calendar))

            # 已读回写放在最后：它依赖前三阶段的结果（尤其是抽取与推送后
            # 的事件状态）。未开启时该阶段会明确报告跳过。
            self._reporter.stage("mark-read", 4, planned)
            stats.stages.append(self.stage_mark_read(apply=apply, limit=limit))

            if with_digest:
                self._reporter.stage("digest", 5, planned)
                stats.stages.append(self.stage_digest(calendar=calendar))
        finally:
            self.release_lock()
            stats.ended_at = utcnow_iso()

        return stats


def _merge_push_stats(approved: PushStats, due: PushStats) -> dict[str, Any]:
    """把「已批准推送」与「到点窗口推送」两份统计合并。

    两者作用于同一批事件（approved 状态），因此计数相加是合理的；
    ``limit_hit`` 取最大值避免重复计算（两侧都受同一上限约束）。
    """
    merged = approved.as_dict()
    for key in (
        "considered", "created", "updated", "backfilled",
        "skipped", "failed", "frozen", "archived",
    ):
        merged[key] = int(merged.get(key, 0)) + int(due.as_dict().get(key, 0))
    merged["limit_hit"] = max(approved.limit_hit, due.limit_hit)
    errors = list(approved.errors) + list(due.errors)
    merged["errors"] = errors[:10]
    merged.pop("dry_run", None)
    return merged


def default_lock_ttl(settings: Settings) -> int:
    """给测试与文档用的默认锁 TTL。"""
    return settings.run_lock_ttl_seconds
