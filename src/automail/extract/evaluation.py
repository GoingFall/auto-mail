"""离线量化评估：P2 的门控。

按规格要求，P2 一开工先出量化报告，**达标才进 P3**（审核队列）。

评测以**事件**为单位（不是邮件）：一封邮件可能含 0 个或多个事件，
按邮件计分无法反映「漏了一个事件」这类错误。

指标：
* **召回（recall）**：真值事件中被抽出的比例 —— 漏识别
* **精确（precision）**：抽出的候选中命中真值的比例 —— 误报
* **F1**：两者的调和平均
* **自动入历准确率**：被标为「可自动入历」的候选中，真正正确的比例
  —— 这是**最关键的指标**，因为它直接决定会不会污染用户日历
* **待审率**：候选进入人工审核的比例（越高越安全但越费人工）

匹配规则：候选与真值的标题关键字匹配 **且** 日期相同，**且**（若真值给了
时刻）时刻在容差内。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .pipeline import Candidate, ExtractionOutcome

#: 「同一天」判定允许的日期偏移（天）。0 表示必须精确同天。
DATE_TOLERANCE_DAYS = 0


@dataclass(slots=True)
class MatchResult:
    """单封邮件的匹配结果。"""

    sample_name: str
    expected_count: int
    candidate_count: int
    matched: int
    missed: list[str] = field(default_factory=list)
    spurious: list[str] = field(default_factory=list)
    auto_push_total: int = 0
    auto_push_correct: int = 0
    auto_push_wrong: list[str] = field(default_factory=list)
    review_count: int = 0


@dataclass(slots=True)
class EvalReport:
    """整体评测报告。"""

    per_sample: list[MatchResult] = field(default_factory=list)

    # ── 聚合指标 ──

    @property
    def expected_total(self) -> int:
        return sum(r.expected_count for r in self.per_sample)

    @property
    def candidate_total(self) -> int:
        return sum(r.candidate_count for r in self.per_sample)

    @property
    def matched_total(self) -> int:
        return sum(r.matched for r in self.per_sample)

    @property
    def recall(self) -> float:
        return self.matched_total / self.expected_total if self.expected_total else 1.0

    @property
    def precision(self) -> float:
        return self.matched_total / self.candidate_total if self.candidate_total else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def auto_push_total(self) -> int:
        return sum(r.auto_push_total for r in self.per_sample)

    @property
    def auto_push_correct(self) -> int:
        return sum(r.auto_push_correct for r in self.per_sample)

    @property
    def auto_push_accuracy(self) -> float:
        """自动入历准确率 —— **最关键的指标**（直接关乎污染用户日历的风险）。"""
        return (
            self.auto_push_correct / self.auto_push_total
            if self.auto_push_total
            else 1.0
        )

    @property
    def false_positive_samples(self) -> list[MatchResult]:
        """本不该抽出任何事件、却抽出了的邮件（误报重灾区）。"""
        return [r for r in self.per_sample if r.expected_count == 0 and r.candidate_count > 0]

    @property
    def missed_all(self) -> list[tuple[str, str]]:
        return [(r.sample_name, m) for r in self.per_sample for m in r.missed]

    @property
    def spurious_all(self) -> list[tuple[str, str]]:
        return [(r.sample_name, s) for r in self.per_sample for s in r.spurious]

    def render(self) -> str:
        """渲染成 Markdown 报告（写盘与贴聊天都用它）。"""
        lines: list[str] = []
        lines.append("# 抽取质量评估报告")
        lines.append("")
        lines.append("> 由 `python -m tests.evaluate` 生成；语料为合成数据（`tests/corpus.py`）。")
        lines.append("")
        lines.append("## 总体指标")
        lines.append("")
        lines.append("| 指标 | 值 | 说明 |")
        lines.append("|---|---|---|")
        lines.append(
            f"| 召回率 | **{self.recall:.1%}** | 真值事件被抽出的比例（漏识别） |"
        )
        lines.append(
            f"| 精确率 | **{self.precision:.1%}** | 候选中命中真值的比例（误报） |"
        )
        lines.append(f"| F1 | **{self.f1:.3f}** | 召回与精确的调和平均 |")
        lines.append(
            f"| 自动入历准确率 | **{self.auto_push_accuracy:.1%}** | "
            f"{self.auto_push_correct}/{self.auto_push_total} —— 最关键指标 |"
        )
        lines.append("")
        lines.append("## 计数")
        lines.append("")
        lines.append(f"- 语料邮件数：{len(self.per_sample)}")
        lines.append(f"- 真值事件数：{self.expected_total}")
        lines.append(f"- 抽出候选数：{self.candidate_total}")
        lines.append(f"- 正确匹配：{self.matched_total}")
        lines.append(f"- 可自动入历：{self.auto_push_total}")
        lines.append("")

        if self.false_positive_samples:
            lines.append("## ⚠️ 误报：本不该抽出事件的邮件")
            lines.append("")
            for r in self.false_positive_samples:
                lines.append(f"- `{r.sample_name}`：抽出 {r.candidate_count} 个候选")
                for s in r.spurious:
                    lines.append(f"    - {s}")
            lines.append("")

        if self.missed_all:
            lines.append("## ⚠️ 漏识别：真值中未被抽出的事件")
            lines.append("")
            for name, desc in self.missed_all:
                lines.append(f"- `{name}`：{desc}")
            lines.append("")

        if self.spurious_all:
            lines.append("## 多余候选（已在误报中列出，此处按事件展开）")
            lines.append("")
            for name, desc in self.spurious_all:
                lines.append(f"- `{name}`：{desc}")
            lines.append("")

        auto_push_violations: list[tuple[str, str]] = []
        for r in self.per_sample:
            for desc in r.auto_push_wrong:
                auto_push_violations.append((r.sample_name, desc))
        if auto_push_violations:
            lines.append("## 🚨 危险的自动入历（会污染日历）")
            lines.append("")
            for name, desc in auto_push_violations:
                lines.append(f"- `{name}`：{desc}")
            lines.append("")

        lines.append("## 逐样本明细")
        lines.append("")
        lines.append("| 样本 | 真值 | 候选 | 匹配 | 自动入历 | 待审 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for r in self.per_sample:
            lines.append(
                f"| `{r.sample_name}` | {r.expected_count} | {r.candidate_count} "
                f"| {r.matched} | {r.auto_push_total} | {r.review_count} |"
            )
        lines.append("")
        return "\n".join(lines)


def evaluate_sample(
    sample_name: str,
    outcome: ExtractionOutcome,
    expected: list[object],
    *,
    user_timezone: str = "Asia/Shanghai",
) -> MatchResult:
    """把一封邮件的抽取结果与真值比对。"""
    tz = ZoneInfo(user_timezone)
    result = MatchResult(
        sample_name=sample_name,
        expected_count=len(expected),
        candidate_count=len(outcome.candidates),
        matched=0,
    )

    remaining = list(expected)
    matched_candidates: set[int] = set()

    for index, candidate in enumerate(outcome.candidates):
        hit_index = _find_matching(candidate, remaining, tz)
        if hit_index is None:
            result.spurious.append(_describe_candidate(candidate, tz))
            continue
        matched_candidates.add(index)
        result.matched += 1
        remaining.pop(hit_index)

    for exp in remaining:
        result.missed.append(_describe_expected(exp))

    # 自动入历准确性：只有「未标记待审」的候选才算自动写入
    for index, candidate in enumerate(outcome.candidates):
        if candidate.requires_review:
            result.review_count += 1
            continue
        result.auto_push_total += 1
        if index in matched_candidates:
            result.auto_push_correct += 1
        else:
            result.auto_push_wrong.append(_describe_candidate(candidate, tz))

    return result


def _find_matching(
    candidate: Candidate, expected: list[object], tz: ZoneInfo
) -> int | None:
    """在真值中找与候选匹配的项，返回索引；无匹配返回 None。"""
    for index, exp in enumerate(expected):
        if _matches(candidate, exp, tz):
            return index
    return None


def _matches(candidate: Candidate, expected: object, tz: ZoneInfo) -> bool:
    """候选是否命中真值。

    标题关键字在 **标题或 evidence** 中匹配即可——评测要回答的是
    「有没有找到这个事件」，而不是「标题措辞是否漂亮」。事件标题的最终措辞
    由用户在审核时确定（也可能走 ICS 的官方 SUMMARY），不应由评测强行约束。
    """
    keyword = getattr(expected, "title_contains", "")
    exp_date = getattr(expected, "start_date", None)
    exp_time = getattr(expected, "start_time", None)
    tolerance = getattr(expected, "tolerance_minutes", 0) or 0

    if keyword:
        haystack = f"{candidate.title or ''}\n{candidate.evidence or ''}".casefold()
        if keyword.casefold() not in haystack:
            return False

    if candidate.start_ts is None:
        return False
    try:
        start = datetime.strptime(candidate.start_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        return False

    local = start.astimezone(tz)
    if exp_date is not None:
        delta_days = abs((local.date() - exp_date).days)
        if delta_days > DATE_TOLERANCE_DAYS:
            return False

    if exp_time:
        hour, minute = (int(x) for x in exp_time.split(":"))
        actual = local.hour * 60 + local.minute
        wanted = hour * 60 + minute
        if abs(actual - wanted) > max(tolerance, 0):
            return False

    return True


def _describe_candidate(candidate: Candidate, tz: ZoneInfo) -> str:
    when = "(无时间)"
    if candidate.start_ts:
        try:
            start = datetime.strptime(candidate.start_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
            when = start.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when = candidate.start_ts
    label = "全天" if candidate.all_day else ""
    return f"[{candidate.source.value}] {when} {label} {candidate.title[:40]}"


def _describe_expected(expected: object) -> str:
    date = getattr(expected, "start_date", None)
    time = getattr(expected, "start_time", None) or "(无时刻)"
    kind = getattr(expected, "kind", "event")
    title = getattr(expected, "title_contains", "")
    return f"{date} {time} [{kind}] {title}"


def build_report(results: list[MatchResult]) -> EvalReport:
    return EvalReport(per_sample=results)
