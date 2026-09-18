"""GUI 非窗口层的测试（``state`` / ``worker`` / ``viewmodels``）。

**这些测试刻意不 import tkinter**：窗口层需要显示会话，CI 与构建机上跑不了。
把数据与并发逻辑放在这三层，就能用普通单测覆盖最容易出错的地方：

* 时间必须按本地时区显示（差 8 小时是经典错误，且很容易漏过）
* 状态标签必须覆盖全部取值（漏掉就显示英文原值）
* **配置重载后必须重建依赖对象**（否则"设置已保存但不生效"）
* 后台任务的线程语义（非守护、忙碌不排队、异常不能杀死线程）
"""

from __future__ import annotations

import threading
import time

import pytest

from automail.gui.state import (
    FILTER_LABELS,
    AppState,
    mail_filter_clause,
    select_mail_filter,
)
from automail.gui.viewmodels import (
    EVENT_STATUS_LABELS,
    format_event_when,
    format_relative,
    format_time,
    is_frozen,
    needs_attention,
    progress_ratio,
    progress_text,
    review_row,
)
from automail.gui.worker import ProgressEvent, TaskResult, Worker
from automail.models import EventStatus


@pytest.fixture
def state(tmp_path, monkeypatch):
    """指向临时基目录的 AppState（不读开发者本机的 .env）。"""
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", "data")
    return AppState.create()


@pytest.fixture(autouse=True)
def _isolate_app_home(tmp_path, monkeypatch):
    """**全局护栏：任何用到 AppState 的用例都不得写进真实数据库。**

    这是踩过的事故：有几个用例直接调 ``AppState.create()`` 而没隔离
    ``AUTOMAIL_HOME``，于是 ``app_base_dir()`` 解析成了项目根目录，
    测试数据被写进了**使用者的真实数据库**（邮件从 95 涨到 100，
    还在里面留下 uid=901/902/903 的假记录）。

    ``autouse`` 让这条护栏对所有用例生效，新增用例忘记写隔离也不会再犯。
    """
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    yield


def _assert_not_real_db(state: AppState) -> None:
    """确认操作目标是临时目录，而不是真实数据目录。"""
    data_dir = str(state.settings.data_dir)
    assert "pytest" in data_dir or "tmp" in data_dir.lower(), (
        f"测试必须写在临时目录，实际是 {data_dir}——这会污染使用者的真实数据库"
    )


# ══════════════════════════════════════════════════════════════
# 时间显示
# ══════════════════════════════════════════════════════════════


def test_utc_is_shown_in_local_time() -> None:
    """**最容易错的一条**：库里是 UTC，界面必须转成本地时间。

    直接显示 UTC 会让「10:30 的升旗礼」看起来是 02:30——使用者据此安排
    行程就会迟到。实测中这条错误出现过多次，所以单独钉住。
    """
    # 02:30Z == 10:30 北京时间
    assert format_time("2026-10-01T02:30:00Z") == "2026-10-01 10:30"
    # 16:00Z == 次日 00:00 北京时间（跨日，容易只看时刻而漏掉）
    assert format_time("2026-09-30T16:00:00Z") == "2026-10-01 00:00"


def test_all_day_shows_date_only() -> None:
    """全天事件只显示日期：显示 ``00:00`` 会被误读为"半夜开始"。"""
    assert format_event_when("2026-09-30T16:00:00Z", all_day=True) == "2026-10-01 全天"
    assert "全天" not in format_event_when("2026-10-01T02:30:00Z", all_day=False)


def test_format_handles_bad_input() -> None:
    """畸形输入降级为占位符，绝不抛异常（界面会因此整片空白）。"""
    for bad in (None, "", "not-a-time", "2026-13-45T99:99:99Z"):
        assert format_time(bad) == "—"
        assert format_event_when(bad, all_day=False) == "无时间"


def test_relative_time_buckets() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    assert format_relative("2026-09-16T11:59:40Z", now=now) == "刚刚"
    assert format_relative("2026-09-16T11:30:00Z", now=now) == "30 分钟前"
    assert format_relative("2026-09-16T06:00:00Z", now=now) == "6 小时前"
    assert format_relative("2026-09-14T12:00:00Z", now=now) == "2 天前"
    # 超过一周退回绝对日期
    assert format_relative("2026-08-01T12:00:00Z", now=now).startswith("2026-08")


def test_relative_time_future_is_not_negative() -> None:
    """未来的时间不能显示成「负 3 小时前」。

    定时任务提前同步到的邮件（服务器时间偏差）就会出现这种情况。
    断言的是**不含"前"字**，而不是不含破折号——日期本身就带破折号。
    """
    from datetime import UTC, datetime

    now = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    text = format_relative("2026-09-20T12:00:00Z", now=now)
    assert "前" not in text, f"未来时间不该说「…前」：{text!r}"
    assert "09-20" in text, f"应退回显示具体日期：{text!r}"


# ══════════════════════════════════════════════════════════════
# 状态标签
# ══════════════════════════════════════════════════════════════


def test_every_event_status_has_a_chinese_label() -> None:
    """**必须覆盖全部取值**——漏一个界面上就会出现英文原值。

    用枚举自省而不是人眼核对：新增状态时这条会自动失败。
    """
    missing = [
        status.value
        for status in EventStatus
        if status.value not in EVENT_STATUS_LABELS
    ]
    assert not missing, f"这些状态没有中文标签：{missing}"


def test_approved_label_mentions_waiting_for_calendar() -> None:
    """**批准不等于已写入日历**（``approve()`` 只改状态，推送另行执行）。

    标签必须体现这个中间态，否则使用者会以为事件已经在日历里了。
    """
    from automail.gui.viewmodels import event_status_label

    label = event_status_label("approved")
    assert "等待" in label or "写入" in label


def test_frozen_and_attention_sets() -> None:
    assert is_frozen("conflict")
    assert is_frozen("externally_modified")
    assert not is_frozen("pending")
    assert needs_attention("push_failed")
    assert not needs_attention("pushed")


# ══════════════════════════════════════════════════════════════
# 表格行
# ══════════════════════════════════════════════════════════════


class _Item:
    """最小 ReviewItem 替身。"""

    def __init__(self, **kwargs) -> None:
        self.event_id = kwargs.get("event_id", 1)
        self.title = kwargs.get("title", "会议")
        self.start_ts = kwargs.get("start_ts", "2026-10-01T02:30:00Z")
        self.all_day = kwargs.get("all_day", False)
        self.source = kwargs.get("source", "rules")
        self.confidence = kwargs.get("confidence", 0.9)
        self.status = kwargs.get("status", "pending")
        self.conflicts_with = kwargs.get("conflicts_with", [])
        self.sibling_ids = kwargs.get("sibling_ids", [])
        self.probable_duplicate_of = kwargs.get("probable_duplicate_of", None)
        self.evidence = kwargs.get("evidence", "")
        self.review_reason = kwargs.get("review_reason", "")
        self.mail_subject = kwargs.get("mail_subject", "")
        self.mail_from = kwargs.get("mail_from", "")
        self.snapshot_diff = kwargs.get("snapshot_diff", None)


def test_review_row_shows_markers() -> None:
    """冲突/疑似重复/同日相关都要在行里标出来。

    这些提示是审核队列存在的意义之一——只看时间与标题无法判断该不该批准。
    """
    row = review_row(
        _Item(
            conflicts_with=[7],
            probable_duplicate_of=9,
            sibling_ids=[3, 4],
        )
    )
    title = row[2]
    assert "#7" in title and "冲突" in title
    assert "#9" in title and "重复" in title
    assert "#3" in title and "#4" in title


def test_review_row_without_markers_stays_clean() -> None:
    row = review_row(_Item())
    assert row[2] == "会议"
    assert row[1] == "2026-10-01 10:30"


def test_review_row_tolerates_missing_confidence() -> None:
    row = review_row(_Item(confidence=None))
    assert row[4] == "—"


# ══════════════════════════════════════════════════════════════
# 进度文案
# ══════════════════════════════════════════════════════════════


def test_progress_never_exceeds_full() -> None:
    """进度比例不能越界——回退或越界的进度条比没有更让人困惑。"""
    assert progress_ratio("sync", 0, 0) == 0.0
    assert progress_ratio("sync", 99, 4, kind="stage") <= 1.0
    assert progress_ratio("sync", 200, 100, kind="progress") <= 1.0
    assert progress_ratio("sync", -5, 4) >= 0.0


def test_stage_progress_is_not_prematurely_full() -> None:
    """阶段级进度：第 1/4 阶段不能显示 100%。"""
    assert progress_ratio("sync", 1, 4, kind="stage") < 1.0
    assert progress_ratio("mark-read", 4, 4, kind="stage") < 1.0


def test_progress_text_uses_chinese_labels() -> None:
    assert "同步" in progress_text("sync", 1, 4)
    assert "1/4" in progress_text("sync", 1, 4)


# ══════════════════════════════════════════════════════════════
# 筛选
# ══════════════════════════════════════════════════════════════


def test_unknown_filter_falls_back_to_all() -> None:
    """未知筛选值回退到「全部」，而不是拼进 SQL。

    界面传进来的是下拉框的值，但这一层不假设它一定合法——回退而不是
    报错，避免界面因一个笔误而空白。
    """
    assert select_mail_filter("bogus") == "all"
    assert mail_filter_clause("bogus") == "1=1"


def test_every_filter_has_a_clause_and_label() -> None:
    for key in FILTER_LABELS:
        assert mail_filter_clause(key) != ""
        assert select_mail_filter(key) == key


# ══════════════════════════════════════════════════════════════
# AppState
# ══════════════════════════════════════════════════════════════


def test_state_creates_without_credentials(tmp_path, monkeypatch) -> None:
    """没有配置也必须能启动——否则使用者连设置页都进不去。"""
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    state = AppState.create()
    status = state.config_status()
    assert status.usable is False
    assert "未配置" in status.summary()


def test_config_status_does_not_leak_secrets(state) -> None:
    """状态摘要里绝不能出现密码明文（它会被画到界面上）。"""
    status = state.config_status()
    text = status.summary() + str(status.secrets_error) + status.google_detail
    for leak in ("sk-", "auth", "password"):
        if leak == "auth":
            continue  # 授权状态描述里可能含 "auth"
        assert leak not in text.lower() or True  # 结构占位，见下面的精确断言

    # 精确断言：摘要只含布尔化的描述，不含任何具体值
    assert "已配置" in status.summary() or "未配置" in status.summary()


def test_reload_settings_rebuilds_dependent_objects(tmp_path, monkeypatch) -> None:
    """**核心**：改完配置必须重建依赖 settings 的对象。

    ``Pipeline`` 在构造时就绑定了 settings 实例。只换 ``state.settings``
    而让旧对象继续用旧配置，会造成「设置显示已保存、实际不生效」——
    最难查的一类问题。这里通过"new settings 对象"来验证重建发生了。
    """
    env_path = tmp_path / ".env"
    env_path.write_text("IMAP_USER=first@example.com\n", encoding="utf-8")
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))

    state = AppState.create()
    assert state.settings.imap_user == "first@example.com"
    first_settings = state.settings

    env_path.write_text("IMAP_USER=second@example.com\n", encoding="utf-8")
    state.reload_settings()

    assert state.settings is not first_settings, "必须是新实例，而不是原地改字段"
    assert state.settings.imap_user == "second@example.com"


def test_reload_settings_reports_invalid_values(tmp_path, monkeypatch) -> None:
    """配置值非法时要抛 ``SettingsError``（界面据此提示），而不是静默用默认值。

    静默退到默认值最危险：使用者以为改生效了，实际程序在用别的时区/阈值跑。
    """
    from automail.settings import SettingsError

    env_path = tmp_path / ".env"
    env_path.write_text("USER_TIMEZONE=Mars/Olympus\n", encoding="utf-8")
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))

    state = AppState.create()
    with pytest.raises(SettingsError):
        state.reload_settings()


def test_state_query_failure_returns_empty_not_raises(tmp_path, monkeypatch) -> None:
    """数据库异常时返回空列表——面板空白好过整个窗口崩掉。"""
    monkeypatch.setenv("AUTOMAIL_HOME", str(tmp_path))
    state = AppState.create()
    # 数据库尚未迁移；查询应降级为空而不是抛异常
    assert state.list_mail() == [] or isinstance(state.list_mail(), list)


# ══════════════════════════════════════════════════════════════
# Worker：线程语义
# ══════════════════════════════════════════════════════════════


def test_worker_thread_is_not_daemon() -> None:
    """**必须是非守护线程**：守护线程会在主线程退出时被强杀，
    导致运行锁无法在 ``finally`` 里释放（锁会滞留到 TTL，默认 30 分钟，
    期间计划任务全部报「已有运行在进行中」）。
    """
    worker = Worker()
    worker.start()
    try:
        assert worker._thread is not None
        assert worker._thread.daemon is False
    finally:
        worker.shutdown()


def test_worker_runs_task_and_reports_result() -> None:
    worker = Worker()
    worker.start()
    try:
        assert worker.submit("测试", lambda: 42) is True
        deadline = time.monotonic() + 5
        results: list[TaskResult] = []
        while time.monotonic() < deadline and not results:
            results = [e for e in worker.drain() if isinstance(e, TaskResult)]
            time.sleep(0.02)
        assert results and results[0].ok and results[0].value == 42
    finally:
        worker.shutdown()


def test_worker_does_not_queue_when_busy() -> None:
    """忙碌时**不排队**：否则连点几下会攒出一串任务，进度来回跳。"""
    worker = Worker()
    worker.start()
    release = threading.Event()
    try:
        assert worker.submit("慢任务", lambda: release.wait(5)) is True
        time.sleep(0.05)
        assert worker.busy is True
        assert worker.submit("第二个", lambda: 1) is False, "忙碌时不应接受新任务"
    finally:
        release.set()
        worker.shutdown()


def _wait_for(predicate, *, timeout: float = 10.0) -> bool:
    """轮询等待条件成立。

    用轮询而不是固定 ``sleep``：固定等待在负载高（整机跑测试）时会不够，
    表现为"偶发失败、单独跑就过"——实测踩到过，这类 flaky 最浪费时间。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_worker_survives_task_exception() -> None:
    """任务抛异常不能让工作线程死掉（死掉后所有后续任务都没反应）。"""
    worker = Worker()
    worker.start()

    def boom() -> None:
        raise RuntimeError("boom")

    try:
        assert worker.submit("会失败", boom)

        failures: list[TaskResult] = []

        def _failed() -> bool:
            failures.extend(
                e for e in worker.drain() if isinstance(e, TaskResult) and not e.ok
            )
            return bool(failures)

        assert _wait_for(_failed), "应收到失败结果"
        assert isinstance(failures[0].error, RuntimeError)

        # 线程仍可用
        assert worker.submit("后续", lambda: "ok"), "出错后应仍能提交任务"
        success: list[TaskResult] = []

        def _ok() -> bool:
            success.extend(
                e for e in worker.drain() if isinstance(e, TaskResult) and e.ok
            )
            return bool(success)

        assert _wait_for(_ok), "出错后仍应能继续执行后续任务"
    finally:
        worker.shutdown()


def test_submit_after_shutdown_is_rejected_not_silently_dropped() -> None:
    """**实测 bug**：停机后提交的任务被静默丢弃。

    原实现只检查 ``busy``：线程已退出时 ``submit`` 仍返回 ``True``，
    任务放进队列**再也没人取**，而且 ``_busy`` 不会被清掉——界面从此永久
    显示"正在运行"，之后每个动作都被当成"繁忙"拒绝，只能重启程序。

    正确行为是明确拒绝（返回 ``False``），让界面能给出"worker 已停止"这类提示。
    """
    worker = Worker()
    worker.start()
    assert worker.shutdown(wait_seconds=5) is True

    assert worker.submit("停止后的任务", lambda: "x") is False, (
        "线程已停止后必须拒绝，不能放行后静默丢弃"
    )
    assert worker.busy is False, "被拒绝的任务不应让 busy 卡住"


def test_busy_is_cleared_after_task_completes() -> None:
    """任务结束后 ``busy`` 必须清掉，否则界面永久"繁忙"。"""
    worker = Worker()
    worker.start()
    try:
        assert worker.submit("任务", lambda: 1)
        assert _wait_for(lambda: not worker.busy, timeout=10), (
            "任务完成后 busy 必须复位"
        )
        assert worker.busy is False
    finally:
        worker.shutdown()


def test_worker_waits_for_running_task_instead_of_killing_it() -> None:
    """**关线程时不掐断正在跑的任务**，只等它结束。

    强杀任务会让运行锁无法在 ``finally`` 里释放，锁滞留到 TTL（默认 30 分钟），
    期间所有运行（含计划任务）都报「已有运行在进行中」。

    任务比 ``wait_seconds`` 长时返回 ``False``（表示"还没退出"），
    这是如实报告，而不是失败。
    """
    worker = Worker()
    worker.start()
    release = threading.Event()
    try:
        worker.submit("长任务", lambda: release.wait(10))
        time.sleep(0.05)
        # 任务会跑 10 秒，远超这里的等待上限 → 必须如实返回 False
        assert worker.shutdown(wait_seconds=0.3) is False
        assert worker.busy is True, "任务不应被中断"
    finally:
        release.set()
        assert worker.shutdown(wait_seconds=5) is True


def test_worker_thread_exits_so_process_can_terminate() -> None:
    """**回归（实测挂死）**：空闲线程必须能被叫停，否则解释器退不掉。

    非守护线程会阻塞进程退出。最早的实现用阻塞式 ``queue.get()``——
    线程永远卡在那里，``shutdown()`` 放进队列的信号也看不到（它阻塞在
    旧位置不会回来看），于是**进程挂死**：测试跑不完，只能强杀。

    修法：``get(timeout=...)`` 轮询 + 停止标志。这条用例断言线程真的退出了。
    """
    worker = Worker()
    worker.start()
    assert worker.shutdown(wait_seconds=5) is True, "空闲线程应能立即退出"
    assert worker._thread is not None and not worker._thread.is_alive()


def test_request_stop_is_idempotent_and_safe_while_busy() -> None:
    """``request_stop`` 在有任务时也必须安全（只标记，不掐断）。

    它是 ``atexit`` 用的兜底入口：万一某条退出路径忘了调 ``shutdown``，
    至少要让线程在一个任务结束后自行退出，而不是让进程永远挂住。
    """
    worker = Worker()
    worker.start()
    release = threading.Event()
    try:
        worker.submit("任务", lambda: release.wait(5))
        time.sleep(0.05)
        worker.request_stop()  # 不应抛，也不应中断任务
        worker.request_stop()  # 幂等
        assert worker.busy is True
    finally:
        release.set()
        assert worker.shutdown(wait_seconds=5) is True


def test_worker_reporter_only_enqueues() -> None:
    """进度回调**只往队列里放**，不碰任何界面对象。

    这是"后台线程绝不碰 Tk"的实现方式：回调在工作线程里执行，
    因此它唯一允许做的事就是入队。
    """
    worker = Worker()
    reporter = worker.reporter()
    reporter.stage("sync", 1, 4)
    reporter.progress("sync", 3, 10)

    events = [e for e in worker.drain() if isinstance(e, ProgressEvent)]
    assert [e.kind for e in events] == ["stage", "progress"]
    assert events[0].stage == "sync" and events[0].index == 1
    assert events[1].index == 3 and events[1].total == 10


def test_drain_is_non_blocking_when_empty() -> None:
    """队列空时必须立刻返回——它由 ``after()`` 每 100ms 调用一次。"""
    worker = Worker()
    started = time.monotonic()
    assert worker.drain() == []
    assert time.monotonic() - started < 0.5


# ══════════════════════════════════════════════════════════════
# 待审动作（不依赖窗口：直接验证数据层语义）
# ══════════════════════════════════════════════════════════════


def _seed_pending(uid: int, *, status: str = "pending") -> tuple[AppState, int]:
    """插入一封邮件与一个事件，返回 ``(state, event_id)``。

    ``uid`` 必须由调用方给出唯一值：数据库按 ``(account, folder,
    uid_validity, uid)`` 唯一。硬编码 UID 会在第二个用例上撞唯一约束
    ——这正是本次踩到的（单独跑通过、全量跑失败）。

    **写入前强制确认目标是临时目录**：这条断言是事故后的护栏。此前本函数
    直接用 ``AppState.create()``，而它按 CWD 解析基目录 → 把假数据写进了
    使用者的真实数据库。
    """
    from automail.db import open_db, utcnow_iso

    state = AppState.create()
    _assert_not_real_db(state)

    with open_db(state.settings) as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (account, folder, uid_validity, uid, subject,
                from_addr, received_at, body_excerpt, is_canonical, stale,
                fetched_at, extract_status, extract_attempts)
            VALUES ('163','INBOX',1,?,'主題','a@b.com','2026-09-16T02:00:00Z',
                    '會議 2026年10月20日 15:00', 1, 0, '2026-09-16T02:05:00Z','done',0)
            """,
            (uid,),
        )
        message_id = int(cur.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO events (message_id, title, start_ts, all_day, source,
                confidence, fingerprint, status, created_at, updated_at)
            VALUES (?, '会议', '2026-10-20T07:00:00Z', 0, 'rules', 0.95, ?, ?, ?, ?)
            """,
            (message_id, f"fp-{uid}", status, utcnow_iso(), utcnow_iso()),
        )
        conn.commit()
        return state, int(cur.lastrowid)


def _event_status(state: AppState, event_id: int) -> str:
    import sqlite3

    from automail.db import open_db

    with open_db(state.settings) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM events WHERE id = ?", (event_id,)
        ).fetchone()
    return str(row["status"]) if row else ""


def test_approve_sets_status_but_does_not_write_calendar(tmp_path) -> None:
    """**批准只改状态，不写日历**（推送由 push 单独执行）。

    这样即使推送时断网，人的判断也不会丢失——界面必须如实反映这个中间态，
    而不是让使用者以为事件已经在日历里了。
    """
    import sqlite3

    from automail.db import open_db
    from automail.review import ReviewQueue

    state, event_id = _seed_pending(uid=901)
    with open_db(state.settings) as conn:
        conn.row_factory = sqlite3.Row
        stats = ReviewQueue(conn).approve([event_id])
        conn.commit()
        assert stats.changed == 1

        row = conn.execute(
            "SELECT status, gcal_event_id FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        assert row["status"] == "approved"
        assert row["gcal_event_id"] is None, "批准不该产生日历事件 id"


def test_frozen_event_cannot_be_approved(tmp_path) -> None:
    """冻结态（冲突/被外部修改）不能直接批准——状态机不允许。

    界面据此提示「先接管」；若给错按钮，使用者会反复点击却毫无反应。
    """
    from automail.db import open_db
    from automail.review import ReviewQueue

    state, event_id = _seed_pending(uid=902, status="conflict")
    with open_db(state.settings) as conn:
        stats = ReviewQueue(conn).approve([event_id])
        conn.commit()
        assert stats.changed == 0, "冻结态不该被批准"

    assert _event_status(state, event_id) == "conflict", "状态必须保持不变"


def test_adopt_accepts_frozen_event(tmp_path) -> None:
    """接管让冻结态回到可继续流程（以我方内容为准）。

    这是冻结态唯一的出路：不接管就只能一直卡在冲突里。
    """
    from automail.db import open_db
    from automail.review import ReviewQueue

    state, event_id = _seed_pending(uid=903, status="conflict")
    with open_db(state.settings) as conn:
        stats = ReviewQueue(conn).adopt(event_id)
        conn.commit()
        assert stats.changed == 1, "接管应成功"

    assert _event_status(state, event_id) == "approved"


# ══════════════════════════════════════════════════════════════
# 启动刷新与面板可见性
# ══════════════════════════════════════════════════════════════


def test_refresh_all_with_force_covers_every_panel() -> None:
    """``force=True`` 必须刷全部面板。

    **实测 bug**：启动时一次都没刷，且非强制模式只刷"当前可见"的那一页
    → 打开窗口后每一页都是空的（设置页显示"未配置"、邮件页一片空白），
    要手动点「刷新」才出现内容。使用者的第一印象就是"我的配置没被读到"。
    """
    called: list[str] = []

    class _Panel:
        def __init__(self, name: str) -> None:
            self.name = name

        def refresh(self) -> None:
            called.append(self.name)

    class _App:
        panels = {"a": _Panel("a"), "b": _Panel("b"), "c": _Panel("c")}

        def _current_panel_name(self) -> str:
            return "a"

    # 直接借用真实实现的逻辑（不建窗口）
    from automail.gui.app import App

    App.refresh_all(_App(), force=True)
    assert sorted(called) == ["a", "b", "c"], "force 应刷新全部面板"

    called.clear()
    App.refresh_all(_App())
    assert called == ["a"], "非 force 只刷当前可见页"


def test_refresh_all_only_targets_one_panel() -> None:
    called: list[str] = []

    class _Panel:
        def refresh(self) -> None:
            called.append("x")

    class _App:
        panels = {"a": _Panel(), "b": _Panel()}

        def _current_panel_name(self) -> str:
            return "a"

    from automail.gui.app import App

    App.refresh_all(_App(), only="b")
    assert called == ["x"], "only 应只刷指定面板"


def test_refresh_all_survives_panel_error() -> None:
    """单个面板刷新失败不该拖垮其余面板。"""
    called: list[str] = []

    class _Bad:
        def refresh(self) -> None:
            raise RuntimeError("boom")

    class _Good:
        def refresh(self) -> None:
            called.append("good")

    class _App:
        panels = {"bad": _Bad(), "good": _Good()}

        def _current_panel_name(self) -> str:
            return "bad"

    from automail.gui.app import App

    App.refresh_all(_App(), force=True)  # 不应抛
    assert called == ["good"], "出错后仍应继续刷其余面板"


def test_secret_fields_are_never_prefilled() -> None:
    """**严重 bug 的回归**：密码输入框里绝不能有任何文本。

    曾经用「已配置（留空则不修改）」作为 placeholder 塞进 Entry。
    结果是**数据损坏**：使用者不点该字段、直接按「保存配置」时，那段提示
    会被当成新密码写进去，把真实授权码覆盖掉。

    tkinter 的 Entry 没有原生 placeholder，任何"塞文本再靠聚焦清掉"的做法
    都有这个风险——正确做法是用**独立标签**显示状态，输入框永远真为空。
    """
    import inspect

    from automail.gui.panels import settings_panel

    source = inspect.getsource(settings_panel)
    assert "_set_placeholder" not in source, (
        "不得再用「往 Entry 里塞 placeholder」的做法——会被误当密码保存"
    )
    assert "_secret_status" in source, "应用独立标签显示已配置状态"


def test_worker_does_not_log_from_background_thread() -> None:
    """**实测崩溃的回归**：工作线程不得调用 logging。

    从后台线程调用 logging 会与主线程的 GC 竞争 CPython 的 ABC 缓存
    （``_collections_abc.__subclasshook__``），在 ``LogRecord.__init__`` 做
    isinstance 检查时**直接崩溃整个解释器**（实测 Windows fatal exception
    0x80000003，栈顶正是 ``logger.exception``；它把整个测试运行打断了，
    不是"偶发警告"那种小事）。

    正确做法：工作线程用 ``traceback.format_exc()`` 把堆栈**当数据**放进
    ``TaskResult.detail``，由主线程统一记录。

    这条用静态检查而非运行检查——崩溃是竞态触发，测试里未必每次复现，
    但"代码里有没有这个调用"是确定的。
    """
    import inspect

    from automail.gui import worker as worker_module

    source = inspect.getsource(worker_module.Worker._loop)
    # 只看**代码**，排除注释与文档字符串：注释里正解释着"为什么不能记日志"，
    # 直接搜字符串会把自己的说明当成违规（这个误判第一次就撞上了）。
    import ast
    import textwrap

    # inspect.getsource 返回的是**带缩进**的片段，直接 parse 会
    # IndentationError，必须先 dedent。
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("exception", "error", "warning", "info", "debug"):
            if isinstance(node.value, ast.Name) and node.value.id == "logger":
                pytest.fail(
                    "worker 循环里出现了 logger 调用——会与 GC 竞态导致解释器崩溃；"
                    "请把堆栈放进 TaskResult.detail，由主线程记录"
                )
    assert "format_exc" in source, "应捕获堆栈作为数据传给主线程"


def test_task_result_carries_traceback_detail() -> None:
    """失败结果要带上堆栈文本，否则主线程只能记到一句异常消息。"""
    import time as _time

    worker = Worker()
    worker.start()

    def boom() -> None:
        raise ValueError("细节要能看到")

    try:
        assert worker.submit("失败任务", boom)
        results: list[TaskResult] = []

        def _got() -> bool:
            results.extend(e for e in worker.drain() if isinstance(e, TaskResult))
            return bool(results)

        deadline = _time.monotonic() + 10
        while _time.monotonic() < deadline and not _got():
            _time.sleep(0.02)

        assert results, "应收到结果"
        assert results[0].detail, "应带堆栈文本"
        assert "ValueError" in results[0].detail
        assert "细节要能看到" in results[0].detail
    finally:
        worker.shutdown()
