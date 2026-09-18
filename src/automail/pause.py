"""暂停自动运行。

计划任务每 30 分钟跑一次 ``run``。有时需要它停下来（改配置、排查问题、
出差期间不想让它动邮箱），但又**不想卸载计划任务**——卸载了还得记得装回来。

因此用一个标记文件表达状态，而不是改计划任务本身：

* 暂停 = 创建 ``data/AUTOMATED_RUN_PAUSED``
* 恢复 = 删除该文件

**作用域是刻意的：只挡计划任务触发的 ``run``，不挡图形界面里的「立即同步」。**
理由是使用者的意图不同：点「立即同步」说明他现在就想跑一次；而暂停表达的是
"别在我不知情的时候自动跑"。若连手动也挡，就得先恢复再点，反而多一步且容易
忘了恢复。

用文件而不是数据库字段：数据库可能正被另一个进程锁着，而这个检查发生在
``run`` 的最开始、应当尽量不依赖别的东西。文件也便于使用者手工创建/删除。
"""

from __future__ import annotations

from pathlib import Path

from .settings import Settings

#: 标记文件名（位于 ``data/`` 下，已被 .gitignore 覆盖）。
PAUSE_FILE_NAME = "AUTOMATED_RUN_PAUSED"

#: 文件内容（便于打开文件的人立刻明白它是干什么的）
PAUSE_FILE_CONTENT = (
    "自动运行已暂停。\n"
    "删除本文件即可恢复（或使用图形界面的「暂停自动运行」开关）。\n"
    "注意：这只影响计划任务；图形界面里手点的「立即同步」不受影响。\n"
)


def pause_file(settings: Settings) -> Path:
    return settings.data_dir / PAUSE_FILE_NAME


def is_paused(settings: Settings) -> bool:
    """当前是否暂停了自动运行。"""
    return pause_file(settings).is_file()


def set_paused(settings: Settings, paused: bool) -> bool:
    """设置暂停状态，返回**是否发生了改变**。

    Raises:
        OSError: 无法创建或删除标记文件（调用方应把原因显示给使用者）。
    """
    path = pause_file(settings)
    if paused:
        if path.is_file():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PAUSE_FILE_CONTENT, encoding="utf-8")
        return True

    if not path.is_file():
        return False
    path.unlink()
    return True


def toggle(settings: Settings) -> bool:
    """切换暂停状态，返回切换**之后**的状态。"""
    new_state = not is_paused(settings)
    set_paused(settings, new_state)
    return new_state
