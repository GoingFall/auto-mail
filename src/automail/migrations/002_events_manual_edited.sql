-- 002: 补齐 events.manual_edited
--
-- 背景：001_initial.sql 定义了 field_provenance（字段级来源标记），
-- 但 EventRepository 还需要一个整行级的「用户手工编辑过」标志：
-- 一旦用户改过某事件，重跑抽取就不得覆盖它。
--
-- 迁移纪律：001 已发布且可能已在别人的库上执行过，**不得修改**，
-- 只能新增迁移。用 PRAGMA user_version 保证只执行一次。
--
-- SQLite 的 ALTER TABLE ADD COLUMN 不支持非恒定默认值，因此用 0 作为默认
-- （历史行均视为「未经人工编辑」，这是安全的保守假设：它们还没有被审批过）。

ALTER TABLE events ADD COLUMN manual_edited INTEGER NOT NULL DEFAULT 0;
