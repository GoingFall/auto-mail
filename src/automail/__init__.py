"""auto-mail：自用网易邮箱(163)管理助手。

从邮件正文抽取事件时间并写入 Google 日历；v1 为只读同步 + 受控写入。

设计规格见 docs/ 目录：
  - spec-event-state-machine.md  事件状态与审批规则
  - spec-imap-sync.md            IMAP 同步一致性与 UIDVALIDITY 处理
  - spec-gcal-ownership.md       Google 事件所有权、更新、删除策略
  - spec-unsubscribe-archive.md  退订与归档的逐项确认与失败恢复（v2）
"""

__version__ = "0.1.0"
