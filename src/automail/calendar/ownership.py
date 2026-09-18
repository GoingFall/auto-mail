"""事件所有权仲裁：三方比对与冻结决策。

实现 docs/spec-gcal-ownership.md §3 的判断链。这是「绝不覆盖用户手改内容」
这条承诺的落地点。

## 三个哈希

| 哈希 | 含义 |
|---|---|
| ``snapshot_hash`` | 我方**上次写入**的内容 |
| ``remote_norm_hash`` | 当前从日历读回的内容 |
| ``local_norm_hash`` | 我方**当前待写入**的内容 |

## 判断链（四个分支，全部必须实现）

```
远端事件不存在（404）          → NOT_FOUND（由 NOT_FOUND_POLICY 决定去向）
etag 未变                      → NO_CHANGE（无操作）
etag 变:
    remote == snapshot         → BENIGN_EVOLUTION（服务端改写，可安全更新）
    remote != snapshot:
        local == snapshot      → EXTERNALLY_MODIFIED（纯外部改动 → 冻结）
        local != snapshot      → CONFLICT（双方都改 → 冻结）
```

**为什么不能用 etag 直接判断**：服务端自身更新（重排字段、刷新元数据）也会
改 etag。只有继续比对内容哈希才能区分「服务端改写」与「用户手改」。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from .normalize import diff_payloads


class OwnershipVerdict(enum.StrEnum):
    """三方比对的结论。"""

    NO_CHANGE = "no_change"
    """etag 未变——远端自上次检查以来没有变化。"""

    BENIGN_EVOLUTION = "benign_evolution"
    """远端内容与我方快照规范化后一致，只是服务端改写了表示 → 可安全更新。"""

    EXTERNALLY_MODIFIED = "externally_modified"
    """远端内容变了而本地没变 → 疑似用户手改 → **冻结**。"""

    CONFLICT = "conflict"
    """远端与本地都变了 → **冻结**，需人工裁决。"""

    NOT_FOUND = "not_found"
    """远端事件已不存在。"""

    NOT_OWNED = "not_owned"
    """远端事件没有我方的 ``auto_mail_key`` 标记 → 不是我们创建的，不得触碰。"""


@dataclass(slots=True)
class ComparisonResult:
    """比对结果。"""

    verdict: OwnershipVerdict
    reason: str
    differences: dict[str, tuple[Any, Any]]
    """规范化后的实际差异字段 ``{字段: (我方值, 远端值)}``。

    判定为 ``externally_modified``/``conflict`` 时，这是使用者最想看的信息——
    「到底哪里不一样」。审核界面直接展示它。
    """

    @property
    def frozen(self) -> bool:
        """是否需要冻结（禁止更新与删除）。"""
        return self.verdict in {
            OwnershipVerdict.EXTERNALLY_MODIFIED,
            OwnershipVerdict.CONFLICT,
            OwnershipVerdict.NOT_OWNED,
        }

    @property
    def may_write(self) -> bool:
        """是否允许写入（更新/删除）。"""
        return self.verdict in {
            OwnershipVerdict.NO_CHANGE,
            OwnershipVerdict.BENIGN_EVOLUTION,
            OwnershipVerdict.NOT_FOUND,
        }


def compare(
    *,
    remote: dict[str, Any] | None,
    snapshot_hash: str | None,
    local_hash: str | None,
    remote_etag: str | None = None,
    last_etag: str | None = None,
    expected_auto_mail_key: str | None = None,
    remote_exists: bool = True,
    snapshot_payload: dict[str, Any] | None = None,
) -> ComparisonResult:
    """执行三方比对。

    Args:
        remote: 远端事件的 payload；``None`` 表示不存在。
        snapshot_hash: 我方上次写入时的规范化哈希。
        local_hash: 我方当前待写入内容的规范化哈希。
        remote_etag: 当前远端 etag。
        last_etag: 我方上次记录到的 etag。
        expected_auto_mail_key: 我方该事件的 ``auto_mail_key``。若非空，
            会校验远端事件确实带此标记（所有权检查）。
        remote_exists: 远端事件是否存在。
        snapshot_payload: 我方上次写入的完整 payload。**仅用于展示差异**——
            没有它，差异只能显示远端值，使用者不知道我方原本是什么。
    """
    from .normalize import extract_auto_mail_key, normalize_hash

    # ── 1. 不存在 ──
    if not remote_exists or remote is None:
        return ComparisonResult(
            verdict=OwnershipVerdict.NOT_FOUND,
            reason="远端事件不存在（可能被删除）",
            differences={},
        )

    # ── 2. 所有权检查（在进行任何比较之前）──
    #
    # 必须在最前面：若远端事件根本不是我们创建的，无论内容是否相同，
    # 都不得更新或删除。这是「只动自己创建的事件」的硬性保证。
    if expected_auto_mail_key:
        actual_key = extract_auto_mail_key(remote)
        if actual_key != expected_auto_mail_key:
            return ComparisonResult(
                verdict=OwnershipVerdict.NOT_OWNED,
                reason=(
                    f"远端事件的所有权标记不匹配（期望 {expected_auto_mail_key}，"
                    f"实际 {actual_key}）——该事件可能已被他人重建或替换，不得触碰"
                ),
                differences={},
            )

    remote_hash = normalize_hash(remote)

    # ── 3. etag 未变 ──
    if last_etag and remote_etag and remote_etag == last_etag:
        return ComparisonResult(
            verdict=OwnershipVerdict.NO_CHANGE,
            reason="etag 未变，远端自上次检查以来无变化",
            differences={},
        )

    # ── 4. etag 变了 → 比对内容 ──
    #
    # etag 变化**不等于**用户手改：服务端自身更新也会改 etag。
    # 必须继续比对内容哈希。
    if snapshot_hash and remote_hash == snapshot_hash:
        return ComparisonResult(
            verdict=OwnershipVerdict.BENIGN_EVOLUTION,
            reason="内容与我方快照一致（仅服务端改写了表示）→ 可安全更新",
            differences={},
        )

    differences = _safe_diff(remote, snapshot_payload)

    if local_hash is not None and snapshot_hash is not None and local_hash != snapshot_hash:
        # 本地也变了，且远端也变了（因为 remote != snapshot）
        return ComparisonResult(
            verdict=OwnershipVerdict.CONFLICT,
            reason="远端与本地都相对我方快照有改动 → 拒绝写入，需人工裁决",
            differences=differences,
        )

    # 本地未变，远端变了 → 纯外部改动
    return ComparisonResult(
        verdict=OwnershipVerdict.EXTERNALLY_MODIFIED,
        reason="远端内容变了而本地未变 → 疑似用户手工修改，冻结更新与删除",
        differences=differences,
    )


def _safe_diff(remote: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, tuple[Any, Any]]:
    """给出差异。快照未保存完整内容时只能报告远端当前值。

    真实的差异方向需要保存快照 payload；v1 只保存快照哈希（省空间），
    因此这里尽可能给出信息：列出远端的关键字段当前值。
    """
    if snapshot is not None:
        return diff_payloads(snapshot, remote)

    from .normalize import canonical_payload

    canonical = canonical_payload(remote)
    return {field: (None, value) for field, value in sorted(canonical.items())}


def describe_verdict(result: ComparisonResult) -> str:
    """给使用者看的一句话说明（含差异摘要）。"""
    if not result.differences:
        return result.reason
    parts = []
    for field, (mine, theirs) in list(result.differences.items())[:4]:
        parts.append(f"{field}: 我方={mine!r} → 远端={theirs!r}")
    suffix = "；".join(parts)
    more = len(result.differences) - 4
    if more > 0:
        suffix += f"（另有 {more} 个字段不同）"
    return f"{result.reason}；差异：{suffix}"
