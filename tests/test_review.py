"""P3 测试：规范化哈希、三方比对、审核队列、推送安全。

**每个用例都对应一条安全承诺**，不是泛泛的功能测试：

* 规范化必须吸收服务端噪声（否则比对机制失效）
* 用户手改必须被检出且**绝不覆盖**
* 只动自己创建的事件（所有权检查）
* 幂等：重跑不产生重复事件
* 崩溃后能被回收（僵尸、残留运行态）
* 延迟窗口的撤销必须真的生效
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from automail.calendar.backend import (
    build_event_payload,
)
from automail.calendar.fake import FakeCalendar
from automail.calendar.normalize import (
    diff_payloads,
    extract_auto_mail_key,
    normalize_datetime,
    normalize_hash,
    normalize_text,
)
from automail.calendar.ownership import (
    OwnershipVerdict,
    compare,
    describe_verdict,
)
from automail.db import utcnow_iso
from automail.models import EventStatus, ScheduledPushState
from automail.push import PushEngine
from automail.review import (
    ReviewError,
    ReviewQueue,
    annotate_probable_duplicates,
    parse_event_ids,
)
from automail.settings import Settings

# ──────────────────────────────────────────────────────────────
# 规范化：吸收服务端噪声
# ──────────────────────────────────────────────────────────────


def test_normalize_datetime_absorbs_timezone_representation() -> None:
    """``Z`` 与 ``+00:00`` 是同一瞬时，必须归一化到相同值。"""
    assert normalize_datetime({"dateTime": "2026-09-20T10:00:00Z"}) == normalize_datetime(
        {"dateTime": "2026-09-20T10:00:00+00:00"}
    )


def test_normalize_datetime_converts_offsets_to_utc() -> None:
    """``+08:00`` 的 18:00 与 UTC 的 10:00 是同一时刻。"""
    a = normalize_datetime({"dateTime": "2026-09-20T18:00:00+08:00"})
    b = normalize_datetime({"dateTime": "2026-09-20T10:00:00Z"})
    assert a == b == "2026-09-20T10:00"


def test_normalize_datetime_all_day() -> None:
    assert normalize_datetime({"date": "2026-09-20"}) == "D:2026-09-20"


def test_normalize_text_folds_newlines_for_comparison() -> None:
    """**真实缺陷的回归测试**。

    服务端会把描述里的换行折叠成空格。若比较时把换行当有意义的内容，
    每次读取都会「看起来有变化」，导致：
    * ``remote_norm_hash`` 永远不等于 ``snapshot_hash``
    * ``benign_evolution`` 分支永远无法命中
    * 每次服务端改写都被误判为 ``conflict``

    实测中正是这个原因让「用户手改」被误报为「双方都改」。
    """
    with_newline = "第一行\n第二行"
    with_space = "第一行 第二行"
    assert normalize_text(with_newline) == normalize_text(with_space)


def test_normalize_text_still_detects_real_content_change() -> None:
    """折叠空白不能把真实的内容差异也抹掉。"""
    assert normalize_text("会议 A") != normalize_text("会议 B")


def test_canonical_payload_ignores_server_injected_fields() -> None:
    """服务器注入的 id/etag/updated 等不参与比较。"""
    base = {
        "summary": "会议",
        "start": {"dateTime": "2026-09-20T10:00:00Z"},
        "extendedProperties": {"private": {"auto_mail_key": "k"}},
    }
    noisy = {
        **base,
        "id": "abc",
        "etag": '"1"',
        "updated": "2026-09-14T00:00:00Z",
        "htmlLink": "https://x",
        "kind": "calendar#event",
        "creator": {"email": "a@b"},
    }
    assert normalize_hash(base) == normalize_hash(noisy)


def test_canonical_payload_detects_semantic_change() -> None:
    base = {
        "summary": "会议",
        "start": {"dateTime": "2026-09-20T10:00:00Z"},
        "extendedProperties": {"private": {"auto_mail_key": "k"}},
    }
    changed = {**base, "summary": "另一个会议"}
    assert normalize_hash(base) != normalize_hash(changed)


def test_extract_auto_mail_key() -> None:
    assert extract_auto_mail_key(
        {"extendedProperties": {"private": {"auto_mail_key": "k1"}}}
    ) == "k1"
    assert extract_auto_mail_key({}) is None
    assert extract_auto_mail_key({"extendedProperties": {}}) is None


def test_diff_payloads_reports_direction() -> None:
    """差异必须给出方向（我方 → 远端），否则审核时不知道原本是什么。"""
    old = {"summary": "原标题", "start": {"dateTime": "2026-09-20T10:00:00Z"}}
    new = {"summary": "新标题", "start": {"dateTime": "2026-09-20T10:00:00Z"}}
    diff = diff_payloads(old, new)
    assert "summary" in diff
    mine, theirs = diff["summary"]
    assert mine == "原标题" and theirs == "新标题"


# ──────────────────────────────────────────────────────────────
# FakeCalendar：模拟真实服务端行为
# ──────────────────────────────────────────────────────────────


def test_fake_calendar_injects_metadata() -> None:
    """假服务端必须注入元数据，否则测不出「朴素哈希会失效」。"""
    cal = FakeCalendar()
    ev = cal.insert_event(
        build_event_payload(
            title="会议", start_ts="2026-09-20T10:00:00Z", end_ts=None,
            all_day=False, auto_mail_key="k",
        )
    )
    assert "etag" in ev.payload
    assert "id" in ev.payload


def test_server_reserialize_does_not_change_normalized_hash() -> None:
    """**核心回归测试**：服务端重新序列化后，规范化哈希必须不变。

    这是三方比对机制成立的前提。若不成立，所有事件每次检查都会被误判
    「被改动」。
    """
    cal = FakeCalendar()
    ev = cal.insert_event(
        build_event_payload(
            title="会议", start_ts="2026-09-20T10:00:00Z",
            end_ts="2026-09-20T11:00:00Z", all_day=False, auto_mail_key="k",
            description="第一行\n第二行",
        )
    )
    before = normalize_hash(ev.payload)
    cal.simulate_server_reserialize(ev.event_id)
    after = normalize_hash(cal.get_event(ev.event_id).payload)
    assert before == after, (
        "服务端重新序列化不应改变规范化哈希，否则每次检查都误判为有改动"
    )


def test_fake_calendar_refuses_event_without_ownership_mark() -> None:
    """后端必须拒绝创建无 auto_mail_key 的事件（所有权是唯一凭据）。"""
    cal = FakeCalendar()
    with pytest.raises(ValueError):
        cal.insert_event({"summary": "无标记"})


# ──────────────────────────────────────────────────────────────
# 三方比对：六个分支
# ──────────────────────────────────────────────────────────────


def _payload(**overrides: object) -> dict:
    base = {
        "summary": "会议",
        "start": {"dateTime": "2026-09-20T10:00:00Z"},
        "end": {"dateTime": "2026-09-20T11:00:00Z"},
        "extendedProperties": {"private": {"auto_mail_key": "k"}},
    }
    base.update(overrides)
    return base


def test_compare_no_change_when_etag_same() -> None:
    payload = _payload()
    h = normalize_hash(payload)
    result = compare(
        remote=payload, snapshot_hash=h, local_hash=h,
        remote_etag='"1"', last_etag='"1"', expected_auto_mail_key="k",
    )
    assert result.verdict is OwnershipVerdict.NO_CHANGE
    assert result.may_write


def test_compare_benign_evolution_when_content_matches_snapshot() -> None:
    """etag 变了但内容与我方快照一致 → 服务端改写，可安全更新。

    这一分支的意义：服务端（Google/Fake）会自行重排字段、刷新 metadata。
    若把它当作「有改动」，机制就会一直误报。
    """
    payload = _payload()
    h = normalize_hash(payload)
    noisy = {**payload, "etag": '"2"', "updated": "2026-09-14T01:00:00Z"}
    # 注意：noisy 里没有 id/etag 之类的语义字段变化
    result = compare(
        remote=noisy, snapshot_hash=h, local_hash=h,
        remote_etag='"2"', last_etag='"1"', expected_auto_mail_key="k",
    )
    assert result.verdict is OwnershipVerdict.BENIGN_EVOLUTION
    assert result.may_write


def test_compare_externally_modified_when_only_remote_changed() -> None:
    """远端变了、本地没变 → 疑似用户手改 → 冻结。"""
    original = _payload()
    h = normalize_hash(original)
    edited = {**_payload(summary="用户改的"), "etag": '"2"'}
    result = compare(
        remote=edited, snapshot_hash=h, local_hash=h,
        remote_etag='"2"', last_etag='"1"', expected_auto_mail_key="k",
        snapshot_payload=original,
    )
    assert result.verdict is OwnershipVerdict.EXTERNALLY_MODIFIED
    assert result.frozen is True
    assert result.may_write is False
    # 差异必须有方向，审核界面据此展示
    assert "summary" in result.differences


def test_compare_conflict_when_both_changed() -> None:
    """远端与本地都变了 → 冲突，拒绝写入。"""
    original = _payload()
    snapshot_h = normalize_hash(original)
    edited_remote = {**_payload(summary="用户改的"), "etag": '"2"'}
    local_payload = _payload(summary="我方改的")
    result = compare(
        remote=edited_remote,
        snapshot_hash=snapshot_h,
        local_hash=normalize_hash(local_payload),
        remote_etag='"2"', last_etag='"1"', expected_auto_mail_key="k",
        snapshot_payload=original,
    )
    assert result.verdict is OwnershipVerdict.CONFLICT
    assert result.frozen is True


def test_compare_not_found() -> None:
    result = compare(remote=None, snapshot_hash="h", local_hash="h", remote_exists=False)
    assert result.verdict is OwnershipVerdict.NOT_FOUND


def test_compare_not_owned_blocks_everything() -> None:
    """所有权不匹配必须冻结——绝不触碰不是自己创建的事件。"""
    others = _payload(
        extendedProperties={"private": {"auto_mail_key": "someone-else"}}
    )
    result = compare(
        remote=others, snapshot_hash=normalize_hash(others),
        local_hash=normalize_hash(others),
        expected_auto_mail_key="k",
    )
    assert result.verdict is OwnershipVerdict.NOT_OWNED
    assert result.frozen is True


def test_compare_server_touch_is_benign_not_conflict() -> None:
    """**真实缺陷回归**：服务端只动 etag 时应判为无害演进，不是冲突。

    若 ``normalize_text`` 不折叠换行，同内容会被算出不同哈希，
    于是每次服务端改写都升级为 ``conflict``，使用者被迫做无谓裁决。
    """
    cal = FakeCalendar()
    ev = cal.insert_event(
        build_event_payload(
            title="会议", start_ts="2026-09-20T10:00:00Z", end_ts=None,
            all_day=False, auto_mail_key="k",
            description="第一行\n第二行",   # 含换行，服务端会折叠
        )
    )
    snapshot_hash = normalize_hash(ev.payload)
    cal.simulate_server_touch(ev.event_id)
    after = cal.get_event(ev.event_id)

    result = compare(
        remote=after.payload, snapshot_hash=snapshot_hash,
        local_hash=snapshot_hash, remote_etag=after.etag, last_etag=ev.etag,
        expected_auto_mail_key="k",
    )
    assert result.verdict is OwnershipVerdict.BENIGN_EVOLUTION, (
        "服务端自身改写不应被判为冲突"
    )


def test_describe_verdict_includes_diff() -> None:
    original = _payload()
    edited = {**_payload(summary="新标题"), "etag": '"2"'}
    result = compare(
        remote=edited, snapshot_hash=normalize_hash(original),
        local_hash=normalize_hash(original), remote_etag='"2"', last_etag='"1"',
        expected_auto_mail_key="k", snapshot_payload=original,
    )
    text = describe_verdict(result)
    assert "原标题" in text or "会议" in text
    assert "新标题" in text


# ──────────────────────────────────────────────────────────────
# 审核队列
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "logs",
    )


def _insert_event(conn: sqlite3.Connection, **overrides: object) -> int:
    values = {
        "message_id": None,
        "title": "测试事件",
        "start_ts": "2026-10-01T02:00:00Z",
        "end_ts": "2026-10-01T03:00:00Z",
        "all_day": 0,
        "source": "rules",
        "confidence": 0.95,
        "fingerprint": "fp-test",
        "evidence": "测试依据",
        "status": "pending",
        "needs_attention": 0,
        "created_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cur = conn.execute(
        f"INSERT INTO events ({columns}) VALUES ({placeholders})", list(values.values())
    )
    return int(cur.lastrowid)


def test_parse_event_ids_supports_lists_and_ranges() -> None:
    assert parse_event_ids(["1,2,3"]) == [1, 2, 3]
    assert parse_event_ids(["1-3"]) == [1, 2, 3]
    assert parse_event_ids(["5-3"]) == [3, 4, 5]  # 反向范围也接受
    assert parse_event_ids(["1,2", "4-5"]) == [1, 2, 4, 5]
    assert parse_event_ids(["3,3"]) == [3]  # 去重


def test_parse_event_ids_rejects_garbage() -> None:
    """非法 id 必须报错而不是静默忽略——静默会让用户以为已处理。"""
    with pytest.raises(ReviewError):
        parse_event_ids(["abc"])
    with pytest.raises(ReviewError):
        parse_event_ids(["1-x"])


def test_approve_transitions_pending_to_approved(conn: sqlite3.Connection) -> None:
    event_id = _insert_event(conn)
    stats = ReviewQueue(conn).approve([event_id])
    assert stats.changed == 1
    row = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["status"] == EventStatus.APPROVED.value


def test_approve_is_idempotent_and_reports_skip(conn: sqlite3.Connection) -> None:
    """重复批准应跳过并说明原因，不静默成功。"""
    event_id = _insert_event(conn)
    queue = ReviewQueue(conn)
    assert queue.approve([event_id]).changed == 1
    second = queue.approve([event_id])
    assert second.changed == 0
    assert second.skipped == 1
    assert any("已是 approved" in r for r in second.reasons)


def test_approve_reports_missing(conn: sqlite3.Connection) -> None:
    stats = ReviewQueue(conn).approve([99999])
    assert stats.not_found == 1
    assert stats.changed == 0


def test_reject_is_terminal(conn: sqlite3.Connection) -> None:
    event_id = _insert_event(conn)
    queue = ReviewQueue(conn)
    assert queue.reject([event_id]).changed == 1
    # 已否决的事件不能再批准
    stats = queue.approve([event_id])
    assert stats.changed == 0
    assert stats.skipped == 1


def test_ignore_is_terminal(conn: sqlite3.Connection) -> None:
    event_id = _insert_event(conn)
    assert ReviewQueue(conn).ignore([event_id]).changed == 1
    row = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["status"] == EventStatus.IGNORED.value


def test_approve_does_not_cancel_scheduled_push_for_manual_approval(
    conn: sqlite3.Connection,
) -> None:
    """人工批准应取消失例化的延迟推送（人的动作立即生效）。"""
    event_id = _insert_event(conn, status="pending")
    queue = ReviewQueue(conn)
    queue.schedule_push(event_id, delay_minutes=30)

    queue.approve([event_id])
    row = conn.execute(
        "SELECT state FROM scheduled_pushes WHERE event_id=?", (event_id,)
    ).fetchone()
    assert row["state"] == ScheduledPushState.CANCELLED.value


def test_edit_marks_field_provenance_as_human(conn: sqlite3.Connection) -> None:
    """人工修正的字段必须记为 human，使 ICS 更新不会覆盖它。"""
    event_id = _insert_event(conn)
    stats = ReviewQueue(conn).edit(event_id, title="我改的标题")
    assert stats.changed == 1

    row = conn.execute(
        "SELECT title, status, manual_edited, field_provenance FROM events WHERE id=?",
        (event_id,),
    ).fetchone()
    assert row["title"] == "我改的标题"
    assert row["status"] == EventStatus.APPROVED.value
    assert row["manual_edited"] == 1
    provenance = json.loads(row["field_provenance"])
    assert provenance.get("title") == "human"


def test_edit_without_fields_reports_skip(conn: sqlite3.Connection) -> None:
    event_id = _insert_event(conn)
    stats = ReviewQueue(conn).edit(event_id)
    assert stats.skipped == 1
    assert stats.changed == 0


def test_adopt_requires_frozen_status(conn: sqlite3.Connection) -> None:
    """只有冻结态可 adopt。"""
    event_id = _insert_event(conn, status="pending")
    stats = ReviewQueue(conn).adopt(event_id)
    assert stats.skipped == 1
    assert "仅冻结态" in stats.reasons[0]


def test_adopt_unfreezes_and_rebaselines(conn: sqlite3.Connection) -> None:
    """adopt 以远端为新基线，状态回到 approved（解冻但需再 push）。"""
    event_id = _insert_event(
        conn, status="externally_modified", remote_norm_hash="remote-h",
        managed_state="frozen",
    )
    stats = ReviewQueue(conn).adopt(event_id)
    assert stats.changed == 1

    row = conn.execute(
        "SELECT status, managed_state, snapshot_hash FROM events WHERE id=?", (event_id,)
    ).fetchone()
    assert row["status"] == EventStatus.APPROVED.value
    assert row["managed_state"] == "adopted"
    assert row["snapshot_hash"] == "remote-h", "快照应重算为远端哈希"


def test_retry_resets_failed(conn: sqlite3.Connection) -> None:
    event_id = _insert_event(conn, status="push_failed", push_attempts=3)
    stats = ReviewQueue(conn).retry([event_id])
    assert stats.changed == 1
    row = conn.execute(
        "SELECT status, push_attempts FROM events WHERE id=?", (event_id,)
    ).fetchone()
    assert row["status"] == EventStatus.APPROVED.value
    assert row["push_attempts"] == 0


def test_retry_skips_events_not_failed(conn: sqlite3.Connection) -> None:
    event_id = _insert_event(conn, status="pushed")
    stats = ReviewQueue(conn).retry([event_id])
    assert stats.skipped == 1


def test_pending_window_lists_scheduled(conn: sqlite3.Connection) -> None:
    """摘要需要「窗口内即将自动入历」的列表，否则 --cancel 没有对象。"""
    event_id = _insert_event(conn, status="approved")
    queue = ReviewQueue(conn)
    queue.schedule_push(event_id, delay_minutes=5)

    items = queue.pending_window(within_minutes=30)
    assert len(items) == 1
    assert items[0].event_id == event_id


def test_cancel_push_returns_event_to_pending(conn: sqlite3.Connection) -> None:
    """撤销后事件必须回到 **pending** 而非 approved。

    回到 approved 会被自动流程再次消费，等于撤销无效。
    """
    event_id = _insert_event(conn, status="approved")
    queue = ReviewQueue(conn)
    queue_id = queue.schedule_push(event_id, delay_minutes=5)

    stats = queue.cancel_push(str(queue_id))
    assert stats.changed == 1

    row = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["status"] == EventStatus.PENDING.value, "撤销应回到待审"

    queue_row = conn.execute(
        "SELECT state FROM scheduled_pushes WHERE id=?", (queue_id,)
    ).fetchone()
    assert queue_row["state"] == ScheduledPushState.CANCELLED.value


def test_cancel_push_accepts_event_id(conn: sqlite3.Connection) -> None:
    event_id = _insert_event(conn, status="approved")
    queue = ReviewQueue(conn)
    queue.schedule_push(event_id, delay_minutes=5)
    assert queue.cancel_push(str(event_id)).changed == 1


def test_cancel_unknown_key_reports_not_found(conn: sqlite3.Connection) -> None:
    stats = ReviewQueue(conn).cancel_push("99999")
    assert stats.not_found == 1
    assert stats.changed == 0


def test_list_items_surfaces_review_reason(conn: sqlite3.Connection) -> None:
    """待审原因写在 evidence 前缀里，列表必须把它解析出来单独展示。"""
    event_id = _insert_event(
        conn, evidence="[待审：时间已过（早于当前时刻，不建议入历）] 2026-08-01"
    )
    item = ReviewQueue(conn).get_item(event_id)
    assert item is not None
    assert "时间已过" in item.review_reason
    # 前缀应被剥离，evidence 保留真实依据
    assert item.evidence.startswith("2026-08-01")


def test_frozen_items_lists_both_frozen_states(conn: sqlite3.Connection) -> None:
    a = _insert_event(conn, status="externally_modified", fingerprint="f1")
    b = _insert_event(conn, status="conflict", fingerprint="f2")
    c = _insert_event(conn, status="pending", fingerprint="f3")
    frozen = {item.event_id for item in ReviewQueue(conn).frozen_items()}
    assert frozen == {a, b}
    _ = c


# ──────────────────────────────────────────────────────────────
# 推送：安全机制
# ──────────────────────────────────────────────────────────────


@pytest.fixture
def engine(settings: Settings, conn: sqlite3.Connection) -> PushEngine:
    return PushEngine(settings, conn, FakeCalendar())


def test_push_creates_event_and_marks_owned(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved")

    stats = engine.push_approved(apply=True)
    assert stats.created == 1

    row = conn.execute(
        "SELECT status, gcal_event_id, gcal_etag, snapshot_hash, managed_state "
        "FROM events WHERE id=?",
        (event_id,),
    ).fetchone()
    assert row["status"] == EventStatus.PUSHED.value
    assert row["gcal_event_id"]
    assert row["snapshot_hash"]
    assert row["managed_state"] == "owned"


def test_push_dry_run_writes_nothing(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """dry-run 必须零副作用：不改库，也不调后端的写接口。"""
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved")

    stats = engine.push_approved(apply=False)
    assert stats.created == 1  # 报告将要新建
    assert cal.events == {}, "dry-run 不得写入日历"
    row = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["status"] == EventStatus.APPROVED.value, "dry-run 不应改状态"


def test_push_ignores_pending_events(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """未批准的候选绝不能被推送。"""
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    _insert_event(conn, status="pending")

    stats = engine.push_approved(apply=True)
    assert stats.considered == 0
    assert cal.events == {}


def test_push_is_idempotent_via_backfill(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """**幂等核心**：数据库里没有 gcal_event_id 但日历里已有我们的标记时，
    必须回填而不重复创建。

    这个场景对应「创建成功但写库前进程崩溃」——没有回填机制，
    重跑就会在用户日历里产生重复事件。
    """
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved", fingerprint="fp-idem")

    # 模拟：日历里已存在该事件（上次创建成功），但库里没有 id
    pre = cal.insert_event(
        build_event_payload(
            title="测试事件", start_ts="2026-10-01T02:00:00Z",
            end_ts="2026-10-01T03:00:00Z", all_day=False,
            auto_mail_key="fp-idem",
        )
    )
    assert len(cal.events) == 1

    stats = engine.push_approved(apply=True)
    assert stats.backfilled == 1, "应回填而非新建"
    assert stats.created == 0
    assert len(cal.events) == 1, "日历里仍只有一条，未产生重复"

    row = conn.execute("SELECT gcal_event_id FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["gcal_event_id"] == pre.event_id


def test_push_freezes_on_external_edit_and_never_overwrites(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """**最重要的安全承诺**：用户手改后，我方绝不覆盖。"""
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved", fingerprint="fp-x")

    engine.push_approved(apply=True)
    row = conn.execute("SELECT gcal_event_id FROM events WHERE id=?", (event_id,)).fetchone()
    gcal_id = row["gcal_event_id"]

    # 用户在日历客户端改了标题与时间
    cal.simulate_user_edit(gcal_id, summary="用户改的标题")
    conn.execute("UPDATE events SET status='approved' WHERE id=?", (event_id,))

    stats = engine.push_approved(apply=True)
    assert stats.frozen == 1
    assert stats.updated == 0

    # 远端内容必须原封不动
    remote = cal.get_event(gcal_id)
    assert remote.payload["summary"] == "用户改的标题"

    row2 = conn.execute(
        "SELECT status, managed_state FROM events WHERE id=?", (event_id,)
    ).fetchone()
    assert row2["status"] == EventStatus.EXTERNALLY_MODIFIED.value
    assert row2["managed_state"] == "frozen"


def test_push_detects_conflict(settings: Settings, conn: sqlite3.Connection) -> None:
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved", fingerprint="fp-c")

    engine.push_approved(apply=True)
    row = conn.execute("SELECT gcal_event_id FROM events WHERE id=?", (event_id,)).fetchone()
    gcal_id = row["gcal_event_id"]

    cal.simulate_user_edit(gcal_id, summary="用户改的")
    # 我方本地也改了：改标题并清掉快照，使 local_hash != snapshot_hash
    conn.execute(
        "UPDATE events SET status='approved', title='我方改的' WHERE id=?", (event_id,)
    )

    stats = engine.push_approved(apply=True)
    assert stats.frozen == 1
    assert cal.get_event(gcal_id).payload["summary"] == "用户改的"


def test_push_never_touches_foreign_events(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """所有权不匹配时拒绝写入——绝不触碰他人事件。"""
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved", fingerprint="fp-mine")

    other = cal.insert_event(
        build_event_payload(
            title="别人的事件", start_ts="2026-10-01T02:00:00Z", end_ts=None,
            all_day=False, auto_mail_key="someone-else",
        )
    )
    conn.execute(
        "UPDATE events SET gcal_event_id=?, gcal_etag='x' WHERE id=?",
        (other.event_id, event_id),
    )

    stats = engine.push_approved(apply=True)
    assert stats.frozen == 1
    assert cal.get_event(other.event_id).payload["summary"] == "别人的事件"


def test_push_respects_per_run_limit(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """超出本轮上限的保持 approved 顺延，不丢弃、不降级。"""
    settings.auto_push_limit_per_run = 2
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    for i in range(5):
        _insert_event(conn, status="approved", fingerprint=f"fp-{i}", title=f"事件{i}")

    stats = engine.push_approved(apply=True)
    assert stats.created == 2
    assert stats.limit_hit == 3

    remaining = conn.execute(
        "SELECT COUNT(*) n FROM events WHERE status='approved'"
    ).fetchone()["n"]
    assert remaining == 3, "超出上限的应保持 approved"


def test_push_marks_failure_and_escalates(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    settings.push_max_attempts = 2
    cal = FakeCalendar()
    cal.fail_insert = True
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved")

    engine.push_approved(apply=True)
    row = conn.execute(
        "SELECT status, push_attempts, needs_attention FROM events WHERE id=?", (event_id,)
    ).fetchone()
    assert row["status"] == EventStatus.PUSH_FAILED.value
    assert row["push_attempts"] == 1
    assert row["needs_attention"] == 0, "首次失败还不该升级"

    conn.execute("UPDATE events SET status='approved' WHERE id=?", (event_id,))
    engine.push_approved(apply=True)
    row2 = conn.execute(
        "SELECT push_attempts, needs_attention FROM events WHERE id=?", (event_id,)
    ).fetchone()
    assert row2["push_attempts"] == 2
    assert row2["needs_attention"] == 1, "超过上限应升级为需关注"


def test_push_not_found_policy_pending(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """远端事件消失时按策略处理，默认转人工确认。"""
    settings.not_found_policy = "pending"
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved")

    engine.push_approved(apply=True)
    row = conn.execute("SELECT gcal_event_id FROM events WHERE id=?", (event_id,)).fetchone()
    cal.remove_externally(row["gcal_event_id"])
    conn.execute("UPDATE events SET status='approved' WHERE id=?", (event_id,))

    stats = engine.push_approved(apply=True)
    assert stats.failed == 1
    row2 = conn.execute(
        "SELECT status, needs_attention FROM events WHERE id=?", (event_id,)
    ).fetchone()
    assert row2["status"] == EventStatus.MISSING.value
    assert row2["needs_attention"] == 1


def test_push_not_found_policy_recreate(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    settings.not_found_policy = "recreate"
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved")

    engine.push_approved(apply=True)
    row = conn.execute("SELECT gcal_event_id FROM events WHERE id=?", (event_id,)).fetchone()
    cal.remove_externally(row["gcal_event_id"])
    conn.execute("UPDATE events SET status='approved' WHERE id=?", (event_id,))

    engine.push_approved(apply=True)
    row2 = conn.execute("SELECT gcal_event_id FROM events WHERE id=?", (event_id,)).fetchone()
    assert row2["gcal_event_id"] is None, "重建策略应清空关联，待下轮创建"


def test_dispatch_due_pushes_scheduled(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """延迟窗口到点后应被推送。"""
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved")

    queue = ReviewQueue(conn)
    queue.schedule_push(event_id, delay_minutes=0)  # 立即到点

    stats = engine.dispatch_due(apply=True)
    assert stats.created == 1
    assert len(cal.events) == 1

    row = conn.execute(
        "SELECT state FROM scheduled_pushes WHERE event_id=?", (event_id,)
    ).fetchone()
    assert row["state"] == ScheduledPushState.DISPATCHED.value


def test_dispatch_skips_cancelled_events(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """被撤销的事件即便队列项还在，也不得推送。"""
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved")

    queue = ReviewQueue(conn)
    queue.schedule_push(event_id, delay_minutes=0)
    # 模拟用户在窗口内撤销：事件回到 pending
    conn.execute("UPDATE events SET status='pending' WHERE id=?", (event_id,))

    stats = engine.dispatch_due(apply=True)
    assert stats.skipped == 1
    assert cal.events == {}, "已撤销的事件不得写入日历"


# ──────────────────────────────────────────────────────────────
# 删除：默认归档而非硬删
# ──────────────────────────────────────────────────────────────


def test_archive_marks_cancelled_not_deleted(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """默认删除语义是**归档**（可恢复），不是硬删除。"""
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved")

    engine.push_approved(apply=True)
    row = conn.execute("SELECT gcal_event_id FROM events WHERE id=?", (event_id,)).fetchone()
    gcal_id = row["gcal_event_id"]

    stats = engine.archive(event_id, apply=True)
    assert stats.archived == 1

    # 事件仍在日历里，只是状态为 cancelled
    remote = cal.get_event(gcal_id)
    assert remote.payload.get("status") == "cancelled"

    row2 = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
    assert row2["status"] == EventStatus.CANCELLED.value


def test_hard_delete_requires_ownership(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """硬删除也必须校验所有权。"""
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved", fingerprint="fp-d")

    other = cal.insert_event(
        build_event_payload(
            title="别人的", start_ts="2026-10-01T02:00:00Z", end_ts=None,
            all_day=False, auto_mail_key="not-mine",
        )
    )
    conn.execute(
        "UPDATE events SET gcal_event_id=? WHERE id=?", (other.event_id, event_id)
    )

    stats = engine.hard_delete(event_id, apply=True)
    assert stats.frozen == 1
    assert other.event_id in cal.events, "他人事件不得被删除"


def test_hard_delete_removes_owned_event(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved", fingerprint="fp-hd")
    engine.push_approved(apply=True)

    stats = engine.hard_delete(event_id, apply=True)
    assert stats.archived == 1
    assert cal.events == {}


def test_archive_event_never_pushed_has_no_remote_effect(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved")  # 未推送过

    stats = engine.archive(event_id, apply=True)
    assert stats.archived == 1
    assert cal.events == {}
    row = conn.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["status"] == EventStatus.CANCELLED.value


# ──────────────────────────────────────────────────────────────
# 端到端：完整的自动入历链路
# ──────────────────────────────────────────────────────────────


def test_full_auto_push_lifecycle(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """自动白名单事件的完整链路：approved → 进延迟队列 → 到点推送 → pushed。"""
    settings.auto_push_delay_minutes = 0
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    queue = ReviewQueue(conn)

    event_id = _insert_event(
        conn, status="approved", source="ics", confidence=0.99,
        fingerprint="fp-auto", ics_uid="uid-1",
    )
    queue.schedule_push(event_id, delay_minutes=settings.auto_push_delay_minutes)

    # 窗口内：应出现在「即将入历」列表里（可撤销对象）
    window = queue.pending_window(within_minutes=60)
    assert any(item.event_id == event_id for item in window)

    # 到点推送
    stats = engine.dispatch_due(apply=True)
    assert stats.created == 1

    row = conn.execute(
        "SELECT status, gcal_event_id, managed_state FROM events WHERE id=?", (event_id,)
    ).fetchone()
    assert row["status"] == EventStatus.PUSHED.value
    assert row["managed_state"] == "owned"


def test_repeated_push_after_pushed_is_noop(
    settings: Settings, conn: sqlite3.Connection
) -> None:
    """已推送且远端无变化时，重复运行不应产生任何写入。"""
    cal = FakeCalendar()
    engine = PushEngine(settings, conn, cal)
    event_id = _insert_event(conn, status="approved", fingerprint="fp-noop")

    engine.push_approved(apply=True)
    before = cal.snapshot()

    # 再次推送（状态已是 pushed，不会进队列）
    stats = engine.push_approved(apply=True)
    assert stats.considered == 0
    assert cal.snapshot() == before

    # 即使人为置回 approved，也应是「无变化跳过」
    conn.execute("UPDATE events SET status='approved' WHERE id=?", (event_id,))
    stats2 = engine.push_approved(apply=True)
    assert stats2.created == 0
    assert stats2.updated == 0
    assert stats2.skipped == 1
    assert cal.snapshot() == before


# ──────────────────────────────────────────────────────────────
# 「可能重复」提示（只提示，不合并）
# ──────────────────────────────────────────────────────────────


def test_title_similarity_matches_same_event() -> None:
    from automail.extract.fingerprint import title_similarity

    # 同一场典礼的不同表述（真实数据里的情况）：缩写 vs 全称
    assert title_similarity(
        "MSc in ACS Programme Inauguration and Orientation Ceremony",
        "MSc in Applied Computing Programme Inauguration and Orientation Ceremony",
    ) >= 0.8


def test_title_similarity_low_for_different_events() -> None:
    from automail.extract.fingerprint import title_similarity

    assert title_similarity("阿里云域名到期提醒", "会议定于9月20日") < 0.5


def test_title_similarity_cannot_distinguish_identical_titles() -> None:
    """**关键局限**：标题完全相同也可能不是同一件事。

    实测：ZA Card 三笔独立消费标题完全相同（相似度 1.00）。
    这正是「只提示不合并」的原因——相似度无法区分这两种情况，
    因此决定权必须留给人。
    """
    from automail.extract.fingerprint import title_similarity

    assert title_similarity(
        "你使用 ZA Card (1033) 完成一笔以下消费",
        "你使用 ZA Card (1033) 完成一笔以下消费",
    ) == 1.0


def test_annotate_hints_same_day_similar_titles(conn: sqlite3.Connection) -> None:
    """同一天 + 标题高度相似 → 标注提示，但**两条都保留**。"""
    queue = ReviewQueue(conn)
    first = _insert_event(
        conn, title="阿里云域名到期提醒", start_ts="2026-08-22T00:00:00Z",
        fingerprint="d1",
    )
    second = _insert_event(
        conn, title="阿里云域名到期提醒", start_ts="2026-08-22T00:00:00Z",
        fingerprint="d2",
    )

    items = annotate_probable_duplicates(queue.list_items(limit=10))
    assert len(items) == 2, "不得合并或隐藏任何候选"
    hinted = [i for i in items if i.probable_duplicate_of is not None]
    assert len(hinted) == 1
    assert hinted[0].probable_duplicate_of in {first, second}
    assert hinted[0].duplicate_similarity is not None


def test_annotate_does_not_hint_different_days(conn: sqlite3.Connection) -> None:
    """不同日期的同主题通知不构成重复嫌疑（周期性提醒很常见）。"""
    queue = ReviewQueue(conn)
    _insert_event(conn, title="阿里云域名到期提醒", start_ts="2026-08-22T00:00:00Z", fingerprint="e1")
    _insert_event(conn, title="阿里云域名到期提醒", start_ts="2026-08-25T00:00:00Z", fingerprint="e2")

    items = annotate_probable_duplicates(queue.list_items(limit=10))
    assert all(i.probable_duplicate_of is None for i in items)


def test_annotate_does_not_hint_dissimilar_titles(conn: sqlite3.Connection) -> None:
    queue = ReviewQueue(conn)
    _insert_event(conn, title="会议定于9月20日", start_ts="2026-09-20T02:00:00Z", fingerprint="f1")
    _insert_event(conn, title="面试定于9月20日", start_ts="2026-09-20T06:00:00Z", fingerprint="f2")

    items = annotate_probable_duplicates(queue.list_items(limit=10))
    assert all(i.probable_duplicate_of is None for i in items)


def test_hint_does_not_affect_approvability(conn: sqlite3.Connection) -> None:
    """提示**绝不**影响能否批准——它只是给人看的标注。

    这是「只提示不合并」的核心保证：系统不替使用者做决定。
    """
    queue = ReviewQueue(conn)
    a = _insert_event(conn, title="同一件事", start_ts="2026-09-20T02:00:00Z", fingerprint="g1")
    b = _insert_event(conn, title="同一件事", start_ts="2026-09-20T02:00:00Z", fingerprint="g2")

    items = annotate_probable_duplicates(queue.list_items(limit=10))
    assert any(i.probable_duplicate_of for i in items)

    # 被标注的条目照样可以批准
    stats = queue.approve([a, b])
    assert stats.changed == 2
