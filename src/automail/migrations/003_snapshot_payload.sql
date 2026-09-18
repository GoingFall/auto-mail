-- 003: 保存快照 payload，让差异展示有意义
--
-- 背景：events 表原本只存 snapshot_hash。判定为 externally_modified /
-- conflict 时，审核界面需要展示「我方原本是什么 → 远端现在是什么」，
-- 只有哈希做不到这一点（只能显示远端值，我方值缺失）。
--
-- 存的是**规范化前的原始 payload 的 JSON**，体积可控（一个事件通常几百字节），
-- 且只在真正推送过的事件上才有值。它不含邮件正文，因此不扩大隐私面。

ALTER TABLE events ADD COLUMN snapshot_payload TEXT;
