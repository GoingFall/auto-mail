"""把数据整形为界面用的行/标签：**纯函数，不含 Tk**。

存在的意义同样是可测性：表格里显示什么、状态怎么翻译成中文、时间去哪儿了——
这些是界面最容易出错的地方（"已批准"和"已推送"混了、时间差 8 小时），
而它们全都能在没有显示会话的机器上用普通单测覆盖。

界面层只负责把这些字符串塞进控件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

# ── 时间 ──────────────────────────────────────────────────────

#: 库里存的是 UTC，界面必须按使用者时区显示。
#: 实测过教训：直接用 UTC 字符串会让"10:00 的事件"看起来早 8 小时。
_DEFAULT_TZ = "Asia/Shanghai"


def to_local(value: str | None, *, tz_name: str = _DEFAULT_TZ) -> datetime | None:
    """把库里的 UTC 时间串转成本地时间；解析失败返回 ``None``。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 - 时区名非法时退回 UTC，而不是崩
        return parsed.astimezone(UTC)


def format_time(value: str | None, *, tz_name: str = _DEFAULT_TZ) -> str:
    """格式化时间戳：``2026-10-01 10:30``。"""
    local = to_local(value, tz_name=tz_name)
    return local.strftime("%Y-%m-%d %H:%M") if local else "—"


def format_date(value: str | None, *, tz_name: str = _DEFAULT_TZ) -> str:
    local = to_local(value, tz_name=tz_name)
    return local.strftime("%Y-%m-%d") if local else "—"


def format_relative(value: str | None, *, now: datetime | None = None) -> str:
    """相对时间（``3 小时前``），用于邮件列表。

    比绝对时间更易扫读——使用者关心"新不新"，而不是精确到分。
    """
    local = to_local(value)
    if local is None:
        return "—"
    reference = now.astimezone(local.tzinfo) if now else datetime.now(local.tzinfo)
    delta = reference - local
    seconds = delta.total_seconds()

    if seconds < 0:
        # 未来的时间（例如定时任务提前同步到的邮件）：不显示"负几小时前"
        return local.strftime("%m-%d %H:%M")
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 86400 * 7:
        return f"{int(seconds // 86400)} 天前"
    return local.strftime("%Y-%m-%d")


def format_event_when(
    start_ts: str | None,
    *,
    all_day: bool,
    tz_name: str = _DEFAULT_TZ,
) -> str:
    """事件的显示时间。全天事件只显示日期——显示 ``00:00`` 会误导。

    无法解析时统一返回「无时间」：事件上下文里"没有时间"比一个占位破折号
    更有意义（事件的核心就是时间，缺失需要明说）。
    """
    local = to_local(start_ts, tz_name=tz_name)
    if local is None:
        return "无时间"
    if all_day:
        return f"{local.strftime('%Y-%m-%d')} 全天"
    return local.strftime("%Y-%m-%d %H:%M")


# ── 状态标签 ──────────────────────────────────────────────────

#: 事件状态 → 中文。**必须覆盖全部取值**，漏掉会在界面上显示英文原值。
EVENT_STATUS_LABELS: dict[str, str] = {
    "pending": "待审",
    "approved": "已批准，等待写入日历",
    "pushed": "已写入日历",
    "push_failed": "写入失败",
    "uncertain": "状态未确认",
    "externally_modified": "被外部修改",
    "conflict": "存在冲突",
    "missing": "日历中已不存在",
    "rejected": "已否决",
    "ignored": "已忽略",
    "cancelled": "已撤销",
    "superseded": "已被取代",
}

SOURCE_LABELS: dict[str, str] = {
    "ics": "日历邀请",
    "rules": "规则",
    "llm": "大模型",
}

EXTRACT_STATUS_LABELS: dict[str, str] = {
    "pending": "待抽取",
    "running": "抽取中",
    "done": "已抽取",
    "failed": "抽取失败",
}

#: 需要使用者留意的状态（界面用醒目颜色标出）
ATTENTION_STATUSES = frozenset(
    {"externally_modified", "conflict", "missing", "push_failed", "uncertain"}
)

#: 冻结态：程序拒绝写入，必须人工裁决
FROZEN_STATUSES = frozenset({"externally_modified", "conflict", "missing"})


def event_status_label(status: str) -> str:
    return EVENT_STATUS_LABELS.get(status, status)


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def extract_status_label(status: str) -> str:
    return EXTRACT_STATUS_LABELS.get(status, status)


def needs_attention(status: str) -> bool:
    return status in ATTENTION_STATUSES


def is_frozen(status: str) -> bool:
    """冻结态 —— 界面据此决定是否显示「接管」按钮。

    ``approve`` 对冻结态是无效的（状态机不允许），因此必须给「接管」
    而不是让人反复点批准却没反应。
    """
    return status in FROZEN_STATUSES


# ── 表格行 ────────────────────────────────────────────────────


def _id_list(ids: list[Any]) -> str:
    """把一组 id 渲染成 ``#3、#4``。

    每个 id 都带 ``#`` 前缀：``#3,4`` 会被读成"第 3 到 4 条"或一个数字，
    而 ``#3、#4`` 明确是两个事件引用。审核时看错对象代价不小。
    """
    return "、".join(f"#{i}" for i in ids)


def review_row(item: Any, *, tz_name: str = _DEFAULT_TZ) -> tuple[str, ...]:
    """待审队列的一行。

    列顺序必须与界面里 ``columns`` 的定义一致——本函数与界面共用一份定义
    （见 ``gui.panels.review``），避免两处漂移。
    """
    markers: list[str] = []
    conflicts = list(getattr(item, "conflicts_with", []) or [])
    if conflicts:
        markers.append(f"与 {_id_list(conflicts)} 冲突")
    if getattr(item, "probable_duplicate_of", None):
        markers.append(f"疑似与 #{item.probable_duplicate_of} 重复")
    siblings = list(getattr(item, "sibling_ids", []) or [])
    if siblings:
        markers.append(f"同日相关 {_id_list(siblings)}")

    title = str(getattr(item, "title", "") or "")
    if markers:
        title = f"{title}（{'；'.join(markers)}）"

    confidence = getattr(item, "confidence", None)
    return (
        str(getattr(item, "event_id", "")),
        format_event_when(
            getattr(item, "start_ts", None),
            all_day=bool(getattr(item, "all_day", False)),
            tz_name=tz_name,
        ),
        title,
        source_label(str(getattr(item, "source", "") or "")),
        f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "—",
        event_status_label(str(getattr(item, "status", "") or "")),
    )


def review_detail(item: Any) -> str:
    """待审项的详情文本（依据 + 来源邮件 + 待审原因）。

    合并成一段而不是分列：这些是给人读的句子，拆成表格反而难读。
    """
    lines: list[str] = []
    reason = getattr(item, "review_reason", "") or ""
    if reason:
        lines.append(f"待审原因：{reason}")
    evidence = getattr(item, "evidence", "") or ""
    if evidence:
        lines.append(f"抽取依据：{evidence}")
    subject = getattr(item, "mail_subject", "") or ""
    sender = getattr(item, "mail_from", "") or ""
    if subject or sender:
        lines.append(f"来源邮件：{subject}　{sender}".strip())

    snapshot = getattr(item, "snapshot_diff", None)
    if snapshot:
        lines.append(_describe_snapshot(snapshot))
    return "\n".join(lines)


def _describe_snapshot(snapshot: dict[str, Any]) -> str:
    """把冻结态的快照差异说成人话。

    ``snapshot_diff`` 目前只带 ``snapshot``（我方原值）。远端现值要在推送时
    实时读取，所以这里如实说明"远端未读取"，而不是编造一个对比。
    """
    own = snapshot.get("snapshot") or {}
    title = own.get("summary") or "（无标题）"
    start = (own.get("start") or {}).get("dateTime") or (own.get("start") or {}).get(
        "date"
    )
    return (
        f"我方原本：{title}"
        + (f"（{start}）" if start else "")
        + "\n远端现值需在写入时实时读取（未缓存远端原文，避免隐私面扩大）"
    )


def mail_row(row: Any, *, tz_name: str = _DEFAULT_TZ) -> tuple[str, ...]:
    """邮件列表的一行。"""
    sender = row.from_name or row.from_addr
    status = extract_status_label(row.extract_status)
    if row.is_unread:
        status = f"● {status}"  # 未读标记：最有效的注意力信号
    return (
        str(row.message_id),
        format_relative(row.received_at),
        str(row.subject),
        sender,
        str(row.event_count) if row.event_count else "—",
        status,
    )


def event_row(event: dict[str, Any], *, tz_name: str = _DEFAULT_TZ) -> tuple[str, ...]:
    """事件列表的一行（邮件详情页里的关联事件）。"""
    confidence = event.get("confidence")
    return (
        str(event.get("id", "")),
        format_event_when(
            event.get("start_ts"),
            all_day=bool(event.get("all_day")),
            tz_name=tz_name,
        ),
        str(event.get("title") or ""),
        source_label(str(event.get("source") or "")),
        f"{confidence:.2f}" if isinstance(confidence, (int, float)) else "—",
        event_status_label(str(event.get("status") or "")),
    )


def progress_text(stage: str, index: int, total: int, *, kind: str = "stage") -> str:
    """状态栏的进度文案。"""
    from ..progress import stage_label

    label = stage_label(stage)
    if total <= 0:
        return label
    if kind == "stage":
        return f"{label}（第 {index}/{total} 阶段）"
    return f"{label}… {index}/{total}"


def progress_ratio(stage: str, index: int, total: int, *, kind: str = "stage") -> float:
    """进度条比例（0~1）。

    阶段级进度只占整条进度条的一部分：把阶段序号当分子，避免"第 1/4 阶段"
    就显示 100% 的错觉。
    """
    if total <= 0:
        return 0.0
    if kind == "stage":
        return max(0.0, min(1.0, (index - 1) / total))
    return max(0.0, min(1.0, index / total))
