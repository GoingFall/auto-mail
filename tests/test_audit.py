"""抽取复盘（``automail audit``）的测试。

这个功能的唯一价值是**准确指出该看哪封邮件**，因此测试重点不是「能跑」，
而是「该报的报、不该报的不报」。假警报比漏报更糟：报告一旦充满噪音，
使用者就不再看了，功能等于不存在。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from automail.audit import (
    SUSPECT_LIKELY,
    SUSPECT_MAYBE,
    SUSPECT_NONE,
    render_markdown,
    run_audit,
)
from automail.db import utcnow_iso
from automail.settings import Settings

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=TZ)


def _settings(tmp_path) -> Settings:
    return Settings(
        account="163",
        user_timezone="Asia/Shanghai",
        data_dir=tmp_path,
        out_dir=tmp_path / "out",
        log_dir=tmp_path / "logs",
        backup_dir=tmp_path / "backup",
    )


def _add_message(
    conn: sqlite3.Connection,
    *,
    uid: int,
    subject: str,
    body: str,
    received: str = "2026-09-16T02:00:00Z",
    fetched: str = "2026-09-16T02:05:00Z",
    extract_status: str = "done",
) -> int:
    """插入一封邮件。

    ``fetched`` 必须显式给出（不用 ``utcnow()``）：窗口判定同时看收信与
    同步时间，若用真实当前时间，测试结果就依赖运行时刻。
    """
    cur = conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            from_addr, received_at, body_excerpt, is_canonical, stale,
            fetched_at, extract_status, extract_attempts)
        VALUES ('163','INBOX',1,? ,?, 'a@b.com', ?, ?, 1, 0, ?, ?, 0)
        """,
        (uid, subject, received, body, fetched, extract_status),
    )
    return int(cur.lastrowid)


def _add_event(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    title: str,
    start_ts: str,
    source: str = "rules",
    all_day: bool = False,
    fingerprint: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events (message_id, title, start_ts, all_day, source,
            confidence, fingerprint, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 0.95, ?, 'pending', ?, ?)
        """,
        (
            message_id,
            title,
            start_ts,
            1 if all_day else 0,
            source,
            fingerprint or f"{title}|{start_ts}",
            utcnow_iso(),
            utcnow_iso(),
        ),
    )


# ══════════════════════════════════════════════════════════════
# 窗口选择
# ══════════════════════════════════════════════════════════════


def test_only_messages_inside_window_are_reviewed(conn, tmp_path) -> None:
    """收信与同步都在窗口外的邮件不进入复盘。"""
    inside = _add_message(
        conn, uid=1, subject="窗口内", body="會議 2026年9月20日 15:00 舉行",
        received="2026-09-16T02:00:00Z", fetched="2026-09-16T02:05:00Z",
    )
    _add_message(
        conn, uid=2, subject="窗口外", body="會議 2026年9月20日 15:00 舉行",
        received="2026-09-10T02:00:00Z", fetched="2026-09-10T02:05:00Z",
    )
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    ids = [i.message_id for i in report.items]
    assert ids == [inside], "窗口外的邮件不该进入复盘"


def test_non_canonical_duplicates_are_skipped(conn, tmp_path) -> None:
    """副本不参与抽取，因此也不进复盘——否则同一内容会被报两次。"""
    cur = conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            from_addr, received_at, body_excerpt, is_canonical, duplicate_of,
            stale, fetched_at, extract_status, extract_attempts)
        VALUES ('163','INBOX',1,9,'副本','a@b.com','2026-09-16T02:00:00Z',
                '會議 2026年9月20日 15:00 舉行', 0, 1, 0, '2026-09-16T02:05:00Z',
                'done', 0)
        """,
    )
    conn.commit()
    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    assert int(cur.lastrowid) not in [i.message_id for i in report.items]


def test_messages_synced_late_are_still_reviewed(conn, tmp_path) -> None:
    """**机器休眠期间的邮件不能漏掉**：收信在窗口外、但刚同步进来的要复查。

    场景：笔记本关机两天，开机后同步到积压邮件。它们「收到」在窗口之外，
    却是**质量最可能出问题**的一批（隔了很久才处理，而使用者以为已经看过）。
    只看 ``received_at`` 会让这批邮件永远进不了复盘。
    """
    late = _add_message(
        conn, uid=1, subject="积压邮件", body="會議 2026年9月20日 15:00",
        received="2026-09-10T02:00:00Z",   # 收信在窗口外
        fetched="2026-09-16T02:00:00Z",    # 但刚刚才同步进来
    )
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    assert late in [i.message_id for i in report.items]


def test_old_mail_never_resynced_is_not_reviewed(conn, tmp_path) -> None:
    """收信与同步都在窗口外的历史邮件不该反复出现在复盘里。

    否则库里的陈年邮件每天都会被重报一遍，报告立刻失去价值。
    """
    _add_message(
        conn, uid=1, subject="陈年邮件", body="會議 2026年9月20日 15:00",
        received="2026-08-01T02:00:00Z", fetched="2026-08-01T02:05:00Z",
    )
    conn.commit()
    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    assert report.items == []


# ══════════════════════════════════════════════════════════════
# 可疑判定：该报的必须报
# ══════════════════════════════════════════════════════════════


def test_prefilter_wants_extraction_but_nothing_extracted(conn, tmp_path) -> None:
    """**核心场景**：预筛说「规则够用、不必问 LLM」而规则空手而归。

    这是最危险的一类：我们**主动放弃了最会判断的来源**（LLM），又没有任何
    证据说明真的没有事件——而流程照常报成功。

    构造：``2026年2月30日`` 这种**形态合法但日期非法**的值。预筛只看正则
    形态，判定「日期与时刻同段配对完整、规则足以覆盖」→ 不调 LLM；规则
    构造 ``date()`` 时抛 ``ValueError`` 静默丢弃 → 零候选。
    """
    _add_message(
        conn, uid=1, subject="會議通知",
        body="會議定於 2026年2月30日 15:00 舉行。",
    )
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    item = report.items[0]
    assert item.prefilter_extract is True
    assert item.prefilter_call_llm is False, "预筛误判为「规则够用」"
    assert item.candidate_count == 0
    assert item.suspicion_level == SUSPECT_LIKELY, (
        "放弃 LLM 且规则空手而归 → 最该看的一类"
    )


def test_llm_consulted_and_empty_is_only_maybe(conn, tmp_path) -> None:
    """**已问过 LLM 且它也说没有** → 至多「值得留意」，不该是「很可能漏抽」。

    实测：账单通知、账号升级进度、确认邮箱这类邮件会命中「登記」等宽泛
    关键词而被预筛放行，但正文确实没有可入历的时间。把它们报成「很可能
    漏抽」会让报告充满假警报——而假警报会让人不再看报告，功能等于失效。
    """
    from automail.extract.llm import LlmResult

    _add_message(
        conn, uid=1, subject="賬單通知",
        body="誠邀您查閱本月賬單，詳情請登入網站。",
    )
    conn.commit()

    class _EmptyLlm:
        available = True

        def extract(self, **kwargs: object) -> LlmResult:
            return LlmResult(events=[], ok=True)

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW, llm=_EmptyLlm())
    item = report.items[0]
    assert item.llm_called is True
    assert item.suspicion_level == SUSPECT_MAYBE, "问过 LLM 了，降级为「值得留意」"
    assert report.counts()["likely_missed"] == 0


def test_stale_store_is_reported_as_likely(conn, tmp_path) -> None:
    """库中结果落后于当前代码 → 明确的可疑项（跑 extract --apply 即可对齐）。"""
    _add_message(
        conn, uid=1, subject="會議通知",
        body="會議定於 2026年9月20日 15:00 舉行。",
    )
    # 故意不写事件：模拟「修完缺陷但还没重跑抽取」
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    item = report.items[0]
    assert item.candidate_count == 1
    assert item.missing_from_store, "当前代码会产出候选，库里却没有"
    assert item.suspicion_level == SUSPECT_LIKELY
    assert "过时" in (item.suspicion or "")


def test_store_matching_by_title_similarity_is_not_stale(conn, tmp_path) -> None:
    """库里已有等价事件时不得报「过时」——标题措辞差异不算缺失。

    LLM 每次给的标题可能不同（「升旗禮」vs「升旗禮（迎迓）」），若按指纹
    精确比对会把同一件事误报成缺失，报告立刻失去可信度。
    """
    mid = _add_message(
        conn, uid=1, subject="會議通知",
        body="會議定於 2026年9月20日 15:00 舉行。",
    )
    # 起始时刻与候选一致，标题只差装饰性后缀
    _add_event(
        conn, mid, title="會議（舉行）", start_ts="2026-09-20T07:00:00Z",
        fingerprint="stored-fp",
    )
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    item = report.items[0]
    assert item.candidate_count == 1
    assert item.missing_from_store == [], (
        "标题措辞不同但同一时刻同一件事，不该报「过时」"
    )


def test_llm_candidates_never_count_as_stale(conn, tmp_path) -> None:
    """**回归**：LLM 产出的候选**不参与**过时判定。

    LLM 输出每次都不完全一样：同一封邮件、同一份代码，两次调用的事件划分
    与标题都可能不同。若把 LLM 产物纳入比对，报告每天都会亮——而报告一亮
    就没人看了。

    这里让假 LLM 返回一个库里绝对没有的事件，验证它不会被算成「过时」。
    """
    from automail.extract.llm import LlmEvent, LlmResult

    _add_message(
        conn, uid=1, subject="沒有時間的通知",
        body="誠邀您撥冗出席本次研討會，詳情稍後公布。",
    )
    conn.commit()

    class _Llm:
        available = True

        def extract(self, **kwargs: object) -> LlmResult:
            return LlmResult(
                events=[
                    LlmEvent(
                        title="LLM 獨有事件", start="2026-12-25T10:00:00+08:00",
                        confidence=0.9, evidence="x",
                    )
                ],
                ok=True,
            )

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW, llm=_Llm())
    item = report.items[0]
    assert item.candidate_count == 1, "LLM 候选应出现在报告里"
    assert item.missing_from_store == [], "但不得据此判定「库中结果过时」"


def test_offline_run_skips_stale_check_when_llm_events_exist(conn, tmp_path) -> None:
    """**回归（实测 #92）**：本轮没调 LLM、库中却有 LLM 事件时，跳过过时判定。

    两条流水线此时不等价，比较没有意义。实测场景：离线规则从
    「二零二六年十月一日」抽出**全天**候选，而真实运行时 LLM 给出更精确的
    10:30 定时候选，流水线按「同日有定时则取代全天」丢弃了前者——库里本就
    没有那条全天事件。离线重跑看不到这个取代关系，把它误报成「库中缺失」。

    取舍：宁可漏报（`--with-llm` 复查时再判），不可误报。
    """
    mid = _add_message(
        conn, uid=1, subject="會議通知",
        body="會議定於 2026年9月20日 15:00 舉行。",
    )
    _add_event(
        conn, mid, title="LLM 版本", start_ts="2026-09-20T07:30:00Z",
        source="llm", fingerprint="llm-fp",
    )
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW, llm=None)
    item = report.items[0]
    assert item.llm_called is False
    assert item.missing_from_store == [], "离线模式不得推断「库中过时」"


def test_filtered_mail_with_date_and_time_is_flagged(conn, tmp_path) -> None:
    """被预筛过滤但正文确实含日期与时刻形态 → 可能是误拦，值得留意。"""
    _add_message(
        conn, uid=1, subject="系統通知",
        body="系統將於 2026年9月20日 15:00 進行維護。",
    )
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    item = report.items[0]
    if not item.prefilter_extract:
        assert item.suspicion_level >= SUSPECT_MAYBE
        assert "日期与时刻形态" in (item.suspicion or "")


def test_failed_extraction_is_flagged(conn, tmp_path) -> None:
    """抽取状态为 failed 的邮件必须进可疑列表。

    且原因要指向「上次失败」，不能被「库中结果过时」掩盖——失败的邮件通常
    没有事件，若先走过时分支，报告会误导成「代码变了」。
    """
    _add_message(
        conn, uid=1, subject="坏邮件", body="會議 2026年9月20日 15:00",
        extract_status="failed",
    )
    conn.commit()
    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    item = report.items[0]
    assert item.needs_eyes
    assert "失败" in (item.suspicion or ""), item.suspicion


def test_missing_llm_credentials_are_not_per_mail_suspects(conn, tmp_path) -> None:
    """**回归**：未配 LLM 时，「需要 LLM 兜底」的邮件不得逐封报可疑。

    实测 72 小时窗口里 16 个可疑项有 15 个都是「未配置 LLM」——那是**一个
    全局配置状态**，不是 15 封邮件各自的毛病。逐封报告会让报告被同一句话淹没。

    但它们也不能算「正常」：只计入「未判定」，报告头部说明一次。
    """
    for uid in (1, 2, 3):
        _add_message(
            conn, uid=uid, subject=f"研討會邀請 {uid}",
            body="誠邀您撥冗出席本次研討會，詳情稍後公布。",
        )
    conn.commit()

    # 默认 settings 无 LLM 凭据
    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    assert report.llm_available is False
    assert report.suspect_items == [], "缺 LLM 不该逐封报可疑"
    assert len(report.unjudged_items) == 3, "但要如实计入「未判定」"
    assert report.counts()["unjudged"] == 3

    text = render_markdown(report)
    assert "LLM 未配置" in text
    assert "未判定" in text


def test_unavailable_llm_is_unjudged_not_per_mail_suspect(conn, tmp_path) -> None:
    """**本轮拿不到 LLM**（未配 / 超预算 / 不可用）→ 归「未判定」，不逐封报可疑。

    这是与「调用成功但抽不到」的关键区别：前者是我们的观测缺口，
    报告头部说明一次即可；逐封报会把报告淹掉。
    """
    _add_message(
        conn, uid=1, subject="研討會邀請",
        body="誠邀您撥冗出席本次研討會，詳情稍後公布。",
    )
    conn.commit()

    class _Unavailable:
        available = False

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW, llm=_Unavailable())
    item = report.items[0]
    assert item.llm_called is False
    assert not item.needs_eyes, "观测缺口不该算成这封邮件的问题"
    assert report.counts()["unjudged"] == 1, "但要如实计入未判定"
    assert report.counts()["likely_missed"] == 0


# ══════════════════════════════════════════════════════════════
# 不该报的不能报（假警报会让报告失去价值）
# ══════════════════════════════════════════════════════════════


def test_clean_mail_is_not_flagged(conn, tmp_path) -> None:
    """正常抽出的邮件不该可疑。"""
    mid = _add_message(
        conn, uid=1, subject="會議通知",
        body="會議定於 2026年9月20日 15:00 舉行。",
    )
    _add_event(conn, mid, title="會議", start_ts="2026-09-20T07:00:00Z")
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    item = report.items[0]
    assert item.candidate_count == 1
    assert item.suspicion_level == SUSPECT_NONE
    assert not item.needs_eyes


def test_marketing_mail_without_clues_is_not_flagged(conn, tmp_path) -> None:
    """被正常过滤的营销邮件（无日期时刻形态）不是可疑项。"""
    _add_message(
        conn, uid=1, subject="優惠推廣", body="立即下單享優惠，退訂請點此。",
    )
    conn.commit()
    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    assert report.items[0].suspicion_level == SUSPECT_NONE


def test_audit_never_writes_to_database(conn, tmp_path) -> None:
    """复盘必须**只读**：不改 extract_status、不写事件。"""
    mid = _add_message(
        conn, uid=1, subject="會議通知",
        body="會議定於 2026年9月20日 15:00 舉行。",
        extract_status="pending",
    )
    conn.commit()
    before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    before_status = conn.execute(
        "SELECT extract_status FROM messages WHERE id = ?", (mid,)
    ).fetchone()[0]

    run_audit(_settings(tmp_path), conn, hours=24, now=NOW)

    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events
    assert (
        conn.execute("SELECT extract_status FROM messages WHERE id = ?", (mid,)).fetchone()[0]
        == before_status
    ), "复盘不得改变抽取状态"


# ══════════════════════════════════════════════════════════════
# 报告渲染
# ══════════════════════════════════════════════════════════════


def test_report_lists_suspects_before_others(conn, tmp_path) -> None:
    """可疑项必须排在报告前部——报告是一眼扫的。"""
    _add_message(
        conn, uid=1, subject="可疑邀请",
        body="謹訂於二零二六年十月一日舉行升旗禮。",
    )
    conn.commit()
    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    text = render_markdown(report)
    assert "需要你看一眼" in text
    assert "可疑邀请" in text
    assert text.index("需要你看一眼") < text.index("其余")


def test_report_counts_are_consistent(conn, tmp_path) -> None:
    mid = _add_message(
        conn, uid=1, subject="會議通知",
        body="會議定於 2026年9月20日 15:00 舉行。",
    )
    _add_event(conn, mid, title="會議", start_ts="2026-09-20T07:00:00Z")
    _add_message(conn, uid=2, subject="優惠", body="立即下單享優惠。")
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    counts = report.counts()
    assert counts["messages"] == len(report.items) == 2
    assert counts["with_candidates"] == 1
    assert counts["stale"] == 0


def test_json_output_is_serializable(conn, tmp_path) -> None:
    """JSON 输出必须可直接序列化（供脚本消费）。"""
    import json

    _add_message(
        conn, uid=1, subject="會議通知",
        body="會議定於 2026年9月20日 15:00 舉行。",
    )
    conn.commit()
    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    payload = json.loads(report.to_json())
    assert payload["hours"] == 24
    assert payload["counts"]["messages"] == 1
    assert payload["items"][0]["subject"] == "會議通知"


def test_single_broken_message_does_not_abort_audit(conn, tmp_path) -> None:
    """单封出错不能拖垮整轮复盘。"""
    _add_message(conn, uid=1, subject="正常", body="會議 2026年9月20日 15:00")
    # 正文为 NULL 的极端记录
    conn.execute(
        """
        INSERT INTO messages (account, folder, uid_validity, uid, subject,
            from_addr, received_at, body_excerpt, is_canonical, stale,
            fetched_at, extract_status, extract_attempts)
        VALUES ('163','INBOX',1,2,'空正文','a@b.com','2026-09-16T02:00:00Z',
                NULL, 1, 0, '2026-09-16T02:05:00Z', 'done', 0)
        """,
    )
    conn.commit()

    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    assert len(report.items) == 2, "一封出错不应丢掉其它邮件"


def test_audit_works_without_llm_credentials(conn, tmp_path) -> None:
    """无 LLM 也必须可用——复盘在离线/未配凭据时同样要有结论。"""
    _add_message(
        conn, uid=1, subject="會議通知",
        body="會議定於 2026年9月20日 15:00 舉行。",
    )
    conn.commit()
    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW, llm=None)
    assert report.used_llm is False
    text = render_markdown(report)
    # 未配凭据时报告要明确说明「哪些无法判定」，而不是假装都正常
    assert "LLM 未配置" in text


def test_hours_window_boundaries(conn, tmp_path) -> None:
    """窗口边界：恰好 24 小时前的邮件在窗口内，25 小时前的在外。"""
    exactly = _add_message(
        conn, uid=1, subject="刚好24h", body="會議 2026年9月20日 15:00",
        received="2026-09-15T04:00:00Z", fetched="2026-09-15T04:01:00Z",
    )
    _add_message(
        conn, uid=2, subject="25h前", body="會議 2026年9月20日 15:00",
        received="2026-09-15T03:59:00Z", fetched="2026-09-15T03:59:30Z",
    )
    conn.commit()
    report = run_audit(_settings(tmp_path), conn, hours=24, now=NOW)
    ids = [i.message_id for i in report.items]
    assert exactly in ids
    assert len(ids) == 1


def test_utc_now_is_used_when_not_injected(conn, tmp_path) -> None:
    """不注入 now 时用当前 UTC 时间（真实调用路径不崩）。"""
    _add_message(
        conn, uid=1, subject="最近", body="會議 2026年9月20日 15:00",
        received=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    conn.commit()
    report = run_audit(_settings(tmp_path), conn, hours=24)
    assert report.generated_at
    assert len(report.items) == 1
