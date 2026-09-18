"""进度回调的测试。

**为什么这个功能必须测「真的会响」**：首次同步一个有历史的邮箱要几分钟。没有
进度反馈时，图形界面看起来就是卡死的——使用者会去点关闭、甚至强杀进程。而强杀
会让运行锁滞留到 TTL（默认 30 分钟），期间一切运行都报「已有运行在进行中」。

所以测试要证明三件事：

1. **阶段**会按 `sync → extract → push → mark-read` 的顺序逐个通知
2. **阶段内部**会分批/逐封推进（只报阶段级的话，界面在阶段内仍是静止的）
3. 不传回调时行为**完全不变**（既有调用方与测试不受影响）
"""

from __future__ import annotations

from automail.pipeline import Pipeline
from automail.progress import ProgressReporter, stage_label
from automail.settings import Settings
from tests.fake_imap import FakeFolder, FakeImapServer


class _Recorder:
    """记录收到的全部进度事件。"""

    def __init__(self) -> None:
        self.stages: list[tuple[str, int, int]] = []
        self.progress: list[tuple[str, int, int]] = []

    def reporter(self) -> ProgressReporter:
        return ProgressReporter(
            on_stage=lambda name, i, total: self.stages.append((name, i, total)),
            on_progress=lambda name, done, total: self.progress.append(
                (name, done, total)
            ),
        )


def _settings(tmp_path, **overrides) -> Settings:
    base = {
        "_env_file": None,
        "account": "163",
        "user_timezone": "Asia/Shanghai",
        "data_dir": tmp_path / "data",
        "out_dir": tmp_path / "out",
        "log_dir": tmp_path / "logs",
    }
    base.update(overrides)
    return Settings(**base)


def _mail(subject: str) -> str:
    return (
        "From: a@b.com\r\n"
        f"Subject: {subject}\r\n"
        "Date: Tue, 15 Sep 2026 22:40:53 +0800\r\n"
        "Message-ID: <m1@b.com>\r\n"
        "\r\n"
        "會議定於 2026年10月20日 15:00 舉行。\r\n"
    )


# ══════════════════════════════════════════════════════════════
# 空操作：不传回调时行为不变
# ══════════════════════════════════════════════════════════════


def test_reporter_defaults_to_noop() -> None:
    """默认 reporter 必须全部是空操作——这是「行为不变」的依据。"""
    reporter = ProgressReporter()
    assert not reporter.active
    # 不抛异常即可（内部不应有 None 解引用）
    reporter.stage("sync", 1, 4)
    reporter.progress("sync", 3, 10)


def test_reporter_isolates_callbacks() -> None:
    """只传其中一个回调时另一个仍要安全。"""
    events: list[tuple[str, int, int]] = []
    only_stage = ProgressReporter(on_stage=lambda n, i, t: events.append((n, i, t)))
    only_stage.progress("sync", 1, 2)  # 不应抛
    only_stage.stage("sync", 1, 2)
    assert events == [("sync", 1, 2)]


def test_stage_labels_cover_pipeline_stages() -> None:
    """界面用的中文标签要覆盖 pipeline 实际会发的阶段名。

    漏掉一个就会在界面上显示英文原名——不是错误，但不该发生。
    """
    for name in ("sync", "extract", "push", "mark-read", "digest"):
        assert stage_label(name) != name or name in ("digest",)


# ══════════════════════════════════════════════════════════════
# 阶段级通知
# ══════════════════════════════════════════════════════════════


def test_stages_fire_in_order(tmp_path, conn) -> None:
    """阶段按固定顺序逐个通知，且「第几个/共几个」要连贯。

    顺序由 pipeline 单独定义——图形界面只接收，不自己拼，否则两处会分叉。
    """
    recorder = _Recorder()
    settings = _settings(tmp_path)
    pipeline = Pipeline(
        settings, conn, run_id="r", reporter=recorder.reporter()
    )

    pipeline.run(apply=False, calendar=_FakeCalendar())

    names = [name for name, _, _ in recorder.stages]
    assert names == ["sync", "extract", "push", "mark-read"]

    indexes = [index for _, index, _ in recorder.stages]
    assert indexes == [1, 2, 3, 4], "序号必须从 1 递增"

    totals = {total for _, _, total in recorder.stages}
    assert totals == {4}, f"总数应恒为 4，实际 {totals}"


def test_digest_is_counted_in_total(tmp_path, conn) -> None:
    """带 digest 时总数要变成 5，否则界面会显示「4/5」却永远到不了 5。"""
    recorder = _Recorder()
    settings = _settings(tmp_path)
    pipeline = Pipeline(settings, conn, run_id="r", reporter=recorder.reporter())

    pipeline.run(apply=False, with_digest=True, calendar=_FakeCalendar())

    assert [name for name, _, _ in recorder.stages][-1] == "digest"
    assert {total for _, _, total in recorder.stages} == {5}


def test_skipped_stage_still_notifies(tmp_path, conn) -> None:
    """被跳过的阶段也要通知，否则进度会停在中途（看起来像卡住）。"""
    recorder = _Recorder()
    settings = _settings(tmp_path)
    pipeline = Pipeline(settings, conn, run_id="r", reporter=recorder.reporter())

    pipeline.run(apply=False, skip_sync=True, skip_push=True, calendar=_FakeCalendar())

    names = [name for name, _, _ in recorder.stages]
    assert names == ["sync", "extract", "push", "mark-read"]
    assert [i for _, i, _ in recorder.stages] == [1, 2, 3, 4]


def test_lock_conflict_emits_no_stage_events(tmp_path, conn) -> None:
    """拿不到锁时不该发任何阶段通知——什么都没跑，报进度是误导。"""
    settings = _settings(tmp_path)
    first = Pipeline(settings, conn, run_id="holder")
    assert first.acquire_lock()[0]

    recorder = _Recorder()
    second = Pipeline(settings, conn, run_id="other", reporter=recorder.reporter())
    stats = second.run(apply=False, calendar=_FakeCalendar())

    assert stats.lock_acquired is False
    assert recorder.stages == []


# ══════════════════════════════════════════════════════════════
# 阶段内推进（关键：只报阶段级不够）
# ══════════════════════════════════════════════════════════════


def test_sync_reports_each_batch(tmp_path, conn) -> None:
    """同步要**逐批**推进，而不是只在阶段开始/结束各报一次。

    首次同步几百封时，阶段级通知会让界面在几分钟里毫无变化。
    """
    server = FakeImapServer()
    folder = FakeFolder(name="INBOX")
    for uid in range(1, 8):
        folder.add(uid=uid, raw=_mail(f"第 {uid} 封"))
    server.folders["INBOX"] = folder
    port = server.start()

    recorder = _Recorder()
    settings = _settings(
        tmp_path,
        imap_host="127.0.0.1",
        imap_port=port,
        imap_user="user@163.com",
        imap_auth_code="authcode",
        imap_use_ssl=False,
        imap_fetch_batch_size=2,  # 7 封 → 4 批
    )
    try:
        pipeline = Pipeline(settings, conn, run_id="r", reporter=recorder.reporter())
        pipeline.run(apply=True, skip_extract=True, skip_push=True)
    finally:
        server.stop()

    sync_events = [p for p in recorder.progress if p[0] == "sync"]
    assert sync_events, "同步必须上报批次进度"
    assert len(sync_events) >= 3, f"7 封按每批 2 封应有多批，实际 {sync_events}"

    done_values = [done for _, done, _ in sync_events]
    assert done_values == sorted(done_values), "已完成数必须单调不减"
    assert done_values[-1] == 7, "最后一批应报满总数"


def test_progress_is_monotonic_and_bounded(tmp_path, conn) -> None:
    """进度值单调不减且不超过总数。

    回退或越界的进度条比没有进度条更糟——使用者会以为程序出错了。
    """
    recorder = _Recorder()
    settings = _settings(tmp_path)
    for index in range(3):
        conn.execute(
            """
            INSERT INTO messages (account, folder, uid_validity, uid, subject,
                from_addr, received_at, body_excerpt, is_canonical, stale,
                fetched_at, extract_status, extract_attempts)
            VALUES ('163','INBOX',1,?,?,'a@b.com','2026-09-16T02:00:00Z',
                    '會議 2026年10月20日 15:00', 1, 0, '2026-09-16T02:05:00Z',
                    'pending', 0)
            """,
            (index + 1, f"第 {index + 1} 封"),
        )
    conn.commit()

    pipeline = Pipeline(settings, conn, run_id="r", reporter=recorder.reporter())
    pipeline.run(
        apply=True, skip_sync=True, skip_push=True, calendar=_FakeCalendar()
    )

    extract_events = [p for p in recorder.progress if p[0] == "extract"]
    assert extract_events, "抽取必须逐封上报"
    dones = [done for _, done, _ in extract_events]
    assert dones == sorted(dones), "必须单调不减"
    assert max(dones) <= 3, "不得超过总数"


def test_failed_batch_still_advances_progress(tmp_path, conn) -> None:
    """失败的批次也要计入进度。

    否则进度条会永远差一截、停在未完成状态——使用者无法判断是"还在跑"
    还是"已经结束但有错"。
    """
    from automail.mail.sync import SyncEngine

    recorder = _Recorder()
    settings = _settings(tmp_path, imap_fetch_batch_size=2)

    class _FlakyBackend:
        """第 2 批失败，其余成功。

        必须提供 ``connect``/``close``：取回失败会走「重连重试」路径，
        那个路径真的会调用它们（缺了会 AttributeError）。
        """

        def __init__(self) -> None:
            self.calls = 0

        def connect(self) -> None:
            return None

        def close(self) -> None:
            return None

        def select_folder(self, folder, *, readonly=True):
            from automail.mail.backend import FolderStatus

            return FolderStatus(uid_validity=1, uid_next=10, exists=4)

        def search_uids(self, criterion):
            return [1, 2, 3, 4]

        def fetch_messages(self, uids):
            self.calls += 1
            if self.calls == 2:
                from automail.mail.backend import MailProtocolError

                raise MailProtocolError("simulated")
            from automail.mail.backend import RawMessage

            return {
                uid: RawMessage(
                    uid=uid,
                    raw=_mail(f"第 {uid} 封").encode(),
                    flags=(),
                    internal_date=None,
                )
                for uid in uids
            }

    engine = SyncEngine(
        settings, conn, _FlakyBackend(), reporter=recorder.reporter()
    )
    engine.sync(apply=True)

    sync_events = [p for p in recorder.progress if p[0] == "sync"]
    assert sync_events, "即使有批次失败也要上报进度"
    assert sync_events[-1][1] == 4, f"最终应报满总数，实际 {sync_events}"


class _FakeCalendar:
    """占位日历后端（本文件只关心进度，不关心推送）。"""

    def find_by_auto_mail_key(self, key: str):
        return None

    def list_events(self, **_kwargs):
        return []

    def insert_event(self, payload):
        raise AssertionError("本文件的用例不应真的写入日历")
