# 规格四：退订与归档的逐项确认与失败恢复

> **v1 状态：本规格描述的写操作全部暂缓。**
> v1 的 `automail cleanup` **只输出建议清单，不发送任何邮件、不访问任何 URL**。
> 本文先把 v2 的安全边界写清楚，避免实现时凭直觉处理不可逆操作。

## 1. 为什么退订是危险操作

`List-Unsubscribe` 头是不可信输入。它可能指向：

* 恶意 URL（点击即确认真实邮箱活跃，招来更多垃圾邮件）；
* 钓鱼页面；
* 与发件人无关的第三方地址。

因此**绝不能**批量自动执行退订。

## 2. v1 行为（只读）

`automail cleanup` 按 `List-Unsubscribe` / `List-Id` / 域名聚类，叠加
「你从未回复过」判定为噪音，输出清单：

```
来源 / 数量 / 最近收到 / 退订方式（mailto:地址 或 URL，仅展示）
```

**不发送邮件、不发起任何 HTTP 请求。** `mailto:` 只显示目标地址，
HTTP 只显示链接文本。

## 3. v2 行为（逐项确认 + 失败恢复）

### 3.1 退订方式与对应处理

| 方式 | 处理 |
|---|---|
| `mailto:` | 逐条打印**收件人 / 主题 / 正文**，单条确认后才发送 |
| HTTP 链接 | **只展示链接，永不自动 GET** |
| RFC 8058 one-click | 单独实现（`List-Unsubscribe-Post: List-Unsubscribe=One-Click`），并显式提示这是一次 POST 请求 |

**支持逐个确认，而不是一次对全部来源执行。** 操作结果写入
`unsubscribe_log(sender, method, target, result, attempted_at)`，
对重复执行做幂等保护（同 sender + method 已成功则不重复执行）。

### 3.2 归档

```
1. COPY 到目标文件夹
2. UID SEARCH 校验目标确实存在
3. 才标记原邮件 \Deleted
4. EXPUNGE 需显式 --expunge
```

要点：

* **目标文件夹由配置指定**，不猜；
* `COPY` 成功但 `EXPUNGE` 失败 → 保留中间态与 UID 映射，**支持重试**；
* **部分成功必须可恢复**，不能留下「复制了一半、删了一半」的模糊状态；
* 记录归档前后的 UID 变化。

`offlineimap3` 与 `kenn-io/msgvault` 都采取「暂存删除、明确确认后才在上游移除」
的姿态，本规格与之一致。

## 4. 待办与提醒（v2）

* todo 抽取**仅来自 LLM**（`source=llm`）→ 因此**恒为 `pending`**；
* `due_ts` 走与事件相同的时间语义规则（规格见实施规格 §8）；
* **提醒事件生成闸门**：仅当 `todos.status='approved'` 才生成
  `todo_reminders` 对应的日历事件；**批准 todo 即视为批准其 T-1 与 DUE
  两条提醒**，不重复审批；
* 提醒写独立日历「Auto-Mail 提醒」，便于整体静音；
* v1/v2 均**不**由超期自动催办。

`todo_reminders` 独立成表的原因：一个 todo 要产生两条提醒（T-1 与到期日），
若只在 `todos` 上放一个 `reminder_event_id` 无法表达，且 due date 重解析后
无法定位与清理旧提醒。
