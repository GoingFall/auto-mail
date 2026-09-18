"""文本清洗：控制字符、ANSI 转义、异常压缩。

单独成模块的理由：这套规则同时服务于**日志**、**数据库写入**和**终端渲染**。
若放在 ``logging_setup`` 里，``db`` 与 ``mail`` 就得反向依赖日志模块，层次会乱。

安全背景：邮件标题、地点、异常文本都可能来自外部输入。不过滤控制字符时，
一段带 ESC 序列的标题会在终端里执行颜色/光标控制（ANSI 注入）。
"""

from __future__ import annotations

import re

#: ANSI CSI 转义序列，以及 C0/C1 控制字符（保留 \t \n \r）
_CONTROL_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # ANSI CSI 转义
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"  # 其余控制字符
)

#: 日志单条消息的最大长度
LOG_MAX_CHARS = 1000

#: runs.error 列的最大长度，防止异常栈把整段正文带进数据库
ERROR_MAX_CHARS = 500

_WHITESPACE_RE = re.compile(r"[ \t]+")


def strip_control(value: str) -> str:
    """移除 ANSI 转义序列与 C0/C1 控制字符（保留 \\t \\n \\r）。

    这是防终端注入的底线。
    """
    return _CONTROL_RE.sub("", value)


def sanitize_text(value: str, *, limit: int = LOG_MAX_CHARS) -> str:
    """剥离控制字符并限长，供日志与终端渲染使用。"""
    cleaned = strip_control(value)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return cleaned


def sanitize_error(exc: object) -> str:
    """把异常压成一行、限长，用于写入 ``runs.error``。

    同时剥离控制字符——异常消息里可能带回显了邮件片段的文本，
    直接落盘会污染数据并造成终端注入。

    注意：本函数只做清洗，**不负责**判断内容是否含正文。
    调用方仍有责任不把 ``body_excerpt`` 传进来。
    """
    text = strip_control(str(exc)).replace("\r", " ").replace("\n", " ")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > ERROR_MAX_CHARS:
        text = text[:ERROR_MAX_CHARS] + "…"
    return text
