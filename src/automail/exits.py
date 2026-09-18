"""CLI 退出码约定。

供 Windows 任务计划判断运行结果：
* 0 正常
* 1 部分缺失（例如尚未配置密钥）——不算失败，可继续开发/运行离线部分
* 2 致命（配置非法、依赖缺失、数据库不可用）
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_FATAL = 2
