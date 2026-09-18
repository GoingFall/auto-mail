# 规格一：事件状态与审批规则

> 对应实施规格 §4。本文是**唯一权威**；代码中的 `models.EventStatus` 与
> `migrations/001_initial.sql` 的 CHECK 约束必须与本文一致。

## 1. 为什么需要审批队列

「自由文本邮件 → 日历事件」的抽取准确率不可能 100%。误判的代价不对称：

* **漏掉**一个事件 → 少一条日历，用户自己还能补救；
* **多写/写错**一条 → 污染用户的真实日历，且用户可能很久之后才发现。

因此把「写入日历」这件事拆成两步：抽取产出**候选**，候选经**审批**才写入。
只有两个来源被信任到可以跳过人工审批，其余一律等待确认。

## 2. 状态全集

| 状态 | 含义 |
|---|---|
| `pending` | 候选，等待人工裁决。**默认落点**。 |
| `approved` | 已批准，等待推送。 |
| `pushed` | 已成功写入 Google 日历。 |
| `push_failed` | 推送失败，按退避重试。 |
| `uncertain` | 反查 Google 时请求本身出错，无法判定远端是否已有该事件。 |
| `externally_modified` | 远端内容与我方快照不一致且本地无改动 → 疑似用户手改。**冻结**。 |
| `conflict` | 远端与本地都相对快照有改动。**冻结**。 |
| `missing` | 远端已不存在该事件（404）。去向由 `NOT_FOUND_POLICY` 决定。 |
| `rejected` | 终态：人工否决。 |
| `ignored` | 终态：忽略；同 fingerprint 不再入队。 |
| `cancelled` | 归档式取消（远端标记为 cancelled，非硬删除）。 |
| `superseded` | 终态：被同 `ics_uid` 的更高 `SEQUENCE` 取代。 |

**终态**：`rejected`、`ignored`、`superseded`。
**冻结态**：`externally_modified`、`conflict` —— 禁止对其发起任何 update/delete。

## 3. 状态转移

```
pending ─approve→ approved ─(自动白名单)─→ [队列 queued] ─dispatch→ pushed
   │                  │                        │                      │
   │                  │                        └─cancel→ pending      ├─外部改动→ externally_modified ─adopt→ approved
   │                  ├─(人工 approve 立即 push)                     ├─双方都改→ conflict ─(adopt|reject)→
   │                  ├─list 报错 → uncertain ─复核→ approved         └─404 → NOT_FOUND_POLICY
   │                  └─push 失败 → push_failed ─超限→ needs_attention
   ├─reject→ rejected(终态)
   ├─ignore→ ignored(终态，同 fingerprint 不再入队)
   └─edit → approved（该字段 field_provenance 记为 human）
```

关键约定：

* **`events.status` 是唯一状态源。** `scheduled_pushes` 只是调度队列，其
  `state`（`queued`/`dispatched`/`cancelled`/`failed`）**不属于**本状态机。
* **`--cancel` 后回到 `pending`，不是 `approved`。** 撤销意味着用户收回了自动
  推送的授权，该事件必须重新显式批准。回到 `approved` 会被自动流程再次消费，
  等于撤销无效。
* **人工 approve 不走延迟窗口。** 审批是人的显式动作，`push --approved --apply`
  立即执行。延迟窗口只作用于自动白名单事件，用途是给自动行为一次复核机会。
* **`adopt` 的落点**：以当前远端内容为新基线，重算 `snapshot_hash`，把
  `field_provenance` 全部字段标为 `human`，`managed_state=adopted`，
  状态回到 `approved`（解冻，但需再次显式 `push` 才写）。
* **审计留痕不静默**：审批时被跳过的事件必须报告原因（「已是 approved」
  「当前为 rejected 不允许 approve」）。静默跳过会让用户以为操作生效了。

## 3.1 命令与实现对应

| 操作 | 命令 | 实现 |
|---|---|---|
| 列出待审 | `events list [-s STATUS] [--since N]` | `review.ReviewQueue.list_items` |
| 批准 | `events approve 1,2` 或 `1-5` | `ReviewQueue.approve` |
| 否决 / 忽略 | `events reject` / `events ignore` | `ReviewQueue.reject` / `ignore` |
| 修正 | `events edit <id> --title X --start Y` | `ReviewQueue.edit` |
| 接管 | `events adopt <id>` | `ReviewQueue.adopt` |
| 重置重试 | `events retry <id>` | `ReviewQueue.retry` |
| 推送 | `push --approved [--apply]` | `push.PushEngine.push_approved` |
| 处理到点 | `push --due [--apply]` | `PushEngine.dispatch_due` |
| 撤销窗口内 | `push --cancel <事件ID\|队列ID>` | `ReviewQueue.cancel_push` |
| 归档取消 | `push --archive <id> [--apply]` | `PushEngine.archive` |
| 硬删除 | `push --hard-delete <id> [--apply]` | `PushEngine.hard_delete` |

**默认全部 dry-run**（`--apply` 才真正写入），但 `--cancel` 例外：它是本地状态
回退、无外部副作用，因此立即生效。

## 4. 审批边界（谁能跳过 pending）

### 自动白名单（直接 approved，经延迟窗口）

1. `source=ics` 且 `METHOD:REQUEST` 且 `organizer` 非空 且起止可解析；或
2. `source=rules` 且 `confidence > 0.85`（**严格大于**）且非模糊时间。

### 强制 pending（无论来源）

* 全部 `source=llm`；
* 多来源冲突（同 fingerprint 给出不同时间或标题）；
* `confidence <= 0.85`；
* 模糊时间（「下周三左右」「大概月底」）；
* **无年份日期（硬门）**；
* **解析出的开始时间早于当前时刻**（见下方 §4.1）；
* **解析出的开始时间早于 `received_at`**；
* 非联系人发来的 ICS（除非 `ICS_AUTO_PUSH_NON_CONTACT=true`）；
* `METHOD:CANCEL` 命中冻结态；
* 含人工字段的 ICS 更新（见下方 §4.2）。

## 4.1 时间已过 → 待审（实测补上的判据）★

**这条是真实数据暴露的缺陷，不是理论风险。**

初版只检查「早于收信时间」，漏了「早于当前时刻」。对已有大量历史邮件的邮箱
做**首次同步**时，后果很严重：抽出的「事件」全是早已过去的日期
（两个多月前的域名到期日、上个月的预约时间等）。

实测数据（89 封真实邮件）：加入该判据前，**4 个被标为「可自动入历」的候选
全部是过去时间**，自动入历准确率为 **0%**。加入后同一批数据降到 **0 个**
（40 个历史时间被正确拦截）。

判据实现为朴素比较，且放在所有其它检查之前——**无论置信度多高、来源多可靠，
过去的时间都不该自动写进日历**。日历是用来规划未来的。

## 4.2 含人工字段的 ICS 更新

收到同 `ics_uid` 更高 `SEQUENCE` 的更新，而该事件含人工编辑字段时：
**冻结人工字段 + 应用 ICS 对其余字段的更新 + 整体置 `pending` 并展示 diff**。
既不静默覆盖用户编辑，也不丢失更新——三者不可兼得时这是唯一安全解。

## 5. 规则 confidence 的来源

规则是确定性匹配，因此 `confidence` 必须由模式本身决定，而不是凭感觉赋值。

```
confidence = w × 特异性系数 × 一致性系数
```

| 因子 | 取值 |
|---|---|
| `w`（模式基础权重） | 绝对日期+显式时刻 **0.95**；绝对日期无时刻 **0.85**；相对日期「下周三」**0.75** |
| 特异性系数 | 含年份 **1.0**；无年份 **0.9** |
| 一致性系数 | 单匹配 **1.0**；多候选匹配 **0.8**（同时触发冲突 → pending） |

由于门槛是**严格** `> 0.85`，实际只有 0.95 档（绝对日期 + 显式时刻）能自动入历。
这消除了「3月5日 年会」这类无时刻事件压线放行的风险。

## 6. 每轮推送上限

`AUTO_PUSH_LIMIT_PER_RUN=10`。**超出部分保持 `approved` 顺延到下一轮**，
不做降级也不丢弃；摘要中记「N 条待推（超本轮上限）」。

## 7. 已知限制

* `METHOD:CANCEL` 遇到冻结态时转为待人工处理，不静默删除。
* **ICS 更新投毒**：伪造「联系人 organizer + 真实 UID + 高 SEQUENCE」可诱导我方
  更新已有事件。缓解措施是摘要对「因 ICS 更新而修改了已有事件」单独留痕，
  使此类操作可被察觉；v1 不承诺抵御这类伪造。
* v1 **不展开 `RRULE`**：重复事件按单次写入，标题加「(重复)」，描述保留原
  RRULE。不承诺完整重复事件管理、RSVP 跟踪、与会者同步。
