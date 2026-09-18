-- auto-mail schema, version 1
--
-- 约定：
--   * 时间戳一律 ISO8601 字符串并统一为 UTC（后缀 Z），比较可用字典序。
--   * 所有状态列都带 CHECK 约束，取值必须与 src/automail/models.py 中的
--     枚举一致；改一处必须改另一处。
--   * 唯一约束服务于幂等：messages 靠 (account,folder,uid_validity,uid)，
--     events 靠 (message_id,fingerprint)，processed_mail 靠 UID 四元组。


-- ───────────────────────────────────────────────────────────────
-- threads：线程（v1 只建图，v2 才做「可能需要回复」提示）
-- ───────────────────────────────────────────────────────────────
CREATE TABLE threads (
    id                INTEGER PRIMARY KEY,
    account           TEXT NOT NULL DEFAULT '163',
    root_message_id   TEXT,                       -- 线程根；缺失时为幽灵节点的键
    subject_norm      TEXT,                       -- 规范化主题，弱关联用
    last_direction    TEXT,                       -- in | out
    last_message_at   TEXT,
    awaiting_since    TEXT,
    link_strength     TEXT,                       -- strong | weak
    status            TEXT,
    reminded_at       TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK (last_direction IS NULL OR last_direction IN ('in', 'out')),
    CHECK (link_strength IS NULL OR link_strength IN ('strong', 'weak'))
);

CREATE INDEX ix_threads_root    ON threads(account, root_message_id);
CREATE INDEX ix_threads_subject ON threads(account, subject_norm);


-- ───────────────────────────────────────────────────────────────
-- messages：同步入库的邮件
--
-- 注意 normalized_message_id 只建【非唯一】索引：同一封邮件可能同时出现在
-- INBOX 与「订阅邮件」等文件夹，若加唯一约束会在合法场景下插入失败。
-- 跨文件夹重复走 duplicate_of / is_canonical 标记，不重跑抽取。
-- ───────────────────────────────────────────────────────────────
CREATE TABLE messages (
    id                    INTEGER PRIMARY KEY,
    account               TEXT NOT NULL DEFAULT '163',
    folder                TEXT NOT NULL,
    uid_validity          INTEGER NOT NULL,
    uid                   INTEGER NOT NULL,

    normalized_message_id TEXT,                   -- 规范化后可空
    duplicate_of          INTEGER REFERENCES messages(id),
    is_canonical          INTEGER NOT NULL DEFAULT 1,

    thread_id             INTEGER REFERENCES threads(id),
    in_reply_to           TEXT,                   -- 线程建图用
    references_ids        TEXT,                   -- 空格分隔的 References 链

    subject               TEXT,
    from_addr             TEXT,
    from_name             TEXT,
    to_addrs              TEXT,
    sent_at               TEXT,
    received_at           TEXT,

    has_ics               INTEGER NOT NULL DEFAULT 0,
    has_unsubscribe       INTEGER NOT NULL DEFAULT 0,
    list_id               TEXT,
    auto_submitted        TEXT,

    -- 只存清洗脱敏后的片段；全文只留哈希
    body_excerpt          TEXT,
    body_sha256           TEXT,
    body_purged           INTEGER NOT NULL DEFAULT 0,

    flags                 TEXT,
    stale                 INTEGER NOT NULL DEFAULT 0,   -- UIDVALIDITY 变化后的遗留
    folder_moved          INTEGER NOT NULL DEFAULT 0,   -- 已移出原文件夹，不重抽
    moved_to_folder       TEXT,
    fetched_at            TEXT,

    extract_status        TEXT NOT NULL DEFAULT 'pending',
    extract_attempts      INTEGER NOT NULL DEFAULT 0,

    CHECK (is_canonical     IN (0, 1)),
    CHECK (has_ics          IN (0, 1)),
    CHECK (has_unsubscribe  IN (0, 1)),
    CHECK (body_purged      IN (0, 1)),
    CHECK (stale            IN (0, 1)),
    CHECK (folder_moved     IN (0, 1)),
    CHECK (extract_status   IN ('pending', 'running', 'done', 'failed')),
    CHECK (extract_attempts >= 0),
    UNIQUE (account, folder, uid_validity, uid)
);

CREATE INDEX ix_msg_nmid     ON messages(account, normalized_message_id);
CREATE INDEX ix_msg_sha      ON messages(account, body_sha256);
CREATE INDEX ix_msg_extract  ON messages(account, extract_status);
CREATE INDEX ix_msg_thread   ON messages(thread_id);
CREATE INDEX ix_msg_received ON messages(account, received_at);


-- ───────────────────────────────────────────────────────────────
-- events：抽取得到的事件候选 → 已批准的日历事件
--
-- fingerprint 只用于去重（sha256(标题+起+止+组织者+地点)）；
-- 更新匹配走 (ics_uid, ics_recurrence_id, organizer)，因为同一 UID 的
-- 实例例外与母事件共享 UID，靠 fingerprint 会误合并。
--
-- 三方比对相关列：snapshot_hash(我方上次写入) / local_norm_hash(本地当前) /
-- remote_norm_hash(远端当前)，三者必须由同一个规范化函数产出才可比。
-- ───────────────────────────────────────────────────────────────
CREATE TABLE events (
    id                 INTEGER PRIMARY KEY,
    message_id         INTEGER REFERENCES messages(id),
    thread_id          INTEGER REFERENCES threads(id),

    title              TEXT,
    start_ts           TEXT,
    end_ts             TEXT,
    all_day            INTEGER NOT NULL DEFAULT 0,
    tz                 TEXT,
    location           TEXT,
    organizer          TEXT,

    source             TEXT NOT NULL,
    confidence         REAL,
    fingerprint        TEXT NOT NULL,
    evidence           TEXT,                     -- 抽取依据的原文片段

    status             TEXT NOT NULL DEFAULT 'pending',
    field_provenance   TEXT,                     -- JSON: {field: "ai" | "human"}
    needs_attention    INTEGER NOT NULL DEFAULT 0,

    -- Google 侧关联与所有权
    gcal_event_id      TEXT,
    gcal_etag          TEXT,
    remote_norm_hash   TEXT,
    local_norm_hash    TEXT,
    snapshot_hash      TEXT,
    managed_state      TEXT,                     -- owned | adopted | frozen
    last_checked_at    TEXT,

    -- ICS 身份（更新匹配键）
    ics_uid            TEXT,
    ics_sequence       INTEGER,
    ics_recurrence_id  TEXT,
    ics_rrule          TEXT,

    push_attempts      INTEGER NOT NULL DEFAULT 0,

    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,

    CHECK (all_day IN (0, 1)),
    CHECK (needs_attention IN (0, 1)),
    CHECK (source IN ('ics', 'rules', 'llm')),
    CHECK (status IN (
        'pending', 'approved', 'pushed', 'push_failed', 'uncertain',
        'externally_modified', 'conflict', 'missing',
        'rejected', 'ignored', 'cancelled', 'superseded'
    )),
    CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0))
);

-- 同一封邮件重跑抽取不得产生重复事件
CREATE UNIQUE INDEX ux_events_msg_fp  ON events(message_id, fingerprint);
CREATE INDEX        ix_events_status  ON events(status, needs_attention);
CREATE INDEX        ix_events_fp      ON events(fingerprint);
CREATE INDEX        ix_events_ics     ON events(ics_uid, ics_recurrence_id);
CREATE INDEX        ix_events_gcal    ON events(gcal_event_id);


-- ───────────────────────────────────────────────────────────────
-- scheduled_pushes：自动入历的延迟窗口（纯调度队列，不参与事件状态机）
-- ───────────────────────────────────────────────────────────────
CREATE TABLE scheduled_pushes (
    id             INTEGER PRIMARY KEY,
    event_id       INTEGER NOT NULL REFERENCES events(id),
    scheduled_for  TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'queued',
    run_id         TEXT,
    created_at     TEXT NOT NULL,
    dispatched_at  TEXT,
    error          TEXT,
    CHECK (state IN ('queued', 'dispatched', 'cancelled', 'failed'))
);

CREATE INDEX ix_sp_due   ON scheduled_pushes(state, scheduled_for);
CREATE INDEX ix_sp_event ON scheduled_pushes(event_id);


-- ───────────────────────────────────────────────────────────────
-- processed_mail：sync 层台账（只管「是否已抓取入库」，不管抽取）
-- 与 messages.extract_status 的职责严格分离，避免出现两套平行账本。
-- ───────────────────────────────────────────────────────────────
CREATE TABLE processed_mail (
    id            INTEGER PRIMARY KEY,
    account       TEXT NOT NULL DEFAULT '163',
    folder        TEXT NOT NULL,
    uid_validity  INTEGER NOT NULL,
    uid           INTEGER NOT NULL,
    status        TEXT NOT NULL,
    error         TEXT,
    processed_at  TEXT NOT NULL,
    CHECK (status IN ('synced', 'fetch_failed')),
    UNIQUE (account, folder, uid_validity, uid)
);

CREATE INDEX ix_pm_status ON processed_mail(status);


-- ───────────────────────────────────────────────────────────────
-- sync_state：每个 account+folder 的增量游标
-- UIDNEXT-1 是「历史最大分配值」，可能大于当前存在的最大 UID，
-- 故 highest_uid 用 max() 累积而非直接赋值。
-- ───────────────────────────────────────────────────────────────
CREATE TABLE sync_state (
    account            TEXT NOT NULL DEFAULT '163',
    folder             TEXT NOT NULL,
    uid_validity       INTEGER NOT NULL,
    highest_uid        INTEGER NOT NULL DEFAULT 0,
    syncs_since_full   INTEGER NOT NULL DEFAULT 0,
    last_sync_at       TEXT,
    last_full_sync_at  TEXT,
    PRIMARY KEY (account, folder)
);


-- ───────────────────────────────────────────────────────────────
-- senders：发件人统计（订阅噪音判定与「非联系人」判据）
-- ───────────────────────────────────────────────────────────────
CREATE TABLE senders (
    account          TEXT NOT NULL DEFAULT '163',
    addr             TEXT NOT NULL,
    name             TEXT,
    total            INTEGER NOT NULL DEFAULT 0,
    unread           INTEGER NOT NULL DEFAULT 0,
    replied_count    INTEGER NOT NULL DEFAULT 0,   -- =0 且从未往来 → 非联系人
    last_seen_at     TEXT,
    has_unsubscribe  INTEGER NOT NULL DEFAULT 0,
    noise_score      REAL,
    PRIMARY KEY (account, addr),
    CHECK (has_unsubscribe IN (0, 1))
);


-- ───────────────────────────────────────────────────────────────
-- runs：每次运行的审计记录
-- 注意 error 只写 message id 与哈希，绝不写邮件正文（规格 §12）。
-- ───────────────────────────────────────────────────────────────
CREATE TABLE runs (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    stats_json  TEXT,
    ok          INTEGER,
    error       TEXT,
    exit_code   INTEGER,
    CHECK (ok IS NULL OR ok IN (0, 1))
);

CREATE INDEX ix_runs_started ON runs(started_at);
CREATE INDEX ix_runs_runid   ON runs(run_id);


-- ───────────────────────────────────────────────────────────────
-- locks：单实例锁，防止两个计划任务并发跑同一命令
-- 以 TTL 抢占：expires_at 过期即可被新 owner 接管。
-- ───────────────────────────────────────────────────────────────
CREATE TABLE locks (
    name          TEXT PRIMARY KEY,
    owner_run_id  TEXT NOT NULL,
    acquired_at   TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);


-- ───────────────────────────────────────────────────────────────
-- unsubscribe_log：退订操作结果（v2；保证重复执行幂等）
-- ───────────────────────────────────────────────────────────────
CREATE TABLE unsubscribe_log (
    id            INTEGER PRIMARY KEY,
    sender        TEXT NOT NULL,
    method        TEXT NOT NULL,                  -- mailto | http | one-click
    target        TEXT,
    result        TEXT,
    attempted_at  TEXT NOT NULL
);

CREATE INDEX ix_unsub_sender ON unsubscribe_log(sender, method);


-- ───────────────────────────────────────────────────────────────
-- todos / todo_reminders：v2 功能，此处先建表以免后续迁移
-- 提醒事件生成闸门：仅 status='approved' 的 todo 才生成提醒，
-- 且「批准 todo」即视为批准其 T-1 与 DUE 两条提醒，不重复审批。
-- ───────────────────────────────────────────────────────────────
CREATE TABLE todos (
    id           INTEGER PRIMARY KEY,
    message_id   INTEGER REFERENCES messages(id),
    text         TEXT NOT NULL,
    due_ts       TEXT,
    priority     INTEGER,
    done         INTEGER NOT NULL DEFAULT 0,
    source       TEXT,
    confidence   REAL,
    evidence     TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK (done IN (0, 1)),
    CHECK (status IN ('pending', 'approved', 'rejected'))
);

CREATE TABLE todo_reminders (
    id                INTEGER PRIMARY KEY,
    todo_id           INTEGER NOT NULL REFERENCES todos(id),
    kind              TEXT NOT NULL,
    reminder_at       TEXT NOT NULL,
    calendar_event_id TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    CHECK (kind IN ('T-1', 'DUE'))
);

CREATE INDEX ix_tr_todo ON todo_reminders(todo_id, kind);


-- ───────────────────────────────────────────────────────────────
-- app_meta：少量全局状态（例如 OAuth 的 needs_reauth 标记）
-- ───────────────────────────────────────────────────────────────
CREATE TABLE app_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT NOT NULL
);
