# 规格二：IMAP 同步一致性与 UIDVALIDITY 处理

> 对应实施规格 §5。目标邮箱为网易 163/126（Coremail 服务端）。

## 1. 本邮箱的三个硬约束

### 1.1 授权码，不是登录密码

第三方客户端必须使用 16 位「客户端授权码」。获取方式：网页版 →
设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP → 新增授权密码（需短信验证，
**只显示一次**）。

### 1.2 必须发送 IMAP `ID` 命令

163 在认证后、`SELECT` 之前要求客户端发送 RFC 2971 `ID`，否则返回：

```
NO SELECT Unsafe Login. Please contact kefu@188.com for help
```

处理策略：认证后主动发送 `ID`；若仍遇到 `Unsafe Login`，**记录服务端原始响应**、
重发 `ID`、再重连——视为针对当前服务端行为的容错，**不**当作永久协议保证，
也**不**据此判定密码错误。

### 1.3 必须禁用 SASL-IR

163/126 使用的 Coremail 会**先声明 `SASL-IR` 能力，然后拒绝 inline 形式**。
因此 `IMAP_USE_SASL_IR=false` 必须硬编码进 163 代码路径，不依赖自动探测。

### 1.4 其它

* **无 `IDLE`**：只能轮询，间隔不低于 15 分钟（`POLL_INTERVAL_MINUTES`）。
* **无服务端 `THREAD`**：线程只能在客户端重建（见规格五相关章节）。
* 文件夹名为 mUTF-7 编码（`&XfJT0ZAB-` 即「已发送」）；`imapclient` 自动解码。
  优先用 `SPECIAL-USE` 标记发现 `\Sent`/`\Junk`/`\Trash`，而非硬编码中文名。
* 单连接、指数退避 1→2→4→…→30 分钟、socket 超时 60 秒。

## 1.5 真机实测发现的四条事实（2026-09-14，89 封邮件）

以下都是在真实账号上验证过的，与最初的规格假设有出入，**必须按实测实现**：

### ① 163 不返回 `UIDNEXT`（连显式请求都被丢弃）

```
C: STATUS INBOX (MESSAGES UIDNEXT UIDVALIDITY)
S: * STATUS "INBOX" (MESSAGES 89 UIDVALIDITY 1)     ← UIDNEXT 被静默丢弃
```

`EXAMINE` 的 `untagged_responses` 里也没有 `UIDNEXT`。后果：**预判门在 163 上
永不生效**，每次同步都必须 `SEARCH`，靠客户端过滤 `uid > highest_uid` 剔除
range 带回的最后一封。因此客户端过滤对 163 是**主路径**而非"兜底"。
代价可接受（多一次 SEARCH，但不多取正文）。

### ② RFC 3501 §6.4.8 的边界在真实服务器上成立

```
最大 UID = 1748072176
C: UID SEARCH 1748072177:*
S: * SEARCH 1748072176          ← 返回最后一封，不是空集
```

这直接验证了客户端过滤的必要性。

### ③ 会话随时可能失效，必须重连续传

实测到的失效原因：

| 现象 | 原因 |
|---|---|
| `Autologout; idle for too long` | 空闲超时（实测介于 2~4 分钟之间：空闲 120s 仍可用，240s 断连） |
| `[WinError 10054] 远程主机强迫关闭了一个现有的连接` | 同一账号多会话/密集访问导致的断连 |

**这是预期事件，不是故障。** 初版遇到断连就放弃，实测出现过「89 封里 69 封
失败」，而其中大部分是可恢复的。现在对每批取回都做「重连 + 重试」
（`IMAP_RECONNECT_ATTEMPTS=3`，线性退避），修复后同样条件下降为**零失败**。

注意：断连后**必须重建连接**——在原 socket 上重试只会继续拿到 `Autologout`。

### ④ `imap4.xlist()` 在 CPython 里不存在

```
AttributeError: Unknown IMAP4 command: 'xlist'
```

`imaplib` 没有注册 `XLIST`。幸运的是 163 声明了 `SPECIAL-USE`，**`LIST` 响应
本身就带用途标记**，直接用即可：

```
* LIST (\Sent)  "/" "&XfJT0ZAB-"
* LIST (\Junk)  "/" "&V4NXPpCuTvY-"
* LIST (\Trash) "/" "&XfJSIJZk-"
```

初版依赖 `XLIST` 且异常被吞掉，导致所有文件夹 `special_use` 为空、
`find_special_folder("\\Sent")` 永远返回 `None`。现在改为从 `LIST` 的 flags
里筛选用途标记（并排除 `\HasNoChildren` 这类结构性标记）。

### ⑤ 取信必须分批

一次性 `FETCH` 大量 UID 更容易触发断连。`IMAP_FETCH_BATCH_SIZE=10`，
逐批处理；某批失败不影响已成功批次。

## 2. 增量同步算法

游标存于 `sync_state(account, folder, uid_validity, highest_uid,
syncs_since_full, last_sync_at, last_full_sync_at)`。

```
1. SELECT folder → 读 UIDVALIDITY 与 UIDNEXT
2. UIDVALIDITY != 库中值:
      该 folder 现有记录标 stale=1（不删除），highest_uid=0，
      记 runs 一条 uidvalidity_changed
3. 预判门: if UIDNEXT - 1 <= highest_uid:
      无新邮件 → 只刷新 flags，跳过 SEARCH，结束
4. 否则: UID SEARCH UID <highest_uid + 1>:*
      结果按 UID 集合处理，容忍不连续（UID 空洞是正常的）
5. highest_uid = max(highest_uid, 本轮最大 UID)
6. 对每封新邮件:
      先按 body_sha256 或 normalized_message_id 匹配同 folder 的
      stale / folder_moved 记录 → 命中则 reactivate（见 §4）
      未命中才完整入库 + 抽取
      写 processed_mail 台账
7. 刷新已存在 UID 的 flags / seen
8. syncs_since_full += 1
9. 若 syncs_since_full >= COMPENSATE_SCANS(20)
   或距 last_full_sync_at > 24h → 补偿扫描（见 §5）
10. 原子领取抽取任务 + 僵尸回收（见 §6）
```

### 为什么必须有「预判门」（第 3 步）

RFC 3501 §6.4.8 原文：

> a UID range of 559:* always includes the UID of the last message in the
> mailbox, even if 559 is higher than any assigned UID value.

也就是说，当邮箱最大 UID 是 4999 时，`UID SEARCH UID 5000:*` **会返回 4999
那封**，而不是空集。若不先判断，同步追平后每轮都会重新拉取最后一封邮件。

**UIDNEXT 缺失时的兜底**：若 SELECT/STATUS 未返回 `UIDNEXT`，退回
`UID SEARCH UID <highest+1>:*` 并在客户端过滤 `uid > highest_uid`
（丢弃服务端返回的最后一封）。不让 UIDNEXT 成为硬前提。

### 关于 `highest_uid` 的累积方式

`UIDNEXT - 1` 是「历史上分配过的最大 UID」，**可能大于当前实际存在的最大 UID**
（最高 UID 的邮件被删除后，UIDNEXT 仍会前进）。因此用 `max()` 累积，
而不是直接赋值。

## 3. 三层账本的职责边界

这是刻意的分层，避免出现两套平行账本导致实现者各自理解出一个版本：

| 层 | 载体 | 职责 | 取值 |
|---|---|---|---|
| sync 层 | `processed_mail.status` | 这封邮件是否已抓取/清洗入库 | `synced` / `fetch_failed` |
| extract 层 | `messages.extract_status` | 这封邮件的抽取进度 | `pending` / `running` / `done` / `failed` |
| 抽取幂等 | `events` 表存在性 | 同 `(message_id, fingerprint)` 是否已有候选 | 唯一索引保证 |
| LLM 成本 | `messages.extract_attempts` | **仅**统计 LLM 调用次数 | 整数，上限 `EXTRACT_MAX_ATTEMPTS=3` |

`extract_attempts` 超限 → `extract_status='failed'`（终态），
`automail events retry-extract <msg_id>` 可手工重启。这一层专为防止
「一封每次都失败的毒邮件在每个 cron tick 上重复烧钱」。

## 3.1 实现中踩到的三个真实陷阱

以下都是实现阶段实际踩到、且有测试固化的坑。它们不写在规格里很容易重犯：

### 3.1.1 `fetch` 必须显式传 `BODY.PEEK[]`

`imapclient.fetch(..., ["BODY[]"])` **会**触发 `\Seen`，把用户未读的邮件标记为
已读——这是一个真实的、不可逆的副作用。必须显式传 `BODY.PEEK[]`。
测试通过假服务器模拟两种行为并断言 flags 未被改动。

### 3.1.2 imapclient 的能力集是 bytes

```python
caps = {c.upper() for c in client.capabilities()}      # 错：{b'ID'}，`"ID" in caps` 恒假
caps = {_to_text(c).upper() for c in client.capabilities()}  # 对
```

写错会导致 `ID` 命令被静默跳过，进而在 163 上触发 `Unsafe Login`，
且错误信息完全指向别处（看起来像凭据或风控问题，实际是我们少发了一个命令）。

### 3.1.3 `imapclient.imap_utf7.encode/decode` 是 bytes 接口

```python
encode(s)  # 入参必须是 str（传 bytes 会**原样返回**，等于没编码）；返回 bytes
decode(b)  # 入参必须是 bytes（传 str 会**原样返回**）；返回 str
```

写错的表现是文件夹名以 `b'&i6KWBZCuTvY-'` 这样的字面量出现在协议里，
服务端找不到邮箱。中文文件夹（如「订阅邮件」）必现。

## 3.2 移动检测必须无条件执行

初版把「检测邮件是否已移出本文件夹」挂在「本轮有增量」的条件下，这是错的：
**邮件被移走时通常没有新邮件到达**，预判门会命中，于是最常见的场景永远不会被
检测到。移动检测因此必须无条件运行（它只发 `UID ALL`，代价很小）。

## 3.3 分批同步（`--limit`）

首次同步大邮箱时，一次性拉几千封容易触发风控。`--limit N` 限制每轮取回的
邮件数，游标停在本批最后一封，剩余留待下轮，因此**既不会漏也不会重复**。
测试用 `limit=2` 分三轮同步 5 封邮件，断言最终 5 行且 `COUNT(DISTINCT uid)=5`。


## 4. UIDVALIDITY 变化后的 reactivate

UIDVALIDITY 变化意味着该文件夹的 UID 空间被重建，旧 UID 不再有效。
但**邮件内容可能完全相同**——直接当新邮件处理会导致正文重取、事件重复抽取。

处理方式：

1. 旧记录保留并标 `stale=1`（不删除，以保留事件关联）；
2. 全量重扫时，对新邮件先按 `body_sha256` 或 `normalized_message_id`
   在当前 folder 的 `stale=1` 记录中查找；
3. 命中 → **reactivate**：清 `stale`、更新 `uid`/`uid_validity`、
   重挂事件关联、**跳过抽取**；
4. 未命中 → 才走完整的入库 + 抽取流程。

同类机制用于 `folder_moved`：邮件从 INBOX 移走时标 `folder_moved=1` 并记
`moved_to_folder`，**不删、不重抽**；在目标文件夹以 `body_sha256` 命中该记录
做 reactivate，而非当新邮件走完整流程。

## 5. 补偿扫描

每 `COMPENSATE_SCANS=20` 次增量，或距上次全量超过 24 小时，执行一次：
重扫最近 `COMPENSATE_DAYS=14` 天的邮件。目的是修复：

* 增量期间漏取的邮件；
* flags / 已读状态漂移；
* 因网络中断而状态不一致的记录。

执行后重置 `syncs_since_full`，更新 `last_full_sync_at`。

## 6. 并发与崩溃安全

* **单实例锁**：`locks` 表（`name` + `owner_run_id` + TTL 抢占）。
  防止两个计划任务同时跑同一命令。
* **SQLite**：`busy_timeout=5000` + `journal_mode=WAL`，允许读写并发。
* **原子领取**：
  ```sql
  UPDATE messages SET extract_status='running'
   WHERE id=? AND extract_status='pending'
  ```
  只有真正抢到的进程能处理该邮件。
* **僵尸回收**：周期性把超时的 `running` 记录回退为 `pending`，
  使崩溃留下的中间态可自愈。

## 7. 与 OfflineIMAP 的差异（有意为之）

`offlineimap3` 遇到 UIDVALIDITY 变化**直接中止该文件夹**，理由是「比静默重映射
安全」。本项目采用「标 stale + 重扫 + reactivate」，原因是我们的事件关联以
`body_sha256` 为准而非 UID。**但变化必须显式记录（`runs.uidvalidity_changed`）
并暴露给用户**，绝不静默发生。
