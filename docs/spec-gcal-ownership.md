# 规格三：Google 事件所有权、更新与删除策略

> 对应实施规格 §8。核心问题：**如何在不覆盖用户手改内容的前提下，管理程序自己
> 创建的日历事件。**

## 1. 所有权模型

程序只管理**自己创建的**事件。识别方式是写入时打标：

```json
"extendedProperties": { "private": { "auto_mail_key": "<fingerprint>" } }
```

* 所有 `events.insert` 都带此标记；
* 所有 update/delete **只作用于带此标记的事件**；
* 绝不触碰任何无标记事件（用户手动创建或其它工具创建的）。

## 2. 幂等：create 之前先反查

**问题**：`events.insert` 成功但写库前进程崩溃 → `gcal_event_id` 仍为空 →
重跑时会重复创建日历事件。

**解法**：每次 create 前先反查：

```
events.list(calendarId, privateExtendedProperty="auto_mail_key=<fingerprint>")
```

* **命中** → 回填 `gcal_event_id` / `etag` / `snapshot_hash`，**跳过 create**；
* **未命中** → 正常 create；
* **请求本身报错** → 状态置 `uncertain`，下一轮复核后回到 `approved` 重试 create。

`privateExtendedProperty` 的格式是 `propertyName=value`，可重复传入（多个条件
之间是 **AND** 语义）。**注意**：它**不能与 `syncToken` 同用**，因此本步骤用
一次性 list，不做增量同步。

### 反例：gcalcli 的做法

`insanum/gcalcli` 的 `AddEvent` 无条件 `events.insert`，重跑必然产生重复事件。
本项目**不采用**这种做法——这是「反查回填」机制存在的原因。

## 3. 三方比对：如何判断事件被外部修改过

**前提事实：Google Calendar API v3 未定义 `If-Match` / 条件请求。**
经核对官方 discovery 文档与 `events.get`/`update`/`delete` 参考页，
均无 `If-Match` 或 412 契约。因此**不能**依赖原子条件请求，改用
「get → 比对 → update」的乐观流程。

### 三个哈希的位置

| 哈希 | 含义 |
|---|---|
| `snapshot_hash` | **我方上次写入**的事件内容 |
| `remote_norm_hash` | 当前从 Google 读回的内容 |
| `local_norm_hash` | 我方本地待写入的内容 |

**三者必须由同一个规范化函数产出**，否则不可比。这是整个机制的地基。

### 规范化哈希函数

```python
def norm_event_hash(payload) -> str:
    """远端/本地/快照共用的规范化哈希。"""
    keep = {summary, description, location, start, end, status,
            extendedProperties.private.auto_mail_key}
    drop = {id, etag, htmlLink, iCalUID, created, updated, sequence,
            hangoutLink, creator, organizer.email, reminders,
            conferenceData, ...}          # 服务器注入或易变字段
    t = {k: payload.get(k) for k in keep}
    t.start = normalize_dt(t.start)       # 统一为 UTC 瞬时或全天日期
    t.end   = normalize_dt(t.end)         # 消除时区表示差异
    t.description = normalize_ws(t.description)   # 折叠空白、统一行尾
    return sha256(canonical_json(t, sort_keys=True, separators=(',', ':')))
```

顺序是「**白名单 + 黑名单 + 时间归一 + 空白归一**」四步。

### 全天事件的时间语义（实测校准）

两个都写进了测试（`tests/test_gcal.py`）：

1. **`start.date` / `end.date` 是日历侧的「当地日期」，不是 UTC 日期。**
   我们库里的 `start_ts` 一律是 UTC 瞬时；当地 2026-10-01 零点存成
   `2026-09-30T16:00:00Z`。因此全天事件**不能**用 `ts[:10]` 取日期，
   否则「10-01 截止」会显示成 09-30——早一天。
2. **`end.date` 是开区间**：Google 只显示 `[start, end)`。
   实测 `start=end` 会被服务端原样接受并存回同一天，即**零长度事件**。
   要表示「10-01 这一天」必须写 `end=10-02`。

因此全天事件的日期换算必须 **start/end 联动**，不能各自独立地取日期。

**为什么必须这样**：`vdirsyncer` 的关键洞察是对**规范化后的形式**做哈希
（属性排序、剥离 VTIMEZONE、丢弃忽略属性），这样服务端的字段重排不会误判为
「用户编辑」。没有这一步，`remote_norm_hash` 每次都会「有差异」，机制失效。

### 实测踩到的两个坑（都已修复并有回归测试）

**① 空白折叠必须连换行一起折叠。**

初版只折叠空格与制表符（`[ \t]+`），保留了换行。但真实服务端会把描述里的
`\n` 折叠成空格——于是：

* `remote_norm_hash` 永远不等于 `snapshot_hash`
* **`benign_evolution` 分支永远无法命中**
* 每次服务端轻微改写都被误判为 `conflict`，使用者被迫做无谓裁决

实测中这一个字符层面的差异，让「用户手改」（应判 `externally_modified`）
被误报为「双方都改」（`conflict`）——虽然都是冻结态、不会覆盖用户数据，
但诊断信息是错的，用户会以为自己也需要为本地改动负责。

修复：比较时把所有空白序列（含换行）折叠成单个空格。
描述里的换行是**排版**而非**内容**；真实手改会改变文字，不会只把换行换成空格。

**② 我方重新构造的 payload 必须稳定。**

`local_hash` 与 `snapshot_hash` 的差异会被判为「本地也改了」。若我方每次
重新生成描述文案（例如 evidence 里追加了新来源），那个措辞变化就会让
**每一次更新都被误判为冲突**。

修复：描述优先沿用已保存的快照值，保证同一事件重复构造得到完全相同的内容。

## 3.1 快照 payload 也要保存

初版只存 `snapshot_hash`。问题：判定为 `externally_modified`/`conflict` 时，
审核界面需要展示「**我方原本是什么** → 远端现在是什么」，只有哈希做不到。

因此 `events.snapshot_payload` 保存上次写入的完整 payload（JSON）。
体积可控（一个事件几百字节），且**不含邮件正文**，不扩大隐私面。
差异展示直接用它（见 `diff_payloads` 与 `describe_verdict`）。

## 4. 判断链（四个分支，必须全部实现）

```
events.get:
  404                          → NOT_FOUND_POLICY（默认 pending）
  所有权标记不匹配              → NOT_OWNED（冻结，绝不触碰）
  etag 未变                     → NO_CHANGE，仅更新 last_checked_at
  etag 变:
      remote_norm_hash == snapshot_hash
          → 服务端序列化/无关字段变动 → BENIGN_EVOLUTION，可安全 update
      remote_norm_hash != snapshot_hash:
          local_norm_hash == snapshot_hash
              → 纯外部改动 → EXTERNALLY_MODIFIED（冻结）
          local_norm_hash != snapshot_hash
              → 双方都改 → CONFLICT（拒绝一切写入，转人工）
```

注意 `etag` 变化**不能**直接判定为用户编辑——服务端自身的更新也会改 etag。
必须继续比对内容哈希。

**所有权检查必须放在最前面**：远端事件若不是我们创建的，无论内容是否相同
都不得更新或删除。这是「只动自己创建的事件」的硬性保证。

## 5. 冻结态与解冻

`externally_modified` 与 `conflict` 都是**冻结态**：禁止对其发起任何 update/delete。

| 操作 | 语义 |
|---|---|
| `events adopt <id>` | 以**当前远端内容**为新基线：重算 `snapshot_hash = remote_norm_hash`，`field_provenance` 全部字段标 `human`，`managed_state=adopted`，状态回 `approved`（需再次显式 push 才写入） |
| `events reject <id>` | 放弃该候选，状态转 `rejected` |

`conflict` 必须先经 `adopt` 或 `reject` 才能离开冻结态。

**为什么 adopt 后回到 `approved` 而不是 `pushed`**：解冻只表示「可以继续接管
该事件了」，本身不该带来任何副作用。回到 `pushed` 会让人以为已经写入过。

## 6. 更新与删除

* **更新**：`events.get` → §3 比对 → `events.update`。
  用 `update`（全量）而非 `patch`，因为 Google 的 `events.patch` 语义有歧义，
  且我们本来就有完整内容。
* **404**：走 `NOT_FOUND_POLICY`（`pending` | `recreate` | `fail`，默认 `pending`）。
* **`edit` 已 pushed 的事件**：改**同一条** `gcal_event_id`，不新建；
  该字段的 `field_provenance` 记为 `human`。
* **删除**：默认 `archive` —— 把远端置为 `status=cancelled`（可恢复）；
  仅 `--hard-delete` 且 `auto_mail_key` 匹配时才真正 `events.delete`。
* **`METHOD:CANCEL` × 冻结态**：转为 `pending` 交人工裁决，**不静默删除**。

## 7. ICS 更新的匹配键

更新匹配用 `(ics_uid, ics_recurrence_id, organizer)`，**不是 fingerprint**。

原因：同一 `ics_uid` 的**实例例外**（`RECURRENCE-ID`）与母事件共享 `UID`，
若用 `ics_uid + organizer` 匹配会把实例例外误合并到母事件上。母事件的
`ics_recurrence_id` 为 NULL，实例例外有其具体值，因此该键能区分二者。

`fingerprint = sha256(归一化标题 + 开始 + 结束 + organizer/发件人 + location)`
**只用于去重**，不参与更新判断。

## 8. OAuth 凭据失效处理

* 捕获 `google.auth.exceptions.RefreshError`；
* `error == "invalid_grant"` → **不当作可重试错误**，持久化
  `app_meta['google.needs_reauth']=1`，在 doctor 与摘要中提示重新授权；
* `server_error` 等瞬时错误 → 按退避重试。

**关于 7 天失效**：Cloud 项目 OAuth 同意屏处于 `Testing` 状态时，敏感 scope 的
refresh token **可能约 7 天失效**；发布到 `In production` 可能涉及验证流程。
程序必须**处理失效并支持重新授权**，不能单靠发布状态解决。

## 9. 延迟窗口

自动白名单事件（ICS / 高置信规则）不立即写入，而是先落 `scheduled_pushes`
（`state=queued`，`scheduled_for = now + AUTO_PUSH_DELAY_MINUTES`）。

* 事件在窗口内保持 `approved`（`scheduled_pushes` 不参与事件状态机）；
* 到点且 `push` 运行时 dispatch：成功 → `events.status=pushed` +队列 `dispatched`；
* 失败 → `events.status=push_failed`；
* `--cancel` → 队列 `cancelled` + `events.status` 回退 **`pending`**；
* **实际窗口 = `AUTO_PUSH_DELAY_MINUTES` ~ 延迟 + 轮询间隔**（默认 5~20 分钟）。
  调度粒度决定 `--cancel` 的实际有效期，不要误以为恰好 5 分钟；
* **digest 必须列出窗口内即将自动入历的事件**，否则窗口没有可撤销对象、形同虚设。
