"""进度回调：让长时间运行能被观察到。

**为什么需要**：首次同步一个已有历史的邮箱要几分钟。命令行下使用者至少能看到
滚动输出，但图形界面里什么都没有——窗口看起来像卡死了，人只会去点关闭或强杀。
（强杀尤其糟：运行锁在 ``finally`` 里释放，强杀会让锁滞留到 TTL，默认 30 分钟。）

回调刻意设计成**两个粒度**：

* ``on_stage(name, index, total)`` —— 阶段级（同步/抽取/推送/已读回写）
* ``on_progress(name, done, total)`` —— 批级或逐封级

只有阶段级是不够的：一个阶段内部可能要跑几分钟，期间界面仍是静止的。
两者都用「名称 + 已完成 + 总数」的统一形状，界面侧可以共用一套渲染逻辑。

**线程归属**：回调在被调用方的线程里同步执行。图形界面必须把它接到
队列上、再回到主线程更新控件——Tk 不是线程安全的，从工作线程直接改控件
会随机崩溃。这条约束是调用方的责任，这里只保证「回调在正确的时机被调用」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class StageCallback(Protocol):
    """阶段开始/结束时调用。``index`` 从 1 开始。"""

    def __call__(self, name: str, index: int, total: int) -> None: ...


class ProgressCallback(Protocol):
    """阶段内部推进时调用。``done`` 单调不减，``total`` 为 0 表示未知总量。"""

    def __call__(self, name: str, done: int, total: int) -> None: ...


@dataclass(slots=True)
class ProgressReporter:
    """把回调收敛成一处的轻量包装。

    存在的意义是**让 ``None`` 情况下的调用点保持干净**：内部各处直接写
    ``reporter.stage(...)``，不必到处 ``if callback is not None`` 判空。
    传 ``None`` 时全部是空操作，因此不改变既有行为。
    """

    on_stage: StageCallback | None = None
    on_progress: ProgressCallback | None = None

    def stage(self, name: str, index: int, total: int) -> None:
        if self.on_stage is not None:
            self.on_stage(name, index, total)

    def progress(self, name: str, done: int, total: int) -> None:
        if self.on_progress is not None:
            self.on_progress(name, done, total)

    @property
    def active(self) -> bool:
        return self.on_stage is not None or self.on_progress is not None


#: 阶段名 → 中文标签。图形界面与命令行共用，避免两处各写一份。
STAGE_LABELS = {
    "sync": "同步邮件",
    "extract": "抽取事件",
    "push": "写入日历",
    "mark-read": "标记已读",
    "digest": "生成摘要",
}


def stage_label(name: str) -> str:
    return STAGE_LABELS.get(name, name)
