"""事件内容的规范化哈希：三方比对的地基。

**为什么必须有这个模块**（docs/spec-gcal-ownership.md §3）：

要判断「远端事件是否被用户手改过」，需要比较三份内容：

* ``snapshot_hash``   —— 我方**上次写入**的内容
* ``remote_norm_hash``—— 当前从日历服务读回的
* ``local_norm_hash`` —— 我方**当前待写入**的

三者必须由**同一个函数**产出，否则不可比。

**关键洞察**（来自 vdirsyncer）：哈希必须作用在**规范化后**的形式上。
服务端会对事件做自己的序列化——字段重排、注入 ``etag``/``updated``/``htmlLink``、
把时区表示从 ``+08:00`` 改成 ``Asia/Shanghai``、重排 extendedProperties。
若直接对原始 JSON 求哈希，**每次读取都会「有差异」**，机制立即失效——
要么把所有事件误判为「被手改」，要么改成谁也不信的比较。

因此规范化的四步是「白名单 + 黑名单 + 时间归一 + 空白归一」。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

#: 参与比较的字段（白名单）。只列语义字段，不列服务器注入的元数据。
_KEEP_FIELDS = (
    "summary",
    "description",
    "location",
    "start",
    "end",
    "status",
    "transparency",
)

#: 服务器注入或易变的字段（黑名单）。它们变了不代表用户改了内容。
_DROP_FIELDS = frozenset(
    {
        "id",
        "etag",
        "htmlLink",
        "iCalUID",
        "created",
        "updated",
        "sequence",
        "hangoutLink",
        "creator",
        "reminders",
        "conferenceData",
        "attendees",
        "organizer",
        "kind",
        "recurrence",
        "extendedProperties",
    }
)

_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")

#: 比较用：把**所有**空白（含换行）折叠成单个空格
_ALL_WHITESPACE_RE = re.compile(r"\s+")


def normalize_datetime(value: Any) -> str:
    """把日历的时间表示归一化为可比字符串。

    处理三类等价表示的差异：

    * ``{"dateTime": "2026-09-20T10:00:00+08:00"}`` 与 ``...Z`` 的时区表示差异
    * ``{"date": "2026-09-20"}``（全天）与带时刻的表示
    * ``{"dateTime": ..., "timeZone": "Asia/Shanghai"}`` 中 ``timeZone`` 的冗余

    统一收敛到 **UTC 瞬时**（精确到分钟）；全天事件收敛到日期。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return _normalize_ts_string(value)
    if not isinstance(value, dict):
        return str(value)

    # 全天事件
    date_value = value.get("date")
    if date_value:
        return f"D:{str(date_value)[:10]}"

    date_time = value.get("dateTime")
    if date_time:
        return _normalize_ts_string(str(date_time))

    return ""


def _normalize_ts_string(text: str) -> str:
    """ISO8601 字符串 → 精确到分钟的 UTC 形式。"""
    cleaned = text.strip()
    if not cleaned:
        return ""
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        # 不可解析就退回字面量（去掉秒与毫秒，保证同源可比）
        return re.sub(r":\d{2}(\.\d+)?$", "", text.strip())
    if parsed.tzinfo is None:
        # 无时区信息：按字面本地时刻处理，不做臆测换算
        return parsed.strftime("%Y-%m-%dT%H:%M")
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def normalize_text(value: Any) -> str:
    """归一化文本，用于**比较**。

    把**所有**空白序列（含换行、制表符）折叠成单个空格。

    为什么连换行也要折叠：真实服务端会重新折行、压缩空白。实测中
    Google 会把描述里的 ``\\n`` 折叠成空格——若比较时把换行当作有意义的内容，
    那么**每一次读取都会「看起来有变化」**：

    * ``remote_norm_hash`` 永远不等于 ``snapshot_hash``
    * ``benign_evolution``（无害演进）分支永远无法命中
    * 每次服务端轻微改写都被误判为 ``conflict``

    描述里的换行是**排版**而非**内容**；用户真正的手改会改变文字，不会只是
    把换行换成空格。因此折叠空白是正确的比较语义。
    """
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _ALL_WHITESPACE_RE.sub(" ", text)
    return text.strip()


def canonical_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """把日历事件 payload 收敛成可比较的规范化字典。

    供测试与调试查看「规范化后到底比什么」——排查「为什么判定为有差异」时
    直接打印它比读哈希有用得多。
    """
    if not payload:
        return {}

    result: dict[str, Any] = {}
    for field in _KEEP_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        if field in {"start", "end"}:
            result[field] = normalize_datetime(value)
        else:
            result[field] = normalize_text(value)

    # auto_mail_key 参与比较是必要的：它能区分「我们的事件」与「别人的事件」，
    # 而它只在 extendedProperties.private 下，需要单独取出。
    key = extract_auto_mail_key(payload)
    if key:
        result["auto_mail_key"] = key

    return result


def extract_auto_mail_key(payload: dict[str, Any] | None) -> str | None:
    """取出 ``extendedProperties.private.auto_mail_key``（我方所有权标记）。"""
    if not payload:
        return None
    props = payload.get("extendedProperties")
    if not isinstance(props, dict):
        return None
    private = props.get("private")
    if not isinstance(private, dict):
        return None
    value = private.get("auto_mail_key")
    return str(value) if value else None


def normalize_hash(payload: dict[str, Any] | None) -> str:
    """计算规范化哈希（sha256 前 32 位十六进制）。

    三个哈希位（snapshot / remote / local）都用它，**不可各自实现**——
    否则比较无意义。
    """
    canonical = canonical_payload(payload)
    serialized = json.dumps(
        canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]


def diff_payloads(
    older: dict[str, Any] | None, newer: dict[str, Any] | None
) -> dict[str, tuple[Any, Any]]:
    """列出两个 payload 在**规范化后**的实际差异字段。

    用途是给人看：当判定为 ``externally_modified`` 时，使用者最想知道
    「到底哪里不一样」。这也是审核界面上「与我方快照的差异」的数据来源。
    """
    left = canonical_payload(older)
    right = canonical_payload(newer)
    keys = sorted(set(left) | set(right))
    differences: dict[str, tuple[Any, Any]] = {}
    for key in keys:
        if left.get(key) != right.get(key):
            differences[key] = (left.get(key), right.get(key))
    return differences
