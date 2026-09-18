"""日志配置。

隐私约束
--------
日志默认只记元数据（UID、主题、发件人）。本模块的过滤器会剥离控制字符
（含 ANSI 转义）并限长——既防终端注入，也避免有人不小心把正文塞进日志时
被原样落盘。

调用方仍需自律：**不要**把 ``body_excerpt`` 或含正文的异常写进日志。
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .sanitize import LOG_MAX_CHARS, sanitize_text, strip_control
from .settings import Settings

__all__ = ["LOG_MAX_CHARS", "sanitize_text", "setup_logging", "strip_control"]


class _SanitizingFilter(logging.Filter):
    """把日志消息压成单条安全文本。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 格式化失败不应让日志系统本身崩掉
            return True
        # 覆盖 msg/args，避免 handler 再次格式化时取回原文
        record.msg = sanitize_text(message)
        record.args = ()
        return True


def setup_logging(settings: Settings, *, console: bool = True) -> logging.Logger:
    """配置 ``automail`` logger 并返回。

    文件日志写 ``logs/automail.log``（轮转 5×1MB）；控制台只放 WARNING 以上，
    避免与 CLI 的 rich 输出打架。
    """
    logger = logging.getLogger("automail")
    logger.setLevel(settings.log_level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = _SanitizingFilter()

    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            Path(settings.log_dir) / "automail.log",
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)
    except OSError:
        # 日志目录不可写不应阻断主流程；doctor 会单独报告目录可写性
        pass

    if console:
        stream = logging.StreamHandler()
        stream.setLevel(logging.WARNING)
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        logger.addHandler(stream)

    return logger
