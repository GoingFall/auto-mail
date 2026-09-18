"""每日摘要：一份 Markdown 报告，落 ``out/digest-YYYY-MM-DD.md``。

## 设计目标

让使用者**扫一眼就知道今天要处理什么**，不必打开邮箱。因此摘要只放
「需要动作」与「刚发生」的东西，不做信息罗列。

## 内容

1. **今日与近期的日历事件**（读回日历）——今天到底有什么安排
2. **延迟窗口内即将自动入历的事件** ← 必须有，否则 ``--cancel`` 无对象
3. **待审事件**（按时间排序，带来源与依据）
4. **需要关注**（推送失败、冻结、远端消失）
5. **近期邮件概览**（新到、带 ICS、线程）
6. **系统状态**（上次同步、LLM 降级提示）

## 刻意的取舍

* **写入本地文件，不发邮件**。发信是不可逆的对外动作，v1 不做
  （规格 §1：摘要自动投递属 v2）。
* **弱关联线程不作为事实陈述**。主题弱关联只能说「主题相同」，
  不能说「这是一条对话」，更不能说「在等你回复」。
* **降级提示必须出现**。若 LLM 未配置导致抽取能力下降，摘要里要说，
  否则使用者以为这就是全部能力。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import parse_iso, utcnow
from .models import EventStatus
from .review import ReviewQueue
from .sanitize import sanitize_text
from .settings import Settings
from .stats import StatsCollector, StatsSnapshot

logger = logging.getLogger("automail.digest")

#: 摘要里最多列多少条（避免报告长得没人看）
MAX_ITEMS = 25


@dataclass(slots=True)
class DigestContext:
    """生成摘要所需的全部数据（一次性收集，便于测试）。"""

    generated_at: datetime
    snapshot: StatsSnapshot
    today_events: list[dict] = field(default_factory=list)
    upcoming_events: list[dict] = field(default_factory=list)
    window_items: list[object] = field(default_factory=list)
    pending_items: list[object] = field(default_factory=list)
    attention_items: list[object] = field(default_factory=list)
    recent_messages: list[dict] = field(default_factory=list)
    active_threads: list[dict] = field(default_factory=list)
    last_run: dict | None = None
    notes: list[str] = field(default_factory=list)
    calendar_error: str | None = None


class DigestBuilder:
    """构造每日摘要。"""

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        *,
        calendar=None,
        timezone: str | None = None,
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._calendar = calendar
        self._tz = ZoneInfo(timezone or settings.user_timezone)

    # ── 数据收集 ──────────────────────────────────────────

    def collect(self) -> DigestContext:
        now = utcnow()
        snapshot = StatsCollector(self._settings, self._conn).collect()
        queue = ReviewQueue(self._conn)

        context = DigestContext(
            generated_at=now,
            snapshot=snapshot,
            window_items=queue.pending_window(within_minutes=120),
            pending_items=queue.list_items(
                statuses=(EventStatus.PENDING,), limit=MAX_ITEMS
            ),
            attention_items=queue.frozen_items()[:MAX_ITEMS],
            recent_messages=self._recent_messages(limit=MAX_ITEMS),
            active_threads=snapshot.threads_longest,
            last_run=self._last_run(),
        )

        # 推送失败/待确认的也要单列出来
        context.attention_items = [
            *context.attention_items,
            *queue.list_items(
                statuses=(EventStatus.PUSH_FAILED, EventStatus.UNCERTAIN),
                limit=MAX_ITEMS,
            ),
        ]

        # 日历事件：有后端就读回，没有就跳过（并说明原因）
        if self._calendar is not None:
            try:
                events = self._calendar.list_events(limit=250)
                context.today_events, context.upcoming_events = _split_calendar_events(
                    events, now=now, tz=self._tz
                )
            except Exception as exc:  # noqa: BLE001 - 日历读取失败不该让摘要整体失败
                context.calendar_error = sanitize_text(str(exc), limit=200)
                logger.warning("读取日历失败：%s", exc)
        else:
            context.calendar_error = "未接入日历后端（P4 后可用）"

        # 系统提示
        context.notes = self._system_notes(context)
        return context

    def _recent_messages(self, *, limit: int) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT id, subject, from_addr, from_name, received_at, has_ics
              FROM messages
             WHERE account = ? AND is_canonical = 1 AND stale = 0
             ORDER BY received_at DESC, id DESC
             LIMIT ?
            """,
            (self._settings.account, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def _last_run(self) -> dict | None:
        row = self._conn.execute(
            "SELECT run_id, command, started_at, ended_at, ok, exit_code, error "
            "FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def _system_notes(self, context: DigestContext) -> list[str]:
        """系统状态提示——必须包含降级与异常，不能只报好消息。"""
        notes: list[str] = []

        if not self._settings.llm_base_url.strip() or not self._settings.llm_api_key_value:
            notes.append(
                "**LLM 未配置**：抽取运行在「仅规则 + ICS」模式。"
                "有事件词但时间形态不完整的邮件无法被兜底抽取。"
                "配置 `LLM_BASE_URL` 与 `LLM_API_KEY` 可启用。"
            )

        llm_key_missing = not self._settings.google_credentials_file.is_file()
        if llm_key_missing:
            notes.append(
                "**日历未接入**：`credentials.json` 不存在，因此摘要里没有日历事件。"
                "P4 完成后此项可用。"
            )

        failed_sync = context.snapshot.sync_by_status.get("fetch_failed", 0)
        if failed_sync:
            notes.append(f"有 {failed_sync} 封邮件的抓取曾失败（见同步台账）。")

        if context.snapshot.runs_failed:
            notes.append(
                f"历史运行中有 {context.snapshot.runs_failed} 次未成功"
                "（`automail runs` 可查）。"
            )

        stuck = context.snapshot.messages_by_extract_status.get("running", 0)
        if stuck:
            notes.append(
                f"有 {stuck} 封邮件停留在「抽取中」状态——可能是上次运行被中断，"
                "下次 extract 会自动回收。"
            )

        weak = context.snapshot.threads_by_strength.get("weak", 0)
        if weak:
            notes.append(
                f"有 {weak} 个线程是**主题弱关联**（仅凭主题相同推断，"
                "不排除同名不同事）。这类线程不作为事实依据。"
            )

        return notes

    # ── 渲染 ──────────────────────────────────────────────

    def render(self, context: DigestContext) -> str:
        lines: list[str] = []
        local_now = context.generated_at.astimezone(self._tz)

        lines.append(f"# 邮件摘要 · {local_now.strftime('%Y-%m-%d')}")
        lines.append("")
        lines.append(f"> 生成于 {local_now.strftime('%Y-%m-%d %H:%M %Z')}")
        lines.append("")

        self._render_calendar(lines, context)
        self._render_window(lines, context)
        self._render_pending(lines, context)
        self._render_attention(lines, context)
        self._render_messages(lines, context)
        self._render_threads(lines, context)
        self._render_system(lines, context)

        return "\n".join(lines)

    def _render_calendar(self, lines: list[str], context: DigestContext) -> None:
        lines.append("## 日历")
        lines.append("")
        if context.calendar_error:
            lines.append(f"*（无法读取日历：{context.calendar_error}）*")
            lines.append("")
            return

        if context.today_events:
            lines.append("**今天**")
            lines.append("")
            for event in context.today_events:
                lines.append(f"- {event['when']} · {event['summary']}")
            lines.append("")
        else:
            lines.append("**今天**：无安排")
            lines.append("")

        if context.upcoming_events:
            lines.append("**接下来 7 天**")
            lines.append("")
            for event in context.upcoming_events[:10]:
                lines.append(f"- {event['when']} · {event['summary']}")
            lines.append("")

    def _render_window(self, lines: list[str], context: DigestContext) -> None:
        if not context.window_items:
            return
        lines.append("## ⏳ 即将自动入历（可撤销）")
        lines.append("")
        lines.append("以下事件已批准，将在延迟窗口结束后自动写入日历：")
        lines.append("")
        for item in context.window_items:
            lines.append(
                f"- `#{item.event_id}` {_format_when(item.start_ts, item.all_day)} · "
                f"{sanitize_text(item.title, limit=60)}"
            )
        lines.append("")
        lines.append("*用 `automail push --cancel <事件ID>` 撤销。*")
        lines.append("")

    def _render_pending(self, lines: list[str], context: DigestContext) -> None:
        lines.append("## 待审事件")
        lines.append("")
        if not context.pending_items:
            lines.append("无。")
            lines.append("")
            return

        lines.append(f"共 {context.snapshot.pending_review} 条（最多列 {MAX_ITEMS} 条）")
        lines.append("")
        for item in context.pending_items:
            lines.append(
                f"- `#{item.event_id}` **{_format_when(item.start_ts, item.all_day)}** · "
                f"{sanitize_text(item.title, limit=60)}  \n"
                f"  来源：{item.source}（置信 {item.confidence:.2f}）"
                if item.confidence is not None
                else f"- `#{item.event_id}` **{_format_when(item.start_ts, item.all_day)}** · "
                f"{sanitize_text(item.title, limit=60)}"
            )
            if item.review_reason:
                lines.append(f"  待审原因：{sanitize_text(item.review_reason, limit=120)}")
            if item.evidence:
                lines.append(f"  依据：{sanitize_text(item.evidence, limit=120)}")
            if item.mail_subject:
                lines.append(
                    f"  邮件：{sanitize_text(item.mail_subject, limit=60)}"
                    f"（{sanitize_text(item.mail_from or '', limit=40)}）"
                )
        lines.append("")
        lines.append("*用 `automail events approve <ID>` 批准，或 `reject` 否决。*")
        lines.append("")

    def _render_attention(self, lines: list[str], context: DigestContext) -> None:
        if not context.attention_items:
            return
        lines.append("## ⚠️ 需要关注")
        lines.append("")
        for item in context.attention_items:
            lines.append(
                f"- `#{item.event_id}` [{item.status}] "
                f"{_format_when(item.start_ts, item.all_day)} · "
                f"{sanitize_text(item.title, limit=60)}"
            )
            if item.review_reason:
                lines.append(f"  {sanitize_text(item.review_reason, limit=160)}")
        lines.append("")

    def _render_messages(self, lines: list[str], context: DigestContext) -> None:
        lines.append("## 近期邮件")
        lines.append("")
        if not context.recent_messages:
            lines.append("无。")
            lines.append("")
            return

        for message in context.recent_messages[:15]:
            when = _format_when(message.get("received_at"), all_day=False)
            ics = " 📅" if message.get("has_ics") else ""
            subject = sanitize_text(message.get("subject") or "(无主题)", limit=60)
            sender = sanitize_text(
                message.get("from_name") or message.get("from_addr") or "", limit=30
            )
            lines.append(f"- {when} · {subject}{ics} — {sender}")
        lines.append("")

    def _render_threads(self, lines: list[str], context: DigestContext) -> None:
        if not context.active_threads:
            return
        lines.append("## 活跃线程（多封往来）")
        lines.append("")
        for thread in context.active_threads[:8]:
            strength = thread.get("link_strength") or "unknown"
            marker = "" if strength == "strong" else " ⚠️弱关联"
            subject = sanitize_text(thread.get("subject_norm") or "(无主题)", limit=55)
            lines.append(f"- {thread.get('n', 0)} 封 · {subject}{marker}")
        lines.append("")
        lines.append("*弱关联仅凭主题相同推断，不排除同名不同事。*")
        lines.append("")

    def _render_system(self, lines: list[str], context: DigestContext) -> None:
        lines.append("## 系统状态")
        lines.append("")
        snapshot = context.snapshot
        lines.append(f"- 邮件总数：{snapshot.messages_total}")
        lines.append(
            "- 抽取状态："
            + "，".join(
                f"{k} {v}" for k, v in sorted(snapshot.messages_by_extract_status.items())
            )
        )
        lines.append(
            "- 事件："
            + (
                "，".join(f"{k} {v}" for k, v in sorted(snapshot.events_by_status.items()))
                or "无"
            )
        )
        if snapshot.events_by_source:
            lines.append(
                "- 事件来源："
                + "，".join(f"{k} {v}" for k, v in sorted(snapshot.events_by_source.items()))
            )
        lines.append(f"- 线程：{snapshot.threads_total} 个")
        lines.append(f"- 发件人：{snapshot.accounts_senders} 个")
        if context.last_run:
            run = context.last_run
            outcome = "成功" if run.get("ok") else "未成功"
            lines.append(
                f"- 上次运行：`{run.get('command')}` @ {run.get('started_at')}（{outcome}）"
            )

        if context.notes:
            lines.append("")
            for note in context.notes:
                lines.append(f"> ⚠️ {note}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(
            "*本摘要由 auto-mail 生成，仅写入本地文件。"
            "审批与推送分离：`events approve` 只改状态，`push --apply` 才写日历。*"
        )

    # ── 落盘 ──────────────────────────────────────────────

    def output_path(self, context: DigestContext) -> Path:
        local_date = context.generated_at.astimezone(self._tz).strftime("%Y-%m-%d")
        return Path(self._settings.out_dir) / f"digest-{local_date}.md"

    def build(self) -> tuple[Path, str]:
        """生成并写入摘要文件，返回 ``(路径, 内容)``。"""
        context = self.collect()
        content = self.render(context)
        path = self.output_path(context)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info("摘要已写入 %s", path)
        return path, content


def _split_calendar_events(
    events: list, *, now: datetime, tz: ZoneInfo
) -> tuple[list[dict], list[dict]]:
    """把日历事件分成「今天」与「未来 7 天」。"""
    today = now.astimezone(tz).date()
    horizon = today + timedelta(days=7)

    today_events: list[dict] = []
    upcoming: list[dict] = []

    for event in events:
        payload = getattr(event, "payload", {}) or {}
        start = _event_start(payload, tz)
        if start is None:
            continue
        summary = sanitize_text(
            str(payload.get("summary") or "(无标题)"), limit=60
        )
        entry = {
            "when": start.strftime("%H:%M") if _has_time(payload) else "全天",
            "date": start.date().isoformat(),
            "summary": summary,
        }
        day = start.date()
        if day == today:
            today_events.append(entry)
        elif today < day <= horizon:
            entry["when"] = f"{start.strftime('%m-%d %H:%M') if _has_time(payload) else start.strftime('%m-%d') + ' 全天'}"
            upcoming.append(entry)

    today_events.sort(key=lambda e: e["when"])
    upcoming.sort(key=lambda e: (e["date"], e["when"]))
    return today_events, upcoming


def _event_start(payload: dict, tz: ZoneInfo) -> datetime | None:
    """从日历 payload 取开始时间（本地时区）。"""
    field = payload.get("start")
    if not isinstance(field, dict):
        return None

    date_value = field.get("date")
    if date_value:
        try:
            parsed = datetime.strptime(str(date_value)[:10], "%Y-%m-%d")
        except ValueError:
            return None
        return parsed.replace(tzinfo=tz)

    date_time = field.get("dateTime")
    if not date_time:
        return None
    text = str(date_time).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _has_time(payload: dict) -> bool:
    field = payload.get("start")
    return isinstance(field, dict) and bool(field.get("dateTime"))


def _format_when(start_ts: str | None, all_day: bool) -> str:
    """ISO UTC → 本地可读形式。"""
    parsed = parse_iso(start_ts)
    if parsed is None:
        return "（无时间）"
    local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    if all_day:
        return local.strftime("%Y-%m-%d 全天")
    return local.strftime("%Y-%m-%d %H:%M")


def digest_date_of(settings: Settings, dt: datetime | None = None) -> date:
    """摘要对应的本地日期（供幂等键使用）。"""
    moment = dt or utcnow()
    return moment.astimezone(ZoneInfo(settings.user_timezone)).date()
