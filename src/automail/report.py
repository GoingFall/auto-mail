"""终端输出渲染。

统一收口 rich 的使用，便于测试时替换为纯文本。所有对外展示的字符串都要
先经 :func:`~automail.logging_setup.sanitize_text` 清洗——邮件标题、地点等
字段来自外部输入，直接渲染会造成 ANSI 转义注入。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from rich.console import Console
from rich.table import Table

from .logging_setup import sanitize_text
from .models import ReadinessItem, ReadinessStatus, RunRecord

_STATUS_STYLE = {
    ReadinessStatus.OK: "green",
    ReadinessStatus.MISSING: "yellow",
    ReadinessStatus.SKIPPED: "dim",
    ReadinessStatus.ERROR: "red",
}

_STATUS_LABEL = {
    ReadinessStatus.OK: "OK",
    ReadinessStatus.MISSING: "MISSING",
    ReadinessStatus.SKIPPED: "SKIPPED",
    ReadinessStatus.ERROR: "ERROR",
}


def console(file: Any = None) -> Console:
    """构造 Console；测试可传入 StringIO 以捕获输出。"""
    return Console(file=file, highlight=False, soft_wrap=False)


def render_readiness(items: Sequence[ReadinessItem], *, out: Console | None = None) -> None:
    """渲染 doctor 的检查表。"""
    out = out or console()
    table = Table(title="auto-mail doctor", show_lines=False, header_style="bold")
    table.add_column("检查项", style="cyan", no_wrap=True)
    table.add_column("结果", no_wrap=True)
    table.add_column("说明", overflow="fold")

    for item in items:
        label = _STATUS_LABEL[item.status]
        style = _STATUS_STYLE[item.status]
        table.add_row(
            sanitize_text(item.name),
            f"[{style}]{label}[/{style}]",
            sanitize_text(item.detail, limit=2000),
        )
    out.print(table)


def render_readiness_json(items: Sequence[ReadinessItem], *, exit_code: int) -> str:
    """doctor 的机器可读输出，供脚本/计划任务判断。"""
    payload = {
        "exit_code": exit_code,
        "items": [
            {
                "name": item.name,
                "status": item.status.value,
                "detail": item.detail,
                "fatal": item.fatal,
            }
            for item in items
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_runs(records: Iterable[RunRecord], *, out: Console | None = None) -> None:
    """渲染最近的运行记录。

    单独列出退出码而不只显示 ok/失败：退出码 1（部分缺失）与 2（致命）
    的处理方式完全不同，混为一谈会让排查失去线索。
    """
    out = out or console()
    table = Table(title="最近运行", header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("命令")
    table.add_column("开始")
    table.add_column("退出码", no_wrap=True)
    table.add_column("结果", no_wrap=True)

    for record in records:
        if record.ok is None:
            outcome = "[dim]进行中[/dim]"
        elif record.ok:
            outcome = "[green]成功[/green]"
        elif record.exit_code == 1:
            outcome = "[yellow]部分缺失[/yellow]"
        else:
            outcome = "[red]失败[/red]"

        table.add_row(
            record.run_id[:8],
            sanitize_text(record.command),
            record.started_at or "",
            "—" if record.exit_code is None else str(record.exit_code),
            outcome,
        )
    out.print(table)


def render_sync(stats: object, *, out: Console | None = None) -> None:
    """渲染同步结果。

    dry-run 时必须显著标注：使用者要看清楚「这些变化还没有真正发生」，
    否则很容易误以为数据库已经更新。
    """
    out = out or console()
    folders = getattr(stats, "folders", []) or []

    table = Table(
        title="同步结果（dry-run，未写入）" if getattr(stats, "dry_run", True)
        else "同步结果",
        header_style="bold",
    )
    table.add_column("文件夹", style="cyan", no_wrap=True)
    table.add_column("UIDVALIDITY", no_wrap=True)
    table.add_column("扫描", no_wrap=True)
    table.add_column("新增", no_wrap=True)
    table.add_column("复活", no_wrap=True)
    table.add_column("副本", no_wrap=True)
    table.add_column("已移走", no_wrap=True)
    table.add_column("失败", no_wrap=True)
    table.add_column("备注", overflow="fold")

    for folder in folders:
        notes: list[str] = []
        if folder.uidvalidity_changed:
            notes.append("UIDVALIDITY 已变化（旧记录标 stale）")
        if folder.gate_skipped:
            notes.append("无新邮件")
        if folder.flags_refreshed:
            notes.append(f"刷新 flags {folder.flags_refreshed}")

        table.add_row(
            sanitize_text(folder.folder),
            str(folder.uid_validity),
            str(folder.scanned),
            str(folder.inserted),
            str(folder.reactivated),
            str(folder.duplicated),
            str(folder.moved),
            str(folder.fetch_failed) if folder.fetch_failed else "—",
            "；".join(notes) or "—",
        )
    out.print(table)

    total = Table.grid(padding=(0, 2))
    total.add_column(style="bold")
    total.add_column()
    total.add_row("新增", str(getattr(stats, "inserted", 0)))
    total.add_row("复活", str(getattr(stats, "reactivated", 0)))
    total.add_row("副本", str(getattr(stats, "duplicated", 0)))
    total.add_row("已移走", str(getattr(stats, "moved", 0)))
    zombies = getattr(stats, "zombies_reclaimed", 0)
    if zombies:
        total.add_row("僵尸任务回收", str(zombies))
    out.print(total)

    if getattr(stats, "dry_run", True):
        out.print("[yellow]以上为预览；加 --apply 才会真正写入数据库。[/yellow]")


def render_extract(stats: object, *, out: Console | None = None) -> None:
    """渲染抽取结果。

    **必须单独显示 LLM 降级数**（``llm_skipped``）：否则「本可被 LLM 抽取但
    因不可用/超预算而没抽」对使用者不可见，会让人误以为抽取质量就是这样。
    """
    out = out or console()
    dry = getattr(stats, "dry_run", True)

    table = Table(
        title="抽取结果（dry-run，未写入）" if dry else "抽取结果",
        header_style="bold",
    )
    table.add_column("项目", style="cyan", no_wrap=True)
    table.add_column("数量", justify="right", no_wrap=True)

    table.add_row("本轮处理邮件", str(getattr(stats, "considered", 0)))
    table.add_row("预筛过滤", str(getattr(stats, "skipped_by_prefilter", 0)))
    table.add_row("产生候选的邮件", str(getattr(stats, "with_candidates", 0)))
    table.add_row("候选总数", str(getattr(stats, "candidates", 0)))
    table.add_row("  ├─ 可自动入历", str(getattr(stats, "auto_pushable", 0)))
    table.add_row("  └─ 需人工审核", str(getattr(stats, "requires_review", 0)))
    # 孤儿清理只在重跑改变过结果时非零。它说明「库里原有候选已按新逻辑对齐」，
    # 是重跑抽取后使用者最需要知道的一件事（否则会疑惑事件数为何变化）。
    removed = getattr(stats, "events_removed", 0)
    orphaned = getattr(stats, "events_orphaned", 0)
    if removed:
        table.add_row("清理旧候选", str(removed))
    if orphaned:
        table.add_row("[yellow]保留的旧候选[/yellow]", f"[yellow]{orphaned}[/yellow]")
    out.print(table)

    by_source = getattr(stats, "by_source", {}) or {}
    if by_source:
        source_table = Table(title="来源分布", header_style="bold")
        source_table.add_column("来源", style="cyan")
        source_table.add_column("数量", justify="right")
        for name, count in sorted(by_source.items()):
            source_table.add_row(name, str(count))
        out.print(source_table)

    llm_table = Table(title="LLM 使用", header_style="bold")
    llm_table.add_column("项目", style="cyan", no_wrap=True)
    llm_table.add_column("数量", justify="right", no_wrap=True)
    llm_table.add_row("实际调用", str(getattr(stats, "llm_called", 0)))
    llm_table.add_row("成功", str(getattr(stats, "llm_ok", 0)))
    llm_table.add_row("失败", str(getattr(stats, "llm_failed", 0)))
    skipped = getattr(stats, "llm_skipped", 0)
    if skipped:
        llm_table.add_row("[yellow]降级跳过[/yellow]", f"[yellow]{skipped}[/yellow]")
    else:
        llm_table.add_row("降级跳过", "0")
    out.print(llm_table)

    if getattr(stats, "llm_unavailable", False):
        out.print(
            "[yellow]LLM 未配置，已降级为「仅规则 + ICS」模式。"
            "在 .env 中填入 LLM_BASE_URL 与 LLM_API_KEY 可启用兜底抽取。[/yellow]"
        )

    if dry:
        out.print("[yellow]以上为预览；加 --apply 才会写入数据库。[/yellow]")


def render_review_queue(
    items: Sequence[object],
    *,
    title: str = "待审核事件",
    out: Console | None = None,
) -> None:
    """渲染审核队列。

    刻意把**抽取出处**（来源邮件主题 + 依据原文）放在同一行附近：
    审核的价值全在上下文，只给一条「9月20日 10:00 会议」无法判断真假。
    """
    out = out or console()

    if not items:
        out.print(f"[green]{title}：无[/green]")
        return

    table = Table(title=f"{title}（{len(items)} 条）", header_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("时间", no_wrap=True)
    table.add_column("标题", overflow="fold")
    table.add_column("来源", no_wrap=True)
    table.add_column("置信", no_wrap=True)
    table.add_column("状态", no_wrap=True)

    for item in items:
        when = _format_when(getattr(item, "start_ts", None), getattr(item, "all_day", False))
        source = getattr(item, "source", "")
        confidence = getattr(item, "confidence", None)
        status = getattr(item, "status", "")
        status_label = _STATUS_CN.get(status, status)
        if status in {"externally_modified", "conflict", "push_failed", "missing"}:
            status_label = f"[yellow]{status_label}[/yellow]"

        conflicts = getattr(item, "conflicts_with", None)
        title_text = sanitize_text(getattr(item, "title", ""))[:60]
        if conflicts:
            title_text += f" [yellow](与 {','.join('#' + str(c) for c in conflicts)} 冲突)[/yellow]"
        dup_of = getattr(item, "probable_duplicate_of", None)
        if dup_of:
            sim = getattr(item, "duplicate_similarity", None)
            title_text += f" [dim](疑似与 #{dup_of} 重复 {sim})[/dim]"

        table.add_row(
            str(getattr(item, "event_id", "")),
            when,
            title_text,
            source,
            f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "—",
            status_label,
        )
    out.print(table)

    if any(getattr(i, "conflicts_with", None) for i in items):
        out.print(
            "[yellow]「时间矛盾」[/yellow]指同一封邮件给出分歧较大的时刻"
            "（相差超过 1 小时），需你判断哪个对。"
        )
    if any(getattr(i, "sibling_ids", None) for i in items):
        out.print(
            "[dim]「同日另有」只是相关提示——同一封邀请函常写多个环节"
            "（如「迎迓 10:00」与「升旗禮 10:30」），它们并不矛盾，各自都该保留。[/dim]"
        )

    if any(getattr(i, "probable_duplicate_of", None) for i in items):
        out.print(
            "[dim]「疑似重复」只是提示（同一天 + 标题高度相似），**不会**被自动合并。\n"
            "同一发件人的多笔独立交易（如多笔消费、多张卡操作）标题也高度相似，\n"
            "合并会丢掉真实信息。确认确实是同一件事时，用 events reject 批量否决多余的。[/dim]"
        )

    # 逐条给出来源与依据——审核需要它
    detail = Table.grid(padding=(0, 2))
    detail.add_column(style="dim", no_wrap=True)
    detail.add_column(overflow="fold")
    for item in items:
        reason = getattr(item, "review_reason", "")
        evidence = getattr(item, "evidence", "")
        mail_subject = getattr(item, "mail_subject", None)
        mail_from = getattr(item, "mail_from", None)

        if reason:
            detail.add_row(f"#{getattr(item, 'event_id', '')} 待审原因", sanitize_text(reason, limit=200))
        if evidence:
            detail.add_row(f"#{getattr(item, 'event_id', '')} 依据", sanitize_text(evidence, limit=200))
        if mail_subject or mail_from:
            detail.add_row(
                f"#{getattr(item, 'event_id', '')} 来源邮件",
                sanitize_text(f"{mail_subject or ''} · {mail_from or ''}", limit=200),
            )
    out.print(detail)


#: 内部状态 → 中文展示
_STATUS_CN = {
    "pending": "待审",
    "approved": "已批准",
    "pushed": "已入历",
    "push_failed": "推送失败",
    "uncertain": "待确认",
    "externally_modified": "被外部修改",
    "conflict": "冲突",
    "missing": "远端已消失",
    "rejected": "已否决",
    "ignored": "已忽略",
    "cancelled": "已取消",
    "superseded": "已被取代",
}


def _format_when(start_ts: str | None, all_day: bool) -> str:
    """把 ISO 时间转成本地可读形式。"""
    if not start_ts:
        return "（无时间）"
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    try:
        parsed = datetime.strptime(start_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return start_ts[:16]
    local = parsed.astimezone(ZoneInfo("Asia/Shanghai"))
    if all_day:
        return local.strftime("%Y-%m-%d 全天")
    return local.strftime("%Y-%m-%d %H:%M")


def render_review_action(stats: object, *, action: str, out: Console | None = None) -> None:
    """渲染一次审批动作的结果。

    必须报告 ``skipped`` 及原因：静默跳过会让用户以为操作生效了。
    """
    out = out or console()
    changed = getattr(stats, "changed", 0)
    skipped = getattr(stats, "skipped", 0)
    not_found = getattr(stats, "not_found", 0)

    parts = [f"[green]已{action} {changed} 条[/green]"]
    if skipped:
        parts.append(f"[yellow]跳过 {skipped} 条[/yellow]")
    if not_found:
        parts.append(f"[red]未找到 {not_found} 条[/red]")
    out.print("，".join(parts))

    reasons = getattr(stats, "reasons", None) or []
    for reason in reasons[:10]:
        out.print(f"  [dim]· {sanitize_text(str(reason), limit=160)}[/dim]")


def render_push(stats: object, *, out: Console | None = None) -> None:
    """渲染推送结果。"""
    out = out or console()
    dry = getattr(stats, "dry_run", True)

    table = Table(
        title="推送结果（dry-run，未写入日历）" if dry else "推送结果",
        header_style="bold",
    )
    table.add_column("项目", style="cyan", no_wrap=True)
    table.add_column("数量", justify="right", no_wrap=True)

    table.add_row("本轮处理", str(getattr(stats, "considered", 0)))
    table.add_row("新建", str(getattr(stats, "created", 0)))
    table.add_row("更新", str(getattr(stats, "updated", 0)))
    backfilled = getattr(stats, "backfilled", 0)
    if backfilled:
        table.add_row("[green]幂等回填[/green]", f"[green]{backfilled}[/green]")
    table.add_row("无变化跳过", str(getattr(stats, "skipped", 0)))
    frozen = getattr(stats, "frozen", 0)
    if frozen:
        table.add_row("[yellow]冻结（检测到手改）[/yellow]", f"[yellow]{frozen}[/yellow]")
    failed = getattr(stats, "failed", 0)
    if failed:
        table.add_row("[red]失败[/red]", f"[red]{failed}[/red]")
    limit_hit = getattr(stats, "limit_hit", 0)
    if limit_hit:
        table.add_row("[yellow]超本轮上限顺延[/yellow]", f"[yellow]{limit_hit}[/yellow]")
    out.print(table)

    errors = getattr(stats, "errors", None) or []
    for error in errors[:8]:
        out.print(f"  [red]· {sanitize_text(str(error), limit=180)}[/red]")

    if frozen:
        out.print(
            "[yellow]冻结的事件未做任何写入。用 `automail events list --status frozen` "
            "查看差异，再用 adopt（接管）或 reject（否决）裁决。[/yellow]"
        )
    if dry:
        out.print("[yellow]以上为预览；加 --apply 才会真正写入日历。[/yellow]")


def render_push_window(items: Sequence[object], *, out: Console | None = None) -> None:
    """渲染延迟窗口内即将自动入历的事件（可撤销对象）。"""
    out = out or console()
    if not items:
        return
    table = Table(title="延迟窗口内即将自动入历（可撤销）", header_style="bold")
    table.add_column("事件ID", style="cyan", no_wrap=True)
    table.add_column("时间", no_wrap=True)
    table.add_column("标题", overflow="fold")
    for item in items:
        table.add_row(
            str(getattr(item, "event_id", "")),
            _format_when(getattr(item, "start_ts", None), getattr(item, "all_day", False)),
            sanitize_text(getattr(item, "title", ""), limit=60),
        )
    out.print(table)
    out.print("[dim]用 `automail push --cancel <事件ID>` 撤销。[/dim]")


def render_event_list(
    items: Sequence[object], *, status: str, out: Console | None = None
) -> None:
    """渲染事件列表。"""
    label = _STATUS_CN.get(status, status)
    render_review_queue(items, title=f"事件列表 · {label}", out=out)


def render_threads(stats: object, *, out: Console | None = None) -> None:
    """渲染线程重建结果。

    **必须单列弱关联数**：主题弱关联不是事实，使用者要知道有多少线程
    只是「主题相同」的推测。
    """
    out = out or console()
    dry = getattr(stats, "dry_run", True)

    table = Table(
        title="线程重建（dry-run，未写入）" if dry else "线程重建",
        header_style="bold",
    )
    table.add_column("项目", style="cyan", no_wrap=True)
    table.add_column("数量", justify="right", no_wrap=True)

    table.add_row("参与建图邮件", str(getattr(stats, "messages", 0)))
    table.add_row("分成线程", str(getattr(stats, "threads", 0)))
    table.add_row("  ├─ 强关联", str(getattr(stats, "strong", 0)))
    weak = getattr(stats, "weak", 0)
    if weak:
        table.add_row("  ├─ 弱关联（仅主题）", f"[yellow]{weak}[/yellow]")
    else:
        table.add_row("  └─ 弱关联（仅主题）", "0")
    ghosts = getattr(stats, "ghosts", 0)
    if ghosts:
        table.add_row("幽灵锚点（父邮件未入库）", str(ghosts))
    orphaned = getattr(stats, "orphaned_by_missing_id", 0)
    if orphaned:
        table.add_row("无 Message-ID（改用哈希）", str(orphaned))
    merged = getattr(stats, "merged_by_subject", 0)
    if merged:
        table.add_row("主题弱关联合并", str(merged))
    cycles = getattr(stats, "cycles_broken", 0)
    if cycles:
        table.add_row("[yellow]循环引用已打破[/yellow]", f"[yellow]{cycles}[/yellow]")
    out.print(table)

    if weak:
        out.print(
            "[dim]弱关联仅凭主题相同推断（不排除同名不同事），"
            "不作为事实依据；自动化通知已排除在弱关联之外。[/dim]"
        )
    if dry:
        out.print("[yellow]以上为预览；加 --apply 才会写入数据库。[/yellow]")


def render_digest(path: object, content: str | None = None, *, out: Console | None = None) -> None:
    """渲染摘要生成结果。

    默认只报告路径——摘要内容较长，终端里看 Markdown 表格并不舒服，
    使用者通常会去打开文件。
    """
    out = out or console()
    out.print(f"[green]摘要已生成[/green] {path}")
    if content:
        out.print("")
        out.print(content)


def render_stats(snapshot: object, *, out: Console | None = None) -> None:
    """渲染只读统计。

    **降级/跳过类指标必须显著**：它们说明「系统主动放弃了什么」，
    与「失败」是不同性质的信号。
    """
    out = out or console()

    mail_table = Table(title="邮件", header_style="bold")
    mail_table.add_column("项目", style="cyan", no_wrap=True)
    mail_table.add_column("数量", justify="right", no_wrap=True)
    mail_table.add_row("总数", str(getattr(snapshot, "messages_total", 0)))
    for status, count in sorted(
        (getattr(snapshot, "messages_by_extract_status", {}) or {}).items()
    ):
        label = {"pending": "待抽取", "running": "抽取中", "done": "已抽取", "failed": "抽取失败"}.get(
            status, status
        )
        mail_table.add_row(f"  {label}", str(count))
    mail_table.add_row("含 ICS", str(getattr(snapshot, "messages_with_ics", 0)))
    mail_table.add_row("可退订", str(getattr(snapshot, "messages_with_unsubscribe", 0)))
    mail_table.add_row("发件人", str(getattr(snapshot, "accounts_senders", 0)))
    out.print(mail_table)

    event_status = getattr(snapshot, "events_by_status", {}) or {}
    if event_status:
        event_table = Table(title="事件", header_style="bold")
        event_table.add_column("状态", style="cyan", no_wrap=True)
        event_table.add_column("数量", justify="right", no_wrap=True)
        for status, count in sorted(event_status.items()):
            label = _STATUS_CN.get(status, status)
            style = ""
            if status in {"externally_modified", "conflict", "push_failed", "missing"}:
                style = f"[yellow]{label}[/yellow]"
            else:
                style = label
            event_table.add_row(style, str(count))
        out.print(event_table)

    by_source = getattr(snapshot, "events_by_source", {}) or {}
    if by_source:
        source_table = Table(title="事件来源", header_style="bold")
        source_table.add_column("来源", style="cyan")
        source_table.add_column("数量", justify="right")
        for source, count in sorted(by_source.items()):
            source_table.add_row(source, str(count))
        out.print(source_table)

    thread_table = Table(title="线程", header_style="bold")
    thread_table.add_column("项目", style="cyan", no_wrap=True)
    thread_table.add_column("数量", justify="right", no_wrap=True)
    thread_table.add_row("总数", str(getattr(snapshot, "threads_total", 0)))
    strengths = getattr(snapshot, "threads_by_strength", {}) or {}
    for strength, count in sorted(strengths.items()):
        label = {"strong": "强关联", "weak": "弱关联（仅主题猜测）"}.get(strength, strength)
        thread_table.add_row(f"  {label}", str(count))
    out.print(thread_table)

    attention = getattr(snapshot, "needs_attention", 0)
    window = getattr(snapshot, "pending_window", 0)
    if attention or window:
        alert_table = Table(title="需要关注", header_style="bold")
        alert_table.add_column("项目", style="cyan", no_wrap=True)
        alert_table.add_column("数量", justify="right", no_wrap=True)
        if attention:
            alert_table.add_row("[yellow]需人工关注的事件[/yellow]", f"[yellow]{attention}[/yellow]")
        if window:
            alert_table.add_row("延迟窗口内待推送", str(window))
        out.print(alert_table)

    runs_table = Table(title="运行", header_style="bold")
    runs_table.add_column("项目", style="cyan", no_wrap=True)
    runs_table.add_column("数量", justify="right", no_wrap=True)
    runs_table.add_row("总运行次数", str(getattr(snapshot, "runs_total", 0)))
    failed = getattr(snapshot, "runs_failed", 0)
    if failed:
        runs_table.add_row("[red]未成功的运行[/red]", f"[red]{failed}[/red]")
    out.print(runs_table)


def render_audit(report: object, *, out: Console | None = None, limit: int = 20) -> None:
    """渲染抽取复盘结果。

    「很可能漏抽」必须排在显眼位置：它是**静默失败**——流程报成功、
    使用者也不会察觉少了什么，正是复查要抓的那一类。
    """
    out = out or console()
    counts = report.counts() if hasattr(report, "counts") else {}

    table = Table(title="抽取复盘", header_style="bold")
    table.add_column("项目", style="cyan", no_wrap=True)
    table.add_column("数量", justify="right", no_wrap=True)
    table.add_row("窗口内邮件", str(counts.get("messages", 0)))
    table.add_row("[red]很可能漏抽[/red]", str(counts.get("likely_missed", 0)))
    table.add_row("[yellow]值得留意[/yellow]", str(counts.get("maybe", 0)))
    table.add_row("已抽出候选", str(counts.get("with_candidates", 0)))
    table.add_row("库中结果过时", str(counts.get("stale", 0)))
    if counts.get("unjudged"):
        table.add_row("[dim]未判定（缺 LLM）[/dim]", str(counts["unjudged"]))
    out.print(table)
    out.print(
        f"[dim]窗口：最近 {getattr(report, 'hours', 24)} 小时"
        f"（{getattr(report, 'window_start', '')} → {getattr(report, 'window_end', '')}）"
        f"　LLM：{'已调用' if getattr(report, 'used_llm', False) else '未调用'}[/dim]"
    )

    suspects = list(getattr(report, "suspect_items", []) or [])
    if not suspects:
        out.print("\n[green]没有可疑项。[/green]")
        return

    out.print("")
    for item in suspects[:limit]:
        level = getattr(item, "suspicion_level", 0)
        tag = "[red][很可能漏抽][/red]" if level == 2 else "[yellow][值得留意][/yellow]"
        out.print(
            f"{tag} #{getattr(item, 'message_id', '?')} "
            f"{sanitize_text(getattr(item, 'subject', ''), limit=80)}"
        )
        out.print(
            f"    {sanitize_text(getattr(item, 'suspicion', '') or '', limit=200)}"
        )
        out.print(
            f"    规则命中 {getattr(item, 'rules_hits', 0)} 个时间"
            f" ｜ 当前候选 {getattr(item, 'candidate_count', 0)}"
            f" ｜ 库中事件 {getattr(item, 'stored_count', 0)}"
        )
        out.print("")

    if len(suspects) > limit:
        out.print(f"[dim]（其余 {len(suspects) - limit} 封见报告文件）[/dim]")


def render_audit_detail(text: str, *, out: Console | None = None) -> None:
    """渲染单封邮件的复盘细节（纯文本，含正文片段）。"""
    out = out or console()
    out.print(sanitize_text(text, limit=20000))


def render_mark_read(stats: object, *, out: Console | None = None, limit: int = 30) -> None:
    """渲染已读回写结果。

    这是**唯一会改变邮箱状态**的操作，因此必须明确说出「真的写了没有」。
    dry-run 与 apply 的输出不能只是数字不同——使用者得一眼看出区别。
    """
    out = out or console()
    dry = bool(getattr(stats, "dry_run", True))
    policy = getattr(stats, "policy", "off")
    # 撤销走的是同一套统计，但方向相反：把「标记」说成「已标记为已读」会
    # 让人以为邮箱被标了已读，而实际是把未读恢复了。措辞必须跟着方向走。
    undo = policy == "undo"
    verb = "恢复为未读" if undo else "标记为已读"
    marked = getattr(stats, "marked", 0)
    candidates = getattr(stats, "total_candidates", 0)
    already_seen = getattr(stats, "already_seen", 0)
    to_write = getattr(stats, "to_write", candidates)

    table = Table(title="已读回写（撤销）" if undo else "已读回写", header_style="bold")
    table.add_column("项目", style="cyan", no_wrap=True)
    table.add_column("数量", justify="right", no_wrap=True)
    table.add_row("策略", policy)
    if dry:
        # 报「实际会发 STORE 的封数」而非总候选数：后者包含本来就已读的，
        # 会让这个唯一改变邮箱状态的操作显得比实际激进。
        table.add_row(f"将{verb}", str(to_write))
    else:
        table.add_row(f"已{verb}", str(marked))
    if already_seen:
        table.add_row("[dim]本来已是已读[/dim]", str(already_seen))
    out.print(table)

    if dry:
        out.print(
            "[yellow]dry-run：未向邮箱发送任何 STORE，邮箱状态未改变。"
            "加 --apply 才真正执行。[/yellow]"
        )
    else:
        out.print(f"[green]已在邮箱中{verb}：{marked} 封。[/green]")

    folders = list(getattr(stats, "folders", []) or [])
    for folder in folders:
        name = getattr(folder, "folder", "?")
        if getattr(folder, "skipped_reason", ""):
            out.print(f"  [yellow]跳过 {name}[/yellow]：{folder.skipped_reason}")
        if getattr(folder, "error", None):
            out.print(
                f"  [red]{name} 错误[/red]："
                f"{sanitize_text(str(folder.error), limit=200)}"
            )
        failed = getattr(folder, "failed", 0)
        if failed:
            out.print(f"  [yellow]{name} 有 {failed} 封未标记成功[/yellow]")

    items = list(getattr(stats, "candidates", []) or [])
    if items:
        out.print("")
        label = "将标记" if dry else "已标记"
        out.print(f"{label}的邮件（{len(items)} 封，最多显示 {limit}）：")
        for item in items[:limit]:
            out.print(
                f"  · #{getattr(item, 'message_id', '?')} "
                f"UID {getattr(item, 'uid', '?')} ｜ "
                f"{sanitize_text(getattr(item, 'subject', ''), limit=60)}"
                f"  [dim]({getattr(item, 'reason', '')})[/dim]"
            )
        if len(items) > limit:
            out.print(f"  [dim]…其余 {len(items) - limit} 封见上方统计[/dim]")


def render_pipeline(stats: object, *, out: Console | None = None) -> None:
    """渲染 ``run`` 的完整流程结果。

    必须逐阶段显示结果与退出码：只报一个总结果是没用的——失败时使用者
    需要立刻知道**是哪一阶段**、以及其余阶段是否照常完成了。
    """
    out = out or console()
    dry = not getattr(stats, "apply", False)

    if not getattr(stats, "lock_acquired", True):
        out.print("[yellow]未执行（已有运行在进行中）[/yellow]")
        out.print(f"  {sanitize_text(getattr(stats, 'lock_conflict', '') or '', limit=300)}")
        out.print(
            "\n[dim]这通常表示上一次运行还在进行（或崩溃后锁尚未到期）。"
            "若确认没有运行在进行，可稍候重试；锁会在 TTL 后自动失效。[/dim]"
        )
        return

    table = Table(
        title="运行流程（dry-run，未写入）" if dry else "运行流程",
        header_style="bold",
    )
    table.add_column("阶段", style="cyan", no_wrap=True)
    table.add_column("结果", no_wrap=True)
    table.add_column("摘要", overflow="fold")

    for stage in getattr(stats, "stages", []):
        code = getattr(stage, "exit_code", 0)
        if getattr(stage, "skipped", False):
            label = "[dim]跳过[/dim]"
            summary = sanitize_text(getattr(stage, "skip_reason", ""), limit=120)
        elif code == 0:
            label = "[green]成功[/green]"
            summary = _stage_summary(stage)
        elif code == 1:
            label = "[yellow]部分完成[/yellow]"
            summary = _stage_summary(stage) or sanitize_text(
                getattr(stage, "error", "") or "", limit=120
            )
        else:
            label = "[red]失败[/red]"
            summary = sanitize_text(getattr(stage, "error", "") or "", limit=120)
        table.add_row(getattr(stage, "name", "?"), label, summary or "—")
    out.print(table)

    overall = getattr(stats, "exit_code", 0)
    label = {0: "[green]全部完成[/green]", 1: "[yellow]部分完成[/yellow]",
             2: "[red]存在致命问题[/red]"}.get(overall, str(overall))
    out.print(f"整体：{label} · 退出码 {overall}")

    if dry:
        out.print("[yellow]以上为预览；加 --apply 才会真正执行。[/yellow]")


def _stage_summary(stage: object) -> str:
    """给一个阶段生成一行人类可读摘要。"""
    name = getattr(stage, "name", "")
    data = getattr(stage, "stats", {}) or {}
    parts: list[str] = []

    if name == "sync":
        parts.append(f"扫描 {data.get('scanned', 0)}")
        parts.append(f"新增 {data.get('inserted', 0)}")
        if data.get("reactivated"):
            parts.append(f"复活 {data.get('reactivated')}")
        if data.get("gate_skipped"):
            parts.append("无新邮件")
    elif name == "extract":
        parts.append(f"处理 {data.get('considered', 0)} 封")
        parts.append(f"候选 {data.get('candidates', 0)}")
        if data.get("llm_skipped"):
            parts.append(f"[yellow]LLM 降级跳过 {data['llm_skipped']}[/yellow]")
        if data.get("failed"):
            parts.append(f"[yellow]失败 {data['failed']}[/yellow]")
    elif name == "push":
        parts.append(f"新建 {data.get('created', 0)}")
        if data.get("updated"):
            parts.append(f"更新 {data.get('updated')}")
        if data.get("backfilled"):
            parts.append(f"[green]幂等回填 {data['backfilled']}[/green]")
        if data.get("frozen"):
            parts.append(f"[yellow]冻结 {data['frozen']}[/yellow]")
        if data.get("failed"):
            parts.append(f"[red]失败 {data['failed']}[/red]")
    elif name == "digest":
        parts.append(str(data.get("path", "")))

    return "，".join(parts)


def render_backup(stats: object, settings: object, *, out: Console | None = None) -> None:
    """渲染备份结果。"""
    out = out or console()

    if getattr(stats, "prune_only", False):
        removed = getattr(stats, "removed", 0)
        out.print(f"[green]已清理 {removed} 个过期备份[/green]")
    else:
        path = stats.get("path") if isinstance(stats, dict) else getattr(stats, "path", "")
        size = stats.get("size_bytes", 0) if isinstance(stats, dict) else 0
        out.print(f"[green]备份完成[/green] {path}")
        out.print(f"  大小：{size / 1024:.1f} KB")

    kept = stats.get("kept") if isinstance(stats, dict) else None
    if kept is not None:
        out.print(f"  当前保留 {kept} 个备份")

    backup_dir = getattr(settings, "backup_dir", None)
    keep = getattr(settings, "db_backup_keep", None)
    age = getattr(settings, "db_backup_max_age_days", None)
    if backup_dir:
        out.print(f"  目录：{backup_dir}")
    if keep is not None and age is not None:
        out.print(f"  [dim]保留策略：最多 {keep} 个 / {age} 天[/dim]")


def render_auth_status(info: object, *, out: Console | None = None) -> None:
    """渲染 Google 授权状态。

    必须逐项显示「有什么、缺什么」：使用者拿到 `credentials.json` 后最常见的
    困惑就是「我到底还差哪一步」。
    """
    out = out or console()

    table = Table(title="Google 日历授权状态", header_style="bold")
    table.add_column("项目", style="cyan", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("路径 / 说明", overflow="fold")

    def mark(present: bool) -> str:
        return "[green]已就绪[/green]" if present else "[yellow]缺失[/yellow]"

    table.add_row(
        "credentials.json",
        mark(getattr(info, "credentials_present", False)),
        str(getattr(info, "credentials_file", "")),
    )
    table.add_row(
        "token.json（授权）",
        mark(getattr(info, "token_present", False)),
        str(getattr(info, "token_file", "")),
    )

    needs_reauth = getattr(info, "needs_reauth", False)
    if needs_reauth:
        table.add_row(
            "需要重新授权",
            "[yellow]是[/yellow]",
            "refresh token 已失效（invalid_grant）",
        )

    out.print(table)

    detail = getattr(info, "detail", "")
    if detail:
        out.print(f"  说明：{detail}")

    ready = getattr(info, "ready", False)
    if ready:
        out.print("\n[green]授权就绪[/green]，可以写入真实日历。")
    else:
        out.print("\n[yellow]尚未就绪[/yellow]。运行 [bold]automail auth[/bold] 完成授权。")


def render_setup(report: object, *, out: Console | None = None) -> None:
    """渲染便携版目录状态。"""
    out = out or console()
    base = getattr(report, "base_dir", "")

    table = Table(title="便携版目录", header_style="bold")
    table.add_column("项目", style="cyan", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("说明", overflow="fold")

    table.add_row("数据目录", "[green]就绪[/green]", str(base))

    created = getattr(report, "created_dirs", []) or []
    table.add_row(
        "目录创建",
        "[green]已创建[/green]" if created else "无需",
        "，".join(p.name for p in created) if created else "均已存在",
    )

    env_created = getattr(report, "env_created", False)
    table.add_row(
        ".env",
        "[green]已生成[/green]" if env_created else "已存在",
        "已从模板生成，请填写内容" if env_created else "读取现有配置",
    )

    missing = set(getattr(report, "missing", []) or [])
    for name in ("credentials.json", "token.json"):
        if name in missing:
            table.add_row(name, "[yellow]缺失[/yellow]", "见下方说明")
        else:
            table.add_row(name, "[green]就绪[/green]", "")

    out.print(table)

    from .portable import describe_missing

    lines = describe_missing(report)
    if lines:
        out.print("")
        for line in lines:
            out.print(f"  · {line}")
