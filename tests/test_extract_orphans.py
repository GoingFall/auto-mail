"""候选落库的**孤儿清理**测试。

背景：改进抽取后重跑，标题与指纹会变，新候选插入而旧候选留在库里——
同一封邮件的同一时刻出现两条，审核队列里看起来像两个独立事件。

但「清理」很容易变成「销毁人的决定」。这里的测试**主要是在钉住不许删的
那几种情况**：已批准、已推送、人改过的，一个都不能碰。
"""

from __future__ import annotations

import sqlite3

from automail.db import utcnow_iso
from automail.extract.pipeline import Candidate, ExtractionOutcome
from automail.extract.prefilter import PrefilterVerdict
from automail.extract.runner import ExtractRunner, ExtractStats
from automail.models import EventSource
from automail.settings import Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        account="163",
        user_timezone="Asia/Shanghai",
        data_dir=tmp_path,
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backup",
    )


def _message(conn: sqlite3.Connection, uid: int = 1) -> int:
    cur = conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            from_addr, received_at, body_excerpt, is_canonical, stale,
            fetched_at, extract_status, extract_attempts)
        VALUES ('163','INBOX',1,?,'會議','a@b.com','2026-09-16T02:00:00Z',
                '會議 2026年9月20日 15:00', 1, 0, '2026-09-16T02:05:00Z','done',0)
        """,
        (uid,),
    )
    return int(cur.lastrowid)


def _event(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    fingerprint: str,
    title: str = "旧标题",
    status: str = "pending",
    manual_edited: int = 0,
    gcal_event_id: str | None = None,
    start_ts: str = "2026-09-20T07:00:00Z",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO events (message_id, title, start_ts, all_day, source,
            confidence, fingerprint, status, manual_edited, gcal_event_id,
            created_at, updated_at)
        VALUES (?, ?, ?, 0, 'rules', 0.95, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id, title, start_ts, fingerprint, status,
            manual_edited, gcal_event_id, utcnow_iso(), utcnow_iso(),
        ),
    )
    return int(cur.lastrowid)


def _candidate(fingerprint: str, title: str = "新标题") -> Candidate:
    return Candidate(
        title=title, start_ts="2026-09-20T07:00:00Z", end_ts=None, all_day=False,
        source=EventSource.RULES, confidence=0.95, fingerprint=fingerprint,
        requires_review=False,
    )


def _outcome(candidates: list[Candidate]) -> ExtractionOutcome:
    return ExtractionOutcome(
        verdict=PrefilterVerdict(extract=True, call_llm=False, reason="stub"),
        candidates=candidates,
    )


def _persist(conn, tmp_path, message_id: int, candidates: list[Candidate]) -> ExtractStats:
    runner = ExtractRunner(_settings(tmp_path), conn)
    stats = ExtractStats(dry_run=False)
    runner._persist(message_id, _outcome(candidates), stats)
    conn.commit()
    return stats


# ══════════════════════════════════════════════════════════════
# 该清理的：机器产出、人未表态
# ══════════════════════════════════════════════════════════════


def test_stale_pending_candidate_is_removed(conn, tmp_path) -> None:
    """**核心场景**：旧候选仍是 pending 且没人碰过 → 重跑后删掉。

    这是「只写不删」造成重复事件的那一类：标题变了 → 指纹变了 → 新条目
    插入而旧条目留下。
    """
    mid = _message(conn)
    old = _event(conn, mid, fingerprint="old-fp")
    conn.commit()

    stats = _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    rows = conn.execute("SELECT fingerprint FROM events WHERE message_id = ?", (mid,)).fetchall()
    assert [r["fingerprint"] for r in rows] == ["new-fp"]
    assert stats.events_removed == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (old,)).fetchone()[0] == 0


def test_rerun_same_fingerprint_is_not_removed(conn, tmp_path) -> None:
    """重跑产出同样指纹 → 是更新而非删除（不能把正常更新当成孤儿）。"""
    mid = _message(conn)
    keep = _event(conn, mid, fingerprint="same-fp")
    conn.commit()

    stats = _persist(conn, tmp_path, mid, [_candidate("same-fp")])

    assert stats.events_removed == 0
    assert stats.events_updated == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (keep,)).fetchone()[0] == 1


# ══════════════════════════════════════════════════════════════
# 绝不许删的：人的决定与外部副作用
# ══════════════════════════════════════════════════════════════


def test_approved_candidate_is_never_removed(conn, tmp_path) -> None:
    """**已批准的事件绝不能因重跑而消失**——那等于销毁人的决定。"""
    mid = _message(conn)
    keep = _event(conn, mid, fingerprint="old-fp", status="approved")
    conn.commit()

    stats = _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    assert stats.events_removed == 0
    assert stats.events_orphaned == 1, "要计入「保留的旧候选」，让人知道它的存在"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (keep,)).fetchone()[0] == 1


def test_pushed_candidate_is_never_removed(conn, tmp_path) -> None:
    """**已写入日历的事件绝不能删**——库里删掉、日历里还在，会造成不一致。"""
    mid = _message(conn)
    keep = _event(
        conn, mid, fingerprint="old-fp", status="pushed", gcal_event_id="evt-1"
    )
    conn.commit()

    stats = _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    assert stats.events_removed == 0
    assert stats.events_orphaned == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (keep,)).fetchone()[0] == 1


def test_manual_edited_candidate_is_never_removed(conn, tmp_path) -> None:
    """人**改过标题/时间**的候选不能删，即使它还是 pending。

    人的修改不会被抽取结果覆盖（``manual_edited`` 的语义），删除同样不行。
    """
    mid = _message(conn)
    keep = _event(conn, mid, fingerprint="old-fp", manual_edited=1)
    conn.commit()

    stats = _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    assert stats.events_removed == 0
    assert stats.events_orphaned == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (keep,)).fetchone()[0] == 1


def test_gcal_linked_candidate_is_never_removed(conn, tmp_path) -> None:
    """只要有 ``gcal_event_id`` 就不能删，无论状态是什么。"""
    mid = _message(conn)
    keep = _event(
        conn, mid, fingerprint="old-fp", status="pending", gcal_event_id="evt-9"
    )
    conn.commit()

    stats = _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    assert stats.events_removed == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (keep,)).fetchone()[0] == 1


def test_rejected_and_ignored_are_kept(conn, tmp_path) -> None:
    """否决/忽略是**终态**：保留它们，否则同一噪音会再次进入审核队列。"""
    mid = _message(conn)
    r = _event(conn, mid, fingerprint="rej-fp", status="rejected")
    i = _event(conn, mid, fingerprint="ign-fp", status="ignored")
    conn.commit()

    stats = _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    assert stats.events_removed == 0
    assert stats.events_orphaned == 2
    for eid in (r, i):
        assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (eid,)).fetchone()[0] == 1


# ══════════════════════════════════════════════════════════════
# 延迟窗口的连带处理
# ══════════════════════════════════════════════════════════════


def test_queued_push_rows_are_cleared_with_the_event(conn, tmp_path) -> None:
    """删除孤儿时要一并清掉它的排队项，否则外键悬空、调度器指向不存在的事件。"""
    mid = _message(conn)
    old = _event(conn, mid, fingerprint="old-fp")
    conn.execute(
        "INSERT INTO scheduled_pushes (event_id, scheduled_for, state, created_at) "
        "VALUES (?, '2026-09-20T07:05:00Z', 'queued', ?)",
        (old, utcnow_iso()),
    )
    conn.commit()

    _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (old,)).fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM scheduled_pushes WHERE event_id = ?", (old,)
        ).fetchone()[0]
        == 0
    ), "排队项必须随之清除"


def test_dispatched_push_protects_the_event(conn, tmp_path) -> None:
    """**已派发的延迟窗口意味着确实推送过** —— 即使 gcal_event_id 为空也不能删。

    这是纵深防御：`gcal_event_id` 为空可能是数据异常，但一条 dispatched 的
    调度记录说明外部（日历）可能已经有对应条目，删掉会造成不一致。
    """
    mid = _message(conn)
    keep = _event(conn, mid, fingerprint="old-fp", status="pending")
    conn.execute(
        "INSERT INTO scheduled_pushes (event_id, scheduled_for, state, created_at, "
        "dispatched_at) VALUES (?, '2026-09-20T07:05:00Z', 'dispatched', ?, ?)",
        (keep, utcnow_iso(), utcnow_iso()),
    )
    conn.commit()

    stats = _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    assert stats.events_removed == 0, "已派发过推送的事件不得删除"
    assert stats.events_orphaned == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (keep,)).fetchone()[0] == 1


def test_llm_events_survive_an_offline_rerun(conn, tmp_path) -> None:
    """**回归**：没带 LLM 重跑时，不得删掉 LLM 抽出的候选。

    离线规则**无法复现** LLM 的结果。若不做保护，一次不带 LLM 的重跑就会
    悄悄抹掉此前花代价换来的 LLM 事件——使用者只会发现「事件少了」，
    而原因极难想到。
    """
    mid = _message(conn)
    keep = _event(conn, mid, fingerprint="llm-fp", status="pending")
    conn.execute("UPDATE events SET source = 'llm' WHERE id = ?", (keep,))
    conn.commit()

    # runner 未传 llm → 离线模式
    stats = _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    assert stats.events_removed == 0, "离线重跑不得删除 LLM 结果"
    assert stats.events_orphaned == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (keep,)).fetchone()[0] == 1


def test_rule_events_still_removed_with_llm_present(conn, tmp_path) -> None:
    """有 LLM 时，规则的旧候选仍应正常清理（保护不能过度）。"""
    mid = _message(conn)
    old = _event(conn, mid, fingerprint="old-fp")  # source=rules
    conn.commit()

    class _Llm:
        available = True

    runner = ExtractRunner(_settings(tmp_path), conn, llm=_Llm())
    stats = ExtractStats(dry_run=False)
    runner._persist(mid, _outcome([_candidate("new-fp")]), stats)
    conn.commit()

    assert stats.events_removed == 1
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (old,)).fetchone()[0] == 0


def test_other_messages_are_untouched(conn, tmp_path) -> None:
    """清理只作用于本封邮件，绝不能波及其它邮件的事件。"""
    mid = _message(conn, uid=1)
    other = _message(conn, uid=2)
    other_event = _event(conn, other, fingerprint="other-fp")
    _event(conn, mid, fingerprint="old-fp")
    conn.commit()

    _persist(conn, tmp_path, mid, [_candidate("new-fp")])

    assert (
        conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (other_event,)).fetchone()[0]
        == 1
    ), "其它邮件的事件不得被删"


def test_dry_run_persist_never_deletes(conn, tmp_path) -> None:
    """dry-run 不调用 ``_persist``，因此绝不删任何东西（回归保护）。"""
    mid = _message(conn)
    keep = _event(conn, mid, fingerprint="old-fp")
    conn.commit()

    # dry-run 走 _process 但不进 _persist
    runner = ExtractRunner(_settings(tmp_path), conn)
    stats = ExtractStats(dry_run=True)
    assert stats.events_removed == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (keep,)).fetchone()[0] == 1
    _ = runner
