"""P6 测试：主流程编排与单实例锁。

**核心承诺**：``run`` 把 sync → extract → push 串成一次运行，且

* 阶段之间互不阻塞（同步失败仍抽取本地邮件；抽取失败仍推送已批准事件）
* 单实例锁防止计划任务重叠
* 崩溃留下的锁会自动失效，不需要人工清理
* 退出码反映**最严重**的阶段，且「拿不到锁」不是错误
* dry-run 全程零副作用
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from automail.calendar.backend import CalendarError
from automail.calendar.fake import FakeCalendar
from automail.db import LockRepository, iso, utcnow
from automail.exits import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL
from automail.mail.backend import (
    MailAuthError,
    MailConnectionError,
)
from automail.pipeline import RUN_LOCK, Pipeline, _merge_push_stats
from automail.push import PushStats
from automail.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        account="163",
        imap_user="",
        imap_auth_code="",
        data_dir=tmp_path / "data",
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "logs",
        run_lock_ttl_seconds=1800,
    )


def _insert_event(conn: sqlite3.Connection, **overrides: object) -> int:
    values: dict[str, object] = {
        "message_id": None,
        "title": "测试事件",
        "start_ts": "2026-10-01T02:00:00Z",
        "end_ts": "2026-10-01T03:00:00Z",
        "all_day": 0,
        "source": "rules",
        "confidence": 0.95,
        "fingerprint": "fp-test",
        "status": "approved",
        "needs_attention": 0,
        "created_at": iso(utcnow()),
        "updated_at": iso(utcnow()),
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cur = conn.execute(
        f"INSERT INTO events ({columns}) VALUES ({placeholders})", list(values.values())
    )
    return int(cur.lastrowid)


# ──────────────────────────────────────────────────────────────
# 单实例锁
# ──────────────────────────────────────────────────────────────


def test_lock_prevents_concurrent_runs(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """两个运行重叠时，第二个必须被拒绝且不执行任何阶段。

    重叠会同时同步同一邮箱（触发风控 + 可能重复入库）、
    同时推送同一事件（可能产生重复日历事件）。
    """
    first = Pipeline(settings, conn, run_id="run-a")
    acquired, _ = first.acquire_lock()
    assert acquired is True

    # 用另一个连接模拟另一个进程
    from automail.db import connect

    other = connect(settings.db_path)
    try:
        second = Pipeline(settings, other, run_id="run-b")
        allowed, reason = second.acquire_lock()
        assert allowed is False
        assert "run-a" in reason
        assert "重叠" in reason or "进行中" in reason
    finally:
        other.close()

    first.release_lock()


def test_rejected_run_skips_all_stages(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """被锁拒绝时不执行任何阶段，退出码为「部分缺失」而非「致命」。"""
    holder = Pipeline(settings, conn, run_id="run-holder")
    holder.acquire_lock()

    from automail.db import connect

    other = connect(settings.db_path)
    try:
        blocked = Pipeline(settings, other, run_id="run-blocked")
        result = blocked.run(apply=False)
    finally:
        other.close()

    assert result.lock_acquired is False
    assert result.stages == [], "被拒绝时不该执行任何阶段"
    assert result.exit_code == EXIT_PARTIAL, "重叠调度是正常情况，不是致命错误"
    holder.release_lock()


def test_expired_lock_is_taken_over(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """崩溃留下的锁会在 TTL 后自动失效——这是用数据库锁而非文件锁的理由。"""
    locks = LockRepository(conn)
    locks.acquire(RUN_LOCK, "crashed-run", ttl_seconds=1800)
    conn.execute(
        "UPDATE locks SET expires_at = ? WHERE name = ?",
        (iso(utcnow() - timedelta(seconds=1)), RUN_LOCK),
    )

    pipeline = Pipeline(settings, conn, run_id="new-run")
    acquired, _ = pipeline.acquire_lock()
    assert acquired is True, "过期锁应被自动接管"
    assert locks.peek(RUN_LOCK)["owner_run_id"] == "new-run"
    pipeline.release_lock()


def test_lock_released_after_run(settings: Settings, conn: sqlite3.Connection) -> None:
    """运行结束必须释放锁，否则下次会被自己挡住。"""
    Pipeline(settings, conn, run_id="r1").run(apply=False)
    assert LockRepository(conn).peek(RUN_LOCK) is None


def test_lock_released_even_on_failure(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """阶段抛异常时也必须释放锁（finally 保证）。"""
    pipeline = Pipeline(settings, conn, run_id="r2")

    class Exploding:
        def sync(self, **kwargs):
            raise MailConnectionError("boom")

    # 让 stage_sync 抛非捕获类型的异常，验证 finally 仍释放锁
    original = pipeline.stage_sync

    def boom(*args: object, **kwargs: object):
        raise KeyboardInterrupt("simulated interruption")

    pipeline.stage_sync = boom  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(apply=False)

    assert LockRepository(conn).peek(RUN_LOCK) is None, "异常路径也必须释放锁"
    _ = original


def test_lock_is_reentrant_for_same_owner(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """同一 owner 可重复获取（避免自身重入被挡）。"""
    pipeline = Pipeline(settings, conn, run_id="same")
    assert pipeline.acquire_lock()[0] is True
    assert pipeline.acquire_lock()[0] is True
    pipeline.release_lock()


# ──────────────────────────────────────────────────────────────
# 阶段：sync
# ──────────────────────────────────────────────────────────────


def test_sync_skipped_without_credentials(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """缺凭据时跳过同步，而不是报失败。

    这很重要：使用者可能只想在本地重跑抽取或推送，没有邮箱凭据不该阻止它。
    """
    pipeline = Pipeline(settings, conn, run_id="r")
    stage = pipeline.stage_sync(apply=False)

    assert stage.skipped is True
    assert "凭据" in stage.skip_reason
    assert stage.exit_code == EXIT_PARTIAL


def test_run_continues_to_extract_when_sync_skipped(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """同步被跳过时，抽取与推送仍照常执行。"""
    _insert_event(conn)
    pipeline = Pipeline(settings, conn, run_id="r")
    result = pipeline.run(apply=False, calendar=FakeCalendar())

    names = [s.name for s in result.stages]
    assert names == ["sync", "extract", "push", "mark-read"]
    assert result.stage("sync").skipped is True
    assert result.stage("push").skipped is False
    # 未开启已读回写 → 该阶段也应明确报告跳过，而不是静默省略
    assert result.stage("mark-read").skipped is True


def test_sync_auth_failure_is_fatal(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """认证失败是致命错误（凭据错了，继续跑没有意义）。"""
    settings.imap_user = "me@163.com"
    settings.imap_auth_code = "badcode"

    pipeline = Pipeline(settings, conn, run_id="r")

    class AuthFailBackend:
        def __enter__(self):
            raise MailAuthError("invalid credentials")

        def __exit__(self, *args: object) -> None:
            pass

    import automail.pipeline as pipeline_module

    original = pipeline_module.ImapBackend
    pipeline_module.ImapBackend = lambda s: AuthFailBackend()  # type: ignore[assignment]
    try:
        stage = pipeline.stage_sync(apply=False)
    finally:
        pipeline_module.ImapBackend = original  # type: ignore[assignment]

    assert stage.exit_code == EXIT_FATAL
    assert "credentials" in (stage.error or "")


def test_sync_network_failure_is_partial_not_fatal(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """网络/服务端问题只算部分完成——下次运行会重试。

    把它当致命错误会让计划任务反复告警，而实际只是临时故障。
    """
    settings.imap_user = "me@163.com"
    settings.imap_auth_code = "code"

    pipeline = Pipeline(settings, conn, run_id="r")

    class NetFailBackend:
        def __enter__(self):
            raise MailConnectionError("connection reset")

        def __exit__(self, *args: object) -> None:
            pass

    import automail.pipeline as pipeline_module

    original = pipeline_module.ImapBackend
    pipeline_module.ImapBackend = lambda s: NetFailBackend()  # type: ignore[assignment]
    try:
        stage = pipeline.stage_sync(apply=False)
    finally:
        pipeline_module.ImapBackend = original  # type: ignore[assignment]

    assert stage.exit_code == EXIT_PARTIAL


def test_sync_unsafe_login_is_partial_not_fatal(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """被服务端限流不算致命——不是凭据问题，下次可能成功。"""
    from automail.mail.backend import UnsafeLoginError

    settings.imap_user = "me@163.com"
    settings.imap_auth_code = "code"
    pipeline = Pipeline(settings, conn, run_id="r")

    class ThrottledBackend:
        def __enter__(self):
            raise UnsafeLoginError("throttled")

        def __exit__(self, *args: object) -> None:
            pass

    import automail.pipeline as pipeline_module

    original = pipeline_module.ImapBackend
    pipeline_module.ImapBackend = lambda s: ThrottledBackend()  # type: ignore[assignment]
    try:
        stage = pipeline.stage_sync(apply=False)
    finally:
        pipeline_module.ImapBackend = original  # type: ignore[assignment]

    assert stage.exit_code == EXIT_PARTIAL


# ──────────────────────────────────────────────────────────────
# 阶段：extract
# ──────────────────────────────────────────────────────────────


def test_extract_reports_partial_when_llm_unavailable(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """LLM 未配置是「配置缺失」而非故障，但必须反映在退出码里。"""
    conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            body_excerpt, body_sha256, extract_status, is_canonical, stale, fetched_at)
        VALUES ('163', 'INBOX', 1, 1, '到期提醒', '您的服务即将到期，请尽快处理。',
                'h1', 'pending', 1, 0, ?)
        """,
        (iso(utcnow()),),
    )
    from automail.extract.llm import LlmExtractor

    pipeline = Pipeline(settings, conn, run_id="r")
    stage = pipeline.stage_extract(
        apply=True, llm=LlmExtractor(base_url="", api_key="", model="m")
    )

    assert stage.exit_code == EXIT_PARTIAL
    assert stage.stats.get("llm_unavailable") is True
    assert stage.stats.get("llm_skipped", 0) >= 1


def test_extract_failure_does_not_block_push(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """抽取失败不该阻止推送已批准事件。"""
    _insert_event(conn)
    pipeline = Pipeline(settings, conn, run_id="r")

    original = pipeline.stage_extract

    def boom(**kwargs: object):
        from automail.pipeline import StageResult

        return StageResult(
            name="extract", exit_code=EXIT_PARTIAL, error="simulated extract failure"
        )

    pipeline.stage_extract = boom  # type: ignore[method-assign]
    cal = FakeCalendar()
    result = pipeline.run(apply=True, calendar=cal)

    assert result.stage("extract").exit_code == EXIT_PARTIAL
    # 推送仍执行了
    assert result.stage("push").exit_code in {EXIT_OK, EXIT_PARTIAL}
    _ = original


# ──────────────────────────────────────────────────────────────
# 阶段：push
# ──────────────────────────────────────────────────────────────


def test_push_creates_event(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    _insert_event(conn)
    cal = FakeCalendar()
    pipeline = Pipeline(settings, conn, run_id="r")
    stage = pipeline.stage_push(apply=True, calendar=cal)

    assert stage.stats["created"] == 1
    assert len(cal.events) == 1


def test_push_frozen_event_yields_partial(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """检测到用户手改而冻结时，退出码应是部分完成。

    冻结是**正确行为**（保护了用户的修改），但需要人处理，因此不能报成功
    而无提示——那会让使用者以为一切正常。
    """
    cal = FakeCalendar()
    _insert_event(conn, fingerprint="fp-frozen")
    pipeline = Pipeline(settings, conn, run_id="r")

    pipeline.stage_push(apply=True, calendar=cal)
    gcal_id = conn.execute("SELECT gcal_event_id FROM events").fetchone()[0]
    cal.simulate_user_edit(gcal_id, summary="用户改的")
    conn.execute("UPDATE events SET status='approved'")

    stage = pipeline.stage_push(apply=True, calendar=cal)
    assert stage.exit_code == EXIT_PARTIAL
    assert stage.stats["frozen"] == 1
    # 用户的修改必须保留
    assert cal.get_event(gcal_id).payload["summary"] == "用户改的"


def test_push_failure_is_partial_not_fatal(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """日历 API 失败不算致命——下次运行会重试（push_attempts 已记录）。"""
    _insert_event(conn)
    cal = FakeCalendar()
    cal.fail_insert = True

    pipeline = Pipeline(settings, conn, run_id="r")
    stage = pipeline.stage_push(apply=True, calendar=cal)

    assert stage.exit_code == EXIT_PARTIAL
    assert stage.stats["failed"] == 1


def test_push_calendar_exception_does_not_break_pipeline(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """日历抛异常时整体流程仍应完成（其余阶段结果保留）。"""
    class BrokenCalendar:
        def list_events(self, **kwargs: object):
            raise CalendarError("calendar down")

        def __getattr__(self, name: str):
            def boom(*args: object, **kwargs: object):
                raise CalendarError("calendar down")

            return boom

    _insert_event(conn)
    pipeline = Pipeline(settings, conn, run_id="r")
    result = pipeline.run(apply=True, calendar=BrokenCalendar())

    assert result.stage("push").exit_code == EXIT_PARTIAL
    assert result.exit_code == EXIT_PARTIAL


# ──────────────────────────────────────────────────────────────
# 阶段：digest
# ──────────────────────────────────────────────────────────────


def test_digest_stage_writes_file(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    pipeline = Pipeline(settings, conn, run_id="r")
    stage = pipeline.stage_digest(calendar=None)

    assert stage.exit_code == EXIT_OK
    assert Path(stage.stats["path"]).exists()


def test_run_with_digest_includes_all_stages(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """阶段顺序固定：sync → extract → push → mark-read → digest。

    ``mark-read`` 必须在 extract/push **之后**——判定「处理完」依赖抽取结果
    与事件状态，跑在前面会用上一轮的旧状态做决定；摘要放最后，因为它要反映
    本轮全部结果。
    """
    pipeline = Pipeline(settings, conn, run_id="r")
    result = pipeline.run(apply=False, with_digest=True, calendar=FakeCalendar())

    assert [s.name for s in result.stages] == [
        "sync",
        "extract",
        "push",
        "mark-read",
        "digest",
    ]


def test_mark_read_stage_reports_skip_when_disabled(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """默认关闭时该阶段要**明确报告跳过**，而不是静默什么都不做。

    静默跳过会被误以为功能坏了；明确说「未开启」才可诊断。
    """
    assert settings.mark_read_policy == "off"
    pipeline = Pipeline(settings, conn, run_id="r")
    stage = pipeline.stage_mark_read(apply=False)

    assert stage.skipped is True
    assert "MARK_READ_POLICY" in (stage.skip_reason or "")


def test_digest_failure_does_not_break_run(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """摘要失败不该让整个运行失败——它是最后一步，属附加产物。"""
    pipeline = Pipeline(settings, conn, run_id="r")
    original = pipeline.stage_digest

    def boom(**kwargs: object):
        from automail.pipeline import StageResult

        return StageResult(name="digest", exit_code=EXIT_PARTIAL, error="disk full")

    pipeline.stage_digest = boom  # type: ignore[method-assign]
    result = pipeline.run(apply=False, with_digest=True, calendar=FakeCalendar())

    assert result.stage("digest").exit_code == EXIT_PARTIAL
    assert result.exit_code == EXIT_PARTIAL
    _ = original


# ──────────────────────────────────────────────────────────────
# 退出码与跳过选项
# ──────────────────────────────────────────────────────────────


def test_exit_code_is_worst_stage(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """整体退出码取最严重的阶段。"""
    pipeline = Pipeline(settings, conn, run_id="r")

    from automail.pipeline import StageResult

    pipeline.stage_sync = lambda **kw: StageResult(name="sync", skipped=True,
                                                   exit_code=EXIT_OK, skip_reason="x")
    pipeline.stage_extract = lambda **kw: StageResult(name="extract", exit_code=EXIT_PARTIAL)
    pipeline.stage_push = lambda **kw: StageResult(name="push", exit_code=EXIT_FATAL)

    result = pipeline.run(apply=False, calendar=FakeCalendar())
    assert result.exit_code == EXIT_FATAL


def test_skip_options(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """--no-* 选项应跳过对应阶段（用于「只重试推送」这类场景）。

    跳过同步是实用的：它避免无谓的邮箱往返（163 有风控，少连一次更好）。
    """
    _insert_event(conn)
    cal = FakeCalendar()
    pipeline = Pipeline(settings, conn, run_id="r")
    result = pipeline.run(
        apply=False, calendar=cal, skip_sync=True, skip_extract=True
    )

    assert result.stage("sync").skipped is True
    assert result.stage("sync").skip_reason == "按要求跳过"
    assert result.stage("extract").skipped is True
    assert result.stage("push").skipped is False


def test_dry_run_writes_nothing(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """dry-run 全程零副作用：不写日历、不改事件状态。"""
    event_id = _insert_event(conn)
    cal = FakeCalendar()
    pipeline = Pipeline(settings, conn, run_id="r")
    result = pipeline.run(apply=False, calendar=cal)

    assert result.apply is False
    assert cal.events == {}, "dry-run 不得写入日历"
    row = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["status"] == "approved", "dry-run 不应改状态"


def test_stats_snapshot_is_json_serializable(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """结果必须能直接写入 runs.stats（JSON）。"""
    import json

    _insert_event(conn)
    pipeline = Pipeline(settings, conn, run_id="r")
    result = pipeline.run(apply=False, calendar=FakeCalendar())

    payload = json.dumps(result.as_dict(), ensure_ascii=False)
    assert "stages" in payload
    assert result.run_id == "r"


def test_stage_result_labels() -> None:
    from automail.pipeline import StageResult

    assert StageResult(name="x", exit_code=EXIT_OK).label == "成功"
    assert StageResult(name="x", exit_code=EXIT_PARTIAL).label == "部分完成"
    assert StageResult(name="x", exit_code=EXIT_FATAL).label == "失败"
    assert StageResult(name="x", skipped=True).label == "跳过"


# ──────────────────────────────────────────────────────────────
# 推送统计合并
# ──────────────────────────────────────────────────────────────


def test_merge_push_stats_adds_counts() -> None:
    approved = PushStats(considered=2, created=1, frozen=1)
    due = PushStats(considered=1, created=1)

    merged = _merge_push_stats(approved, due)
    assert merged["considered"] == 3
    assert merged["created"] == 2
    assert merged["frozen"] == 1


def test_merge_push_stats_takes_max_limit_hit() -> None:
    """两侧受同一上限约束，limit_hit 取最大值而非相加（避免重复计数）。"""
    approved = PushStats(limit_hit=3)
    due = PushStats(limit_hit=2)
    assert _merge_push_stats(approved, due)["limit_hit"] == 3


def test_merge_push_stats_caps_errors() -> None:
    approved = PushStats(errors=[f"e{i}" for i in range(8)])
    due = PushStats(errors=[f"d{i}" for i in range(8)])
    assert len(_merge_push_stats(approved, due)["errors"]) == 10


# ──────────────────────────────────────────────────────────────
# SyncStats 聚合完整性
# ──────────────────────────────────────────────────────────────


def test_sync_stats_aggregates_scanned() -> None:
    """**回归测试**：``scanned`` 必须在聚合层可见。

    曾漏掉它，导致摘要显示「扫描 0，新增 1」这种自相矛盾的数字，
    使用者会以为统计坏了，进而怀疑整个同步结果。
    """
    from automail.mail.sync import FolderSyncStats, SyncStats

    stats = SyncStats(
        folders=[
            FolderSyncStats(folder="INBOX", scanned=3, inserted=2),
            FolderSyncStats(folder="订阅邮件", scanned=1, inserted=1),
        ]
    )
    assert stats.scanned == 4
    assert stats.inserted == 3
    assert stats.as_dict()["scanned"] == 4


def test_sync_stats_reports_errors() -> None:
    from automail.mail.sync import FolderSyncStats, SyncStats

    clean = SyncStats(folders=[FolderSyncStats(folder="INBOX", inserted=1)])
    assert clean.has_errors is False

    failed = SyncStats(
        folders=[FolderSyncStats(folder="INBOX", fetch_failed=2)]
    )
    assert failed.has_errors is True

    with_error = SyncStats(
        folders=[FolderSyncStats(folder="INBOX", error="throttled")]
    )
    assert with_error.has_errors is True


# ──────────────────────────────────────────────────────────────
# 端到端：完整流程
# ──────────────────────────────────────────────────────────────


def test_full_run_with_real_local_data(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """完整流程（无邮箱凭据、有本地邮件与已批准事件）。

    这是最常见的使用形态：邮件早已同步过，只需重跑抽取与推送。
    """
    conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            body_excerpt, body_sha256, extract_status, is_canonical, stale, fetched_at)
        VALUES ('163', 'INBOX', 1, 1, '会议通知',
                '会议定于 2026年10月20日 下午3点 举行。', 'h1', 'pending', 1, 0, ?)
        """,
        (iso(utcnow()),),
    )
    cal = FakeCalendar()
    pipeline = Pipeline(settings, conn, run_id="e2e")
    result = pipeline.run(apply=True, calendar=cal, with_digest=True)

    assert result.lock_acquired is True
    assert result.stage("sync").skipped is True  # 无凭据
    assert result.stage("extract").stats["candidates"] == 1
    assert result.stage("digest").exit_code == EXIT_OK

    # 抽取出的候选是待审状态 → 不会自动推送
    assert cal.events == {}, "LLM/规则候选应待审，不该自动入历"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_run_after_approval_pushes_event(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """批准后重跑，事件应被推送到日历。"""
    from automail.review import ReviewQueue

    conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            body_excerpt, body_sha256, extract_status, is_canonical, stale, fetched_at)
        VALUES ('163', 'INBOX', 1, 1, '会议通知',
                '会议定于 2026年10月20日 下午3点 举行。', 'h1', 'pending', 1, 0, ?)
        """,
        (iso(utcnow()),),
    )
    cal = FakeCalendar()
    pipeline = Pipeline(settings, conn, run_id="r1")
    pipeline.run(apply=True, calendar=cal)

    event_id = conn.execute("SELECT id FROM events").fetchone()[0]
    ReviewQueue(conn).approve([event_id])

    result = Pipeline(settings, conn, run_id="r2").run(apply=True, calendar=cal)

    assert result.stage("push").stats["created"] == 1
    assert len(cal.events) == 1
    status = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()[0]
    assert status == "pushed"


def test_second_run_is_idempotent(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """重复运行不产生副作用（幂等）。"""
    _insert_event(conn, fingerprint="fp-idem-2")
    cal = FakeCalendar()

    first = Pipeline(settings, conn, run_id="a").run(apply=True, calendar=cal)
    assert first.stage("push").stats["created"] == 1

    second = Pipeline(settings, conn, run_id="b").run(apply=True, calendar=cal)
    assert second.stage("push").stats["created"] == 0
    assert len(cal.events) == 1, "重跑不得产生重复事件"


# ──────────────────────────────────────────────────────────────
# 纵深防御：锁失效时幂等性必须顶住
# ──────────────────────────────────────────────────────────────


def test_idempotency_holds_when_runs_overlap(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """**纵深防御验证**：即使两个运行重叠（锁未生效），下层幂等性仍保证正确。

    锁的粒度只覆盖单次 ``Pipeline.run()``。若两个任务几乎同时启动，
    第二个可能在第一个释放锁后才拿到锁——此时锁形同虚设。

    但这不会造成损害，因为：

    * **同步** 幂等：``highest_uid`` 游标 + ``UNIQUE(account,folder,uid_validity,uid)``
    * **推送** 幂等：create 前按 ``auto_mail_key`` 反查，命中则回填

    因此锁的定位是「**减少无谓工作**」的第一道防线，而**正确性由幂等性保证**。
    这个分层让系统在锁失效时依然安全——不依赖单一机制。
    """
    _insert_event(conn, fingerprint="overlap-fp")
    cal = FakeCalendar()

    first = Pipeline(settings, conn, run_id="overlap-1").run(
        apply=True, calendar=cal, skip_sync=True, skip_extract=True
    )
    assert first.stage("push").stats["created"] == 1
    assert len(cal.events) == 1

    # 第二个运行紧跟着跑（模拟锁未生效）
    second = Pipeline(settings, conn, run_id="overlap-2").run(
        apply=True, calendar=cal, skip_sync=True, skip_extract=True
    )

    assert second.stage("push").stats["created"] == 0, "应识别出远端已有该事件"
    assert len(cal.events) == 1, "不得产生重复事件"


def test_sync_unique_constraint_prevents_duplicate_rows(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """同步层的幂等由唯一约束保证（锁失效时的第二道防线）。"""
    import sqlite3 as _sqlite3

    def insert() -> None:
        conn.execute(
            """
            INSERT INTO messages (account, folder, uid_validity, uid, subject,
                is_canonical, stale, fetched_at)
            VALUES ('163', 'INBOX', 1, 42, '重复同步', 1, 0, ?)
            """,
            (iso(utcnow()),),
        )

    insert()
    with pytest.raises(_sqlite3.IntegrityError):
        insert()
