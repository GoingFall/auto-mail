"""邮件相关：MIME 解析、头部解析、IMAP 后端与同步。

分层：
* :mod:`automail.mail.charset`  —— 字节解码与字符集兜底
* :mod:`automail.mail.headers`  —— RFC 2047 解码、Message-ID 规范化、退订头
* :mod:`automail.mail.mime`     —— MIME 遍历、正文清洗、ICS 收集
* :mod:`automail.mail.backend`  —— 邮件后端协议（便于测试注入）
* :mod:`automail.mail.imap_backend` —— 163/Coremail 适配实现
* :mod:`automail.mail.sync`     —— 增量同步算法
"""
