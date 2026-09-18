"""后台任务线程：把耗时操作移出界面线程。

**两条硬约束**（违反任意一条都会导致随机崩溃或挂死）：

1. **Tk 只能由主线程操作。** 后台线程绝不能直接改控件——它必须把结果放进
   队列，由主线程用 ``after()`` 取出来渲染。这不是"最佳实践"，是 tkinter 的
   硬性限制。
2. **线程必须是非守护的（non-daemon）。** 守护线程会在主线程退出时被直接
   杀掉，可能留下半成品事务与**未释放的运行锁**。锁在 ``finally`` 里释放，
   被强杀就释放不了，只能等 TTL（默认 30 分钟）过期——期间所有运行都会报
   "已有运行在进行中"。

因此这个模块刻意做得很小：一个非守护线程、一个队列、一种"提交任务"的接口，
所有线程安全的判断都集中在这里，界面层不需要自己操心。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("automail.gui.worker")


@dataclass(slots=True)
class TaskResult:
    """一个后台任务的最终结果。"""

    name: str
    value: Any = None
    error: BaseException | None = None
    detail: str = ""
    """已格式化好的异常堆栈。

    由工作线程用 ``traceback.format_exc()`` 捕获后带过来——**不能**在工作线程里
    直接 ``logger.exception``（会与主线程 GC 竞争 CPython 的 ABC 缓存并崩溃，
    见 ``_loop`` 的说明）。日志由主线程记录。
    """

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(slots=True)
class ProgressEvent:
    """进度消息（由 ``ProgressReporter`` 的回调产生）。

    与 :class:`TaskResult` 分开，因为界面对它们的处理完全不同：进度是高频的、
    只更新状态栏；结果是低频的、要重建列表。
    """

    stage: str
    index: int
    total: int
    message: str = ""
    kind: str = "stage"
    """``stage``（阶段切换）或 ``progress``（阶段内推进）。"""


@dataclass(slots=True)
class LogRecord:
    """日志行（供只读日志页与状态栏尾部显示）。"""

    level: int
    text: str


class Worker:
    """单后台线程 + 事件队列。

    为什么只要一个线程：本项目的耗时任务都是"整条流水线"（同步→抽取→推送），
    并发跑多个没有意义，反而会互相争抢那把单实例锁。串行还能让进度显示更诚实
    ——不会出现两个任务的进度交错。

    **线程退出语义**（踩过的坑，务必保持）：

    线程是非守护的（为了不让运行中的任务被强杀、进而让运行锁滞留），但
    **非守护线程会阻塞解释器退出**：一个阻塞在 ``queue.get()`` 上的非守护线程
    会让进程永远退不掉。实测过——测试跑不完、进程挂死。

    因此循环用 ``get(timeout=...)`` 轮询并检查停止标志，:meth:`shutdown` 只
    **请求**停止（当前任务跑完才退），绝不中途掐断正在执行的任务。
    """

    #: 轮询停止标志的间隔（秒）。200ms 的唤醒开销可忽略，
    #: 但能让"退出请求"及时被看到——否则又要挂住。
    IDLE_POLL_SECONDS = 0.2

    def __init__(self) -> None:
        self._tasks: queue.Queue[Callable[[], Any] | None] = queue.Queue()
        #: 传给界面线程的事件（进度 / 结果 / 日志），主线程用 after() 消费
        self.events: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._busy = threading.Event()
        self._stopping = threading.Event()
        self._current: str = ""
        self._started = False

    # ── 生命周期 ──────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        # daemon=False：**刻意**。守护线程会在主线程退出时被强杀，
        # 导致运行锁无法在 finally 里释放（见模块文档）。
        self._thread = threading.Thread(
            target=self._loop, name="automail-gui-worker", daemon=False
        )
        self._thread.start()
        self._started = True
        # 兜底：万一某条退出路径忘了调 shutdown()，非守护线程会让解释器
        # 永远退不掉（实测挂死过）。这里注册一次 atexit 请求停止，
        # 保证"忘了调"只表现为"任务跑完才退"，而不是进程挂住。
        import atexit

        atexit.register(self.request_stop)

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                task = self._tasks.get(timeout=self.IDLE_POLL_SECONDS)
            except queue.Empty:
                # 没有任务：回到循环顶部检查停止标志。
                # 不能用阻塞式 get()——那样退出请求永远看不到，进程会挂死。
                continue
            if task is None:  # 关闭信号
                break
            try:
                value = task()
                self.events.put(TaskResult(name=self._current, value=value))
            except BaseException as exc:  # noqa: BLE001 - 任务异常不能让线程死掉
                # **这里刻意不调用 logging**。
                #
                # 两条理由，第一条是实测出来的硬故障：
                #
                # 1. 从后台线程调用 logging 会与主线程的 GC 竞争 CPython 的 ABC
                #    缓存（``_collections_abc.__subclasshook__``），在
                #    ``LogRecord.__init__`` 做 isinstance 检查时**直接崩溃整个
                #    解释器**（实测：Windows fatal exception 0x80000003，
                #    栈顶正是 ``logger.exception``）。不是偶发理论问题——它把
                #    整个测试运行打断了。
                # 2. 架构上也更对：worker 只负责把结果放进队列，**由主线程统一
                #    记录日志**。这样日志顺序与实际处理顺序一致，也避免多线程
                #    日志带来的交错。
                #
                # ``traceback.format_exc`` 是纯 Python 字符串拼接，不碰 logging
                # 与 ABC，因此安全；堆栈信息一并带给主线程，诊断能力不损失。
                import traceback as _tb

                detail = _tb.format_exc()
                self.events.put(
                    TaskResult(name=self._current, error=exc, detail=detail)
                )
            finally:
                self._busy.clear()
                self._current = ""

    def submit(self, name: str, fn: Callable[[], Any]) -> bool:
        """提交任务；已有任务在跑时返回 ``False``（不排队）。

        不排队是刻意的：界面上的按钮在忙碌时应当禁用，而不是让使用者点五下
        攒出五个任务。返回 ``False`` 让调用方据此提示"正在运行中"。

        **线程已停止时同样返回 ``False``，绝不放行**。这是实测踩到的 bug：
        停机后 ``submit`` 仍返回 ``True``，但线程已经退出了，任务被静默丢弃，
        而且 ``_busy`` 再也不会被清掉——界面从此永久显示"正在运行"，
        之后每个动作都被当成"繁忙"拒绝。宁可明确拒绝，也不要静默丢弃。
        """
        if self._stopping.is_set():
            logger.warning("worker 已停止，拒绝任务：%s", name)
            return False
        thread = self._thread
        if thread is not None and not thread.is_alive():
            logger.warning("worker 线程已退出，拒绝任务：%s", name)
            return False
        if self.busy:
            return False

        self._current = name
        self._busy.set()
        self._tasks.put(fn)
        return True

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    @property
    def current_task(self) -> str:
        return self._current

    def request_stop(self) -> None:
        """请求线程退出（当前任务跑完才退，不中途掐断）。

        与 :meth:`shutdown` 分开是为了让"退出请求"在任何时机都安全——
        包括有任务在跑时（此时只是标记，任务结束后线程自行退出）。
        """
        self._stopping.set()

    def shutdown(self, *, wait_seconds: float = 5.0) -> bool:
        """请求退出并等待，返回线程是否已退出。

        **不会中断正在执行的任务**：若任务在跑，只能在它结束后退出，
        因此可能返回 ``False``。这是刻意的——强杀任务会让运行锁滞留到 TTL
        （默认 30 分钟），期间计划任务全部报"已有运行在进行中"。
        """
        self._stopping.set()
        thread = self._thread
        if thread is None:
            return True
        if thread.is_alive():
            thread.join(timeout=wait_seconds)
        return not thread.is_alive()

    # ── 事件消费（主线程调用） ─────────────────────────────

    def drain(self, *, limit: int = 200) -> list[Any]:
        """取出待处理事件（非阻塞）。

        界面上用 ``after()`` 定期调用它——这是"后台线程绝不碰控件"的实现方式：
        线程只往队列里放，主线程主动取。
        """
        drained: list[Any] = []
        for _ in range(limit):
            try:
                drained.append(self.events.get_nowait())
            except queue.Empty:
                break
        return drained

    # ── 进度回调工厂 ──────────────────────────────────────

    def reporter(self) -> Any:
        """构造接到本 worker 的 ``ProgressReporter``。

        回调在**工作线程**里被调用，因此这里只做一件事：把事件塞进队列。
        绝不在这里碰 Tk——那正是模块文档第一条约束要防的。
        """
        from ..progress import ProgressReporter

        def on_stage(name: str, index: int, total: int) -> None:
            self.events.put(
                ProgressEvent(stage=name, index=index, total=total, kind="stage")
            )

        def on_progress(name: str, done: int, total: int) -> None:
            self.events.put(
                ProgressEvent(stage=name, index=done, total=total, kind="progress")
            )

        return ProgressReporter(on_stage=on_stage, on_progress=on_progress)


@dataclass(slots=True)
class WorkerFacade:
    """把 worker 与"是否允许提交"的策略绑在一起，便于界面调用。

    界面上常见的写法是「点击 → 若忙碌则提示，否则提交」。把这段策略收在这里，
    面板里只调 :meth:`run`，避免每个按钮各写一遍判断。
    """

    worker: Worker
    events_seen: list[str] = field(default_factory=list)

    def run(self, name: str, fn: Callable[[], Any]) -> bool:
        return self.worker.submit(name, fn)
