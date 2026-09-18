"""离线评测入口：`python -m tests.evaluate`。

运行语料的完整抽取流程（**全程离线，LLM 用 mock**），产出 Markdown 报告。
这是 P2 的门控：报告达标才进入 P3（审核队列）。

用法::

    python -m tests.evaluate              # 打印报告
    python -m tests.evaluate --write      # 同时写入 docs/eval-report.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC
from pathlib import Path
from zoneinfo import ZoneInfo

# 允许 `python -m tests.evaluate` 从仓库根直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpus import BASE_RECEIVED, build_corpus  # noqa: E402

from automail.extract.evaluation import build_report, evaluate_sample  # noqa: E402
from automail.extract.llm import LlmEvent, LlmResult  # noqa: E402
from automail.extract.pipeline import extract_from_raw  # noqa: E402

#: 评测使用的固定「当前时刻」= 语料基准收信时间。
#: 固定它是为了让评测**可复现**——否则真实时间推移会让语料里的事件
#: 逐渐变成「过去」，触发「时间已过 → 待审」规则，指标凭空波动。
EVAL_NOW = BASE_RECEIVED


class ScriptedLlm:
    """按样本内容返回预置结果的假 LLM。

    刻意**不**调用真实 API：评测必须可离线、可复现、零成本。
    它模拟「LLM 能看懂自由文本但可能漏」的真实特性。
    """

    def __init__(self, responses: dict[str, list[LlmEvent]] | None = None) -> None:
        self.responses = responses or {}
        self.calls = 0
        self.stats = type("S", (), {"calls": 0, "failures": 0, "skipped_budget": 0, "input_chars": 0})()

    def extract(self, *, excerpt: str, subject: str | None, received_at, user_timezone):
        self.calls += 1
        self.stats.calls += 1
        # 按主题关键字匹配预置响应
        for key, events in self.responses.items():
            if key.casefold() in (subject or "").casefold():
                return LlmResult(events=events, ok=True)
        return LlmResult(events=[], ok=True)


def build_mock_llm() -> ScriptedLlm:
    """预置「LLM 补规则之漏」的响应。

    覆盖规则难以处理的表达（英文自然语言、模糊相对时间）。
    """
    return ScriptedLlm(
        responses={
            # 「下周三下午2点」——规则能处理，但 mock 也给出结果以验证合并
            "面试通知": [
                LlmEvent(
                    title="面试",
                    start="2026-09-23T14:00:00+08:00",
                    all_day=False,
                    confidence=0.9,
                    evidence="面试安排在下周三下午2点",
                )
            ],
            # 域名到期：规则也命中（无时刻 → 待审）；LLM 给同一结果
            "域名到期": [
                LlmEvent(
                    title="域名续费",
                    start="2026-10-08",
                    all_day=True,
                    confidence=0.85,
                    evidence="将于 2026年10月8日 到期",
                )
            ],
            # 信用卡：规则命中
            "信用卡": [
                LlmEvent(
                    title="信用卡还款",
                    start="2026-09-28",
                    all_day=True,
                    confidence=0.9,
                    evidence="还款到期日为 2026年9月28日",
                )
            ],
        }
    )


def run_evaluation(*, llm: object | None = None) -> str:
    corpus = build_corpus()
    received = BASE_RECEIVED.replace(tzinfo=UTC)
    # 固定「当前时刻」以保证评测可复现：否则随着真实时间推移，语料里的
    # 事件会逐个变成「过去」，评测结果会莫名变化。
    now = EVAL_NOW.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    llm = llm if llm is not None else build_mock_llm()

    results = []
    for sample in corpus.samples:
        outcome = extract_from_raw(
            sample.raw,
            received_at=received,
            user_timezone="Asia/Shanghai",
            llm=llm,
            now=now,
        )
        results.append(
            evaluate_sample(
                sample.name,
                outcome,
                list(sample.expected),
                user_timezone="Asia/Shanghai",
            )
        )

    return build_report(results).render()


def main() -> int:
    parser = argparse.ArgumentParser(description="离线抽取质量评测")
    parser.add_argument("--write", action="store_true", help="写入 docs/eval-report.md")
    args = parser.parse_args()

    report = run_evaluation()
    print(report)

    if args.write:
        target = Path(__file__).resolve().parents[1] / "docs" / "eval-report.md"
        target.write_text(report, encoding="utf-8")
        print(f"\n已写入 {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
