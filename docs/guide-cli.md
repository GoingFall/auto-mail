# 命令行与服务器使用指南

图形界面见 [GUI 指南](guide-gui.md)，内部设计见 [技术说明](guide-technical.md)。

**所有写操作默认 dry-run**，必须显式 `--apply` 才真正执行。

## 五步跑通

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e .
.venv/Scripts/automail.exe doctor          # 自检，无需凭据

cp .env.example .env                       # 填 IMAP_USER / IMAP_AUTH_CODE
.venv/Scripts/automail.exe sync            # 预览，不写库
.venv/Scripts/automail.exe sync --apply    # 真正入库
```

配齐凭据约 15 分钟（163 授权码最快，Google OAuth 最慢）。

服务器上也可用便携版的 `auto-mail.exe`（命令行版），计划任务与 cron 用它拿退出码。

## 日常五步

```bash
automail sync --apply              # 1. 同步（只读邮箱）
automail extract --apply           # 2. 抽取候选（全部进待审）
automail events list               # 3. 看待审
automail events approve 12,15      # 4. 批准（支持 1,2 或 1-5）
automail push --approved --apply   # 5. 写入日历
```

**审批与推送是两步**，故意如此：推送时断网也不丢人的判断，审批已落库，下次 push 继续。

其余常用：

```bash
automail run --apply               # 把上面 1-4 串起来（计划任务跑这个）
automail audit                     # 复盘：最近哪些可能没抽对（只读）
automail digest                    # 生成摘要到 out/
automail mark-read                 # 把处理完的邮件标为已读（默认 dry-run）
automail stats                     # 只读统计
```

## 退出码

计划任务靠它判断成败：

| 码 | 含义 |
|---|---|
| `0` | 全部就绪 |
| `1` | 部分完成（缺凭据、LLM 降级、推送失败、检测到手改）**不算失败** |
| `2` | 致命（配置非法、认证失败、依赖缺失、数据库不可用） |

`run` 取**最严重**的阶段。网络/风控失败与「冻结」都记为 1：

```bash
automail doctor --json    # 机器可读
```

## 全部命令

```
automail doctor [--live] [--json]         自检；默认离线，--live 才联网
automail runs [-n N]                      最近运行记录
automail sync [--apply] [--limit N]       增量同步（只读邮箱）
automail extract [--apply] [--limit N]    抽取候选（ICS → 规则 → LLM）
automail events list [-s STATUS]          查看事件（pending/approved/pushed/frozen/failed/all）
automail events approve|reject|ignore 1,2 审批（支持 1,2 或 1-5）
automail events edit <id> --title/--start 人工修正（记为 human，不被自动覆盖）
automail events adopt <id>                接管被外部修改的事件（解冻）
automail events retry <id>                重置推送失败的事件  ← 失败恢复靠它
automail push [--approved] [--apply]      写入日历（--due 到点、--cancel 撤窗口）
automail run [--apply] [--digest]         全流程（--no-sync/--no-extract/--no-push）
automail threads [--apply]                重建邮件线程
automail digest [--open]                  每日摘要（Markdown，落 out/）
automail audit [--hours 24] [--show <id>] 抽取复盘（只读）
automail mark-read [--apply] [--undo]     标为已读（默认 dry-run）
automail stats [--json]                   只读统计
automail pause [--resume]                 暂停/恢复计划任务
automail setup [--quiet]                  便携版首次运行引导
automail backup [--prune-only]            备份并清理超期备份
automail auth [--status|--revoke]         Google OAuth 授权
```

离线评测（不联网、不花钱）：

```
python -m tests.evaluate         打印报告
python -m tests.evaluate --write 写入 docs/eval-report.md
```

## 同步是只读的

`sync` 用 `EXAMINE` + `BODY.PEEK[]`，**不**标记已读、不移动、不删除。

首次同步大邮箱要分批，否则容易触发风控：

```bash
automail sync --apply --limit 200    # 每轮 200 封，可反复跑
```

## 已读回写（唯一的邮箱写操作）

默认**关闭**。只改 `\Seen` 一个标志，不删不移动。

```bash
# .env
MARK_READ_POLICY=resolved    # off（默认）| resolved | processed
```

| 策略 | 含义 |
|---|---|
| `off` | 不开启（默认） |
| `resolved` | 只在没有任何事件等你处理时才标（推荐） |
| `processed` | 所有抽取完成的邮件都标 |

先看 dry-run 再动手：

```bash
automail mark-read                    # 只报告
automail mark-read --limit 3 --apply  # 先标 3 封试水
automail mark-read --undo --apply     # 标错了：恢复
```

`resolved` 有两道保守判断：事件还在 `pending`/`uncertain`/`push_failed` 时保持未读；
主题像在等你响应（邀请、确认、截止日期）时保持未读。

> ⚠️ **启用后计划任务会自动标记。** `run --apply` 每 30 分钟跑一次，包含已读回写。
> 想人工复核就别用 `run`，只手动跑 `mark-read`。
> 库中 `UIDVALIDITY` 与服务端不一致时**拒绝回写**（UID 变了就可能标到别的邮件）。
> `--undo` 只恢复本程序标记过的，你在别的客户端读过的不会被碰。

## 三样凭据

163 授权码与 Google 凭据必需，LLM 可选（未配置则降级为「仅规则 + ICS」）。

### 163 授权码

1. 登录 163 网页版 → 设置 → **POP3/SMTP/IMAP**
2. 开启 **IMAP/SMTP 服务**（需短信验证）
3. **新增授权密码** → 得到 16 位授权码

```ini
IMAP_USER=yourname@163.com
IMAP_AUTH_CODE=你生成的16位授权码
```

授权码**只显示一次**，立即保存。它**不是**登录密码。

### Google Calendar

1. [Google Cloud Console](https://console.cloud.google.com/) 新建项目
2. **API 和服务 → 库** → 启用 **Google Calendar API**
3. **OAuth 同意屏幕**：填 Branding；Audience 选**外部**，
   **并在「测试用户」里添加你自己的 Google 账号** ← 最容易漏
4. **凭据 → 创建凭据 → OAuth 客户端 ID → 桌面应用**
5. 下载 JSON，重命名为 `credentials.json` 放项目根目录
6. `automail auth`（打开浏览器，点「允许」）

| 症状 | 原因 | 解决 |
|---|---|---|
| 403 access_denied | 你的账号不在「测试用户」名单 | 第 3 步添加自己（项目所有者不会自动进名单） |
| 等待回调超时 | 同上，Google 不回跳 | 同上 |
| 选错了账号 | 浏览器登录了多个 Google 账号 | 用无痕窗口打开授权链接 |
| 「尚未验证此应用」 | 正常 | 点「高级 → 继续前往」 |
| 约 7 天后失效 | 同意屏处于 `Testing`，敏感 scope 的 refresh token 会过期 | 重新 `automail auth`；程序会提示而非静默失败 |

授权范围只有 `calendar.events`。查状态：`automail auth --status`。

### LLM（可选）

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-chat
```

境内服务优先，**邮件内容不出境**。未配置时抽取降级并如实报告。

## 配置

复制 `.env.example` 为 `.env`。所有项有默认值，**只有空白的项才需要填**。

| 配置 | 默认 | 说明 |
|---|---|---|
| `USER_TIMEZONE` | `Asia/Shanghai` | 时间解析基准时区 |
| `CONFIDENCE_AUTO_PUSH_THRESHOLD` | `0.85` | 严格大于才自动入历；实际只有 0.95 档可过 |
| `AUTO_PUSH_DELAY_MINUTES` | `5` | 自动入历的可撤销窗口 |
| `AUTO_PUSH_LIMIT_PER_RUN` | `10` | 每轮推送上限，超出顺延 |
| `ICS_AUTO_PUSH_NON_CONTACT` | `false` | 非联系人 ICS 默认进待审（防投毒） |
| `NOT_FOUND_POLICY` | `pending` | 远端 404 去向：`pending`/`recreate`/`fail` |
| `AMBIGUOUS_DATE_POLICY` | `pending` | 同日多候选取舍：`pending`/`earliest` |
| `MARK_READ_POLICY` | `off` | 已读回写策略 |
| `EXTRACT_MAX_ATTEMPTS` / `PUSH_MAX_ATTEMPTS` | `3` / `5` | 超过后升级为「需人工关注」 |
| `EXCERPT_MAX_CHARS` | `4000` | 本地保留的正文片段上限 |

图形界面里填的账号密码会加密写入 `data/secrets.dat`，并同步维护 `.env` 对应键。

## 定时运行（Windows 任务计划）

```powershell
# 注册全部（run 每 30 分钟、digest 08:00、audit 08:30、backup 周日 03:00、登录后补跑）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks.ps1

# 卸载
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks.ps1 -Remove
```

装完每 30 分钟自动跑一轮，你只需定期看 `events list`。约 2 分钟。

| 注意 | 说明 |
|---|---|
| 间隔 ≥ 15 分钟 | 163 无 IDLE 只能轮询，过密触发风控（脚本会拒绝更短的设置） |
| 仅在登录时运行 | 不存密码是刻意的。关机期间漏掉的跑动由「登录后补一次」钩子补上 |
| 补处理钩子 | 管理员用计划任务，普通用户用启动文件夹快捷方式（无需提权） |

手动注册单个任务（`run.ps1` 固定了解释器路径，避免环境差异）：

```powershell
schtasks /create /tn "auto-mail run" /sc minute /mo 30 /F ^
  /tr "powershell -NoProfile -ExecutionPolicy Bypass -File D:\Workspace\auto-mail\scripts\run.ps1 run --apply"
```

### 阶段之间互不阻塞

`run` 按 sync → extract → push → mark-read 执行，**任一阶段失败不阻断后续**：

| 情况 | 行为 |
|---|---|
| 邮箱风控断开 | 同步失败，仍抽取本地邮件；下次重新同步 |
| LLM 未配置 | 抽取降级为仅规则，仍推送已批准事件 |
| 日历 API 限流 | 转 `push_failed`，**不自动重试** → 见下 |
| 缺邮箱凭据 | 跳过同步，其余照常 |
| 已读回写未开启 | 报告「未开启」并跳过 |

`mark-read` 必须在 extract/push 之后：它依赖抽取结果与事件状态，跑前面会改变邮箱状态。

### ⚠️ 推送失败不会自动重试（已知偏差）

`push_approved()` 只选 `status='approved'`。事件一旦转 `push_failed`，**再也不会被
任何一轮 push 捡起**（`push_attempts` 只用于升级「需人工关注」）。

```
push_failed(attempts=2) → 跑一轮 push_approved ⇒ considered = 0，状态不变
```

**恢复方式**：

```bash
automail events retry <id>     # 重置为 approved 并清零 attempts
```

界面**没有**「重试」按钮。这与 `models.py`、`pipeline.py` 的注释不符，属实现问题。

### 备份

自动备份只在检测到待执行迁移时触发，因此单独给了命令与周任务：

```bash
automail backup              # 备份 + 清理超期
automail backup --prune-only # 只清理
```

保留策略由 `DB_BACKUP_KEEP`（数量）与 `DB_BACKUP_MAX_AGE_DAYS`（年龄）控制。
备份**不含邮件正文**（库里只存脱敏片段）。

## 抽取复盘（`automail audit`）

回看最近 24 小时，回答：**哪些可能没抽对？** 只读库、只写 `out/audit-<日期>.md`，
默认不调 LLM。

| 分类 | 含义 |
|---|---|
| 很可能漏抽 | 本该有结果却没有。最危险——流程照常报成功 |
| 值得留意 | 有可疑迹象，但可能是正常情况 |
| 未判定 | 本该 LLM 兜底但本轮没调 → 观测能力缺口，不是邮件问题 |

```bash
automail audit                # 最近 24 小时
automail audit --hours 72     # 回看 3 天
automail audit --with-llm     # 连同 LLM 判断（消耗额度，最准）
automail audit --show 92      # 单封邮件完整细节
```

取舍是**宁可漏报，不可误报**：过时判定只比较确定性来源（规则/ICS），
因为 LLM 每次输出不完全一样。

## 安全保证（都有测试覆盖）

| 保证 | 说明 |
|---|---|
| 只写已批准的事件 | `pending` 永不写入 |
| 创建前先反查 | 按 `auto_mail_key` 查，命中则**回填**——覆盖「创建成功但写库前崩溃」 |
| 只动自己创建的事件 | 所有权标记不匹配 → 冻结 |
| 检测用户手改则冻结 | 三方比对，不覆盖用户的修改 |
| 删除默认归档 | 置 `cancelled`（可恢复）；硬删除需 `--hard-delete` |
| 撤销真的生效 | `--cancel` 后回 `pending`，不会被自动流程再消费 |
| 默认 dry-run | 所有写操作需 `--apply` |
| 邮箱默认只读 | `EXAMINE` + `BODY.PEEK` |
| 只改 `\Seen` 一个标志 | 后端接口不含删除/移动/改其它标志 |
| UIDVALIDITY 不符则拒绝写 | UID 一变就可能指向别的邮件 |

## 相关文档

* [GUI 指南](guide-gui.md) —— 只用界面的用法
* [便携版指南](portable-usage.md) —— 打包、放置、开机自启
* [技术说明](guide-technical.md) —— 架构、数据流、状态机、并发安全
* [返回 README](../README.md)
