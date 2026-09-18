# 命令行与服务器使用指南

> 给「用命令行 / 部署到服务器跑定时任务」的用法。
> 图形界面见 [图形界面使用指南](guide-gui.md)；内部设计见 [技术说明](guide-technical.md)。

## 两种使用方式

* **便携版（推荐日常使用）**：打包成 exe，双击运行，目标机器不需要 Python。
  见 [便携版使用指南](portable-usage.md)。
  **服务器上同样可用**——`auto-mail.exe` 是命令行版，计划任务与 cron 用它拿退出码。
* **源码运行（开发/调试）**：见下方「快速开始」。

**所有写操作默认 dry-run**，必须显式 `--apply` 才真正执行。

## 快速开始（源码运行）

```bash
# 1. 创建虚拟环境并安装（Windows / Git Bash）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e .

# 2. 自检（无需任何凭据即可运行）
.venv/Scripts/automail.exe doctor

# 3. 配置 163 授权码后，先预览同步（不写库）
cp .env.example .env      # 填入 IMAP_USER / IMAP_AUTH_CODE
.venv/Scripts/automail.exe sync            # dry-run：只报告将要发生的变化
.venv/Scripts/automail.exe sync --apply    # 真正写入本地数据库

# 4. 离线评测抽取质量（不联网、不花钱）
.venv/Scripts/python.exe -m tests.evaluate --write
```

**所有写操作默认 dry-run**，必须显式 `--apply` 才真正执行。

`doctor` 退出码约定，可直接用于计划任务判断：

| 退出码 | 含义 |
|---|---|
| `0` | 全部就绪 |
| `1` | 部分项缺失（例如尚未配置密钥）——**不算失败** |
| `2` | 致命（配置非法、依赖缺失、数据库不可用） |

`run` 的整体退出码同样取**最严重**的阶段。两个关键判断：网络/风控失败记为 1 而非 2
（下次会重试，当致命会让任务反复告警）；检测到用户手改而冻结记为 1
（这是正确行为，但需要人处理，不能报成功而无提示）。

## 典型使用流程

```bash
automail sync --apply          # 1. 同步邮件（只读邮箱）
automail extract --apply       # 2. 抽取出事件候选（全部进待审）
automail events list           # 3. 查看待审事件（含来源邮件与依据）
automail events approve 12,15  # 4. 批准（支持 1,2 或 1-5）
automail events reject 13      #    或否决
automail events edit 14 --title "面试" --start 2026-09-23T06:00:00Z
automail push --approved --apply   # 5. 写入日历

automail threads --apply       # 可选：重建邮件线程
automail digest                # 生成每日摘要到 out/
automail audit                 # 抽取复盘：最近 24h 哪些可能没抽对（只读）
automail mark-read             # 可选：把处理完的邮件标为已读（默认 dry-run）
automail stats                 # 查看只读统计
```

**审批与推送是分开的两步**：人的判断（approve）不该被技术故障吞掉。
即使推送时断网，审批结果也已落库，下次 `push` 继续。

### 安全保证（都有测试覆盖）

| 保证 | 说明 |
|---|---|
| 只写已批准的事件 | `pending` 永不写入 |
| 创建前先反查 | 按 `auto_mail_key` 查一次，命中则**回填**而非新建——覆盖「创建成功但写库前崩溃」，避免重复事件 |
| 只动自己创建的事件 | 所有权标记不匹配 → 冻结，绝不触碰 |
| 检测用户手改则冻结 | 三方比对（快照/远端/本地规范化哈希），不覆盖用户的修改 |
| 删除默认归档 | 置为 `cancelled`（可恢复）；硬删除需显式 `--hard-delete` |
| 撤销真的生效 | `--cancel` 后事件回到 `pending`，不会被自动流程再次消费 |
| 默认 dry-run | 所有写操作需 `--apply` |
| 邮箱默认只读 | 同步全程 `EXAMINE` + `BODY.PEEK`；唯一的写操作（已读回写）默认关闭 |
| 只改 `\Seen` 一个标志 | 已读回写的后端接口不含删除/移动/改其它标志的能力 |
| UIDVALIDITY 不符则拒绝写 | UID 一变动就可能指向别的邮件，此时绝不回写 |

## 命令一览

```
automail doctor [--live] [--json]        自检；默认离线，--live 才联网（含真实 IMAP 检查）
automail runs [-n N]                     查看最近运行记录
automail sync [--apply] [--limit N]      从 163 增量同步邮件（只读邮箱）
automail extract [--apply] [--limit N]   抽取事件候选（ICS → 规则 → LLM）
automail events list [-s STATUS]         查看事件（-s pending/approved/pushed/frozen/failed/all）
automail events approve|reject|ignore 1,2 审批（支持 1,2 或 1-5 批量）
automail events edit <id> --title/--start 人工修正（记为 human，不被自动覆盖）
automail events adopt <id>               接管被外部修改的事件（解冻）
automail events retry <id>               重置推送失败的事件
automail push [--approved] [--apply]     写入日历（--due 处理到点、--cancel 撤销窗口内）
automail run [--apply] [--digest]        串起全流程（--no-sync/--no-extract/--no-push）
                                         （受暂停标记影响，加 --ignore-pause 可强制跑）
automail threads [--apply]               重建邮件线程（163 无服务端 THREAD）
automail digest [--open]                 生成每日摘要（Markdown，落 out/）
automail audit [--hours 24] [--show <id>] 抽取复盘：最近哪些邮件可能没抽对（只读）
automail mark-read [--apply] [--undo]    把处理完的邮件标为已读（默认 dry-run）
automail stats [--json]                  只读统计（含降级/跳过指标）
automail pause [--resume]                暂停/恢复计划任务的自动运行
automail setup [--quiet]                 便携版首次运行引导
automail backup [--prune-only]           备份数据库并清理超期备份
automail auth [--status|--revoke]        Google OAuth 授权（含状态查询、换账号）
```

评测入口（离线、零成本）：

```
python -m tests.evaluate                 打印抽取质量报告
python -m tests.evaluate --write         写入 docs/eval-report.md
```

### 抽取复盘（`automail audit`）

抽取质量靠**真实邮件**迭代，而不是靠想象。`audit` 每天跑一次，回看最近 24
小时收到的邮件，回答一个问题：**哪些可能没抽对？**

它只读数据库、只写本地报告（`out/audit-<日期>.md`），**不碰邮箱、不碰日历、
不改库**。默认也**不调用 LLM**——复盘是给人看的，不是再花一次 token 得到
同样的答案。

报告把邮件分成三类，重点在**静默失败**：

| 分类 | 含义 |
|---|---|
| 很可能漏抽 | 抽取本该有结果却没有。最危险的一类——流程照常报成功，看不出少了什么 |
| 值得留意 | 有可疑迹象，但可能是正常情况（例如通知类邮件确实没有时间） |
| 未判定 | 本该由 LLM 兜底，而本轮没调 LLM → **观测能力的缺口，不是邮件的问题** |

最后一类是被单独分出来的，不是漏报。实测 72 小时窗口里，最初 16 个可疑项有
15 个都是同一句「未配置 LLM」——那是**一个全局配置状态**，逐封报告会把真正
的信号淹掉，报告也就没人看了。现在它只在报告头部说明一次，并单独计数。

```
automail audit                    # 复盘最近 24 小时
automail audit --hours 72         # 回看 3 天
automail audit --with-llm         # 连同 LLM 一起判断（消耗额度，判定最准）
automail audit --show 92          # 打印单封邮件的完整细节（含正文片段）
```

> **复盘本身也修正过两次误报**，都写进了测试：LLM 的输出每次都不完全一样，
> 所以过时判定只比较确定性来源（规则/ICS）；离线重跑看不到「定时取代全天」
> 这类跨来源合并，因此那种情况下跳过判定。取舍一致：**宁可漏报，不可误报**。

### 同步是严格只读的

`sync` 使用 `EXAMINE`（只读选中）与 `BODY.PEEK[]`（不改变已读状态）取信，
**不会**标记已读、移动或删除任何邮件。测试里有专门用例断言这一点：
取正文后邮件的 flags 必须保持为空。

首次同步大邮箱建议分批，避免一次性拉取过多触发风控：

```bash
automail sync --apply --limit 200    # 每轮最多取 200 封，可反复执行
```

### 已读回写（唯一会改变邮箱状态的操作）

默认**关闭**。启用后会把「已处理完」的邮件标为已读，让「未读」继续表示
「需要你处理」——而不是被已处理的邮件占满。

它只改 `\Seen` 一个标志：不删邮件、不移动、不改别的标志。启用方式：

```bash
# .env
MARK_READ_POLICY=resolved    # off（默认）| resolved | processed
```

然后先看 dry-run，确认要标的是哪些，再加 `--apply`：

```bash
automail mark-read                      # 只报告，不发任何 STORE
automail mark-read --limit 3 --apply    # 先标 3 封试水
automail mark-read --undo --apply       # 标错了：恢复为未读
```

| 策略 | 含义 |
|---|---|
| `off` | 不开启（默认） |
| `resolved` | 只在**没有任何事件等你处理**时才标（推荐） |
| `processed` | 所有抽取完成的邮件都标 |

`resolved` 有两道保守判断，都是为了不把「其实要你做事」的邮件藏起来：

1. 事件仍处于 `pending` / `uncertain` / `push_failed` → 保持未读（那正等着你）。
2. 主题或发件人像在等你响应（邀请、确认、回复请求、截止日期…）→ 保持未读。
   这条是实测补的：GitHub 的仓库邀请不产生日历事件，按「无事件即已处理」
   会被标掉——而它显然需要你回应。

> ⚠️ **启用后计划任务会自动标记。** `automail run --apply` 每 30 分钟执行一次，
> 其中包含已读回写。也就是说改完 `.env` 就等于让它在后台自动整理邮箱，
> **不再需要每次手动确认**。想先人工复核就别用 `run`，只手动跑 `mark-read`。
>
> 写入前有一道硬闸门：库中 `UIDVALIDITY` 与服务端不一致时**拒绝回写**
> ——UID 一变就可能指向完全不同的邮件，那时继续写会标到别人的邮件上。
> `--undo` 只恢复本程序标记过的（记在 `marked_read_at`），
> 你自己在别的客户端读过的不会被碰。

## 需要准备的三样凭据

163 授权码与 Google 凭据用于真实接入；LLM 凭据可选（未配置时抽取降级为仅规则 + ICS）。

### 1. 163 邮箱授权码

第三方客户端**不能**用网页登录密码，必须用 16 位「客户端授权码」：

1. 登录 163 网页版 → **设置** → **POP3/SMTP/IMAP**
2. 开启 **IMAP/SMTP 服务**（需绑定手机的短信验证）
3. 点击 **新增授权密码** → 得到 16 位授权码

> 授权码**只显示一次**，请立即保存。每个客户端可单独生成一个。

填入 `.env`：

```ini
IMAP_USER=yourname@163.com
IMAP_AUTH_CODE=你生成的16位授权码
```

### 2. Google Calendar 凭据

1. 在 [Google Cloud Console](https://console.cloud.google.com/) 新建项目
2. **API 和服务** → **库** → 启用 **Google Calendar API**
3. **OAuth 同意屏幕**（现名 **Google Auth Platform**）：
   - **Branding**：填写应用名与用户支持邮箱
   - **Audience**：用户类型选 **外部（External）**，
     并在 **测试用户** 里 **+ Add users** 添加你实际用来授权的 Google 账号
4. **凭据** → **创建凭据** → **OAuth 客户端 ID** → 类型选 **桌面应用**
5. 下载 JSON，重命名为 `credentials.json` 放到项目根目录
6. 运行 `automail auth`（会打开浏览器授权，需要你点「允许」）

> ⚠️ **第 3 步的「测试用户」最容易漏，漏了必然失败。**
> `calendar.events` 属**敏感** scope，应用处于「测试」发布状态时只有
> 名单里的账号能授权——而**项目所有者不会自动进入该名单**，必须手动添加自己。
> 漏掉它的表现是授权页显示「**错误 403：access_denied**」，且因为 Google 不回跳，
> 程序侧只能看到「等待回调超时」。程序会在报错时把这套排查步骤打印出来。

> ⚠️ **浏览器同时登录多个 Google 账号时**，可能自动选了未列入名单的那个。
> 错误页最后一行「联系开发者 `<邮箱>`」写的就是它。用**无痕窗口**打开授权链接
> 可以避免这个问题。

> ⚠️ **关于「Google 尚未验证此应用」**：这是正常的，点「高级 → 继续前往」即可。
> 自建应用不会通过 Google 审核，单人自用也不需要审核。

> ⚠️ **关于 7 天失效**：OAuth 同意屏处于 `Testing` 状态时，敏感 scope 的
> refresh token **可能约 7 天失效**；发布到 `In production` 可能涉及验证流程。
> 本程序会**处理失效并提示重新授权**，不会因此静默失败，但你需要知道这个前提。

授权范围只有 `calendar.events`（查看与编辑日历事件），不含其他 Google 数据。
随时可用 `automail auth --status` 查看授权状态。

### 3. LLM API Key（境内服务优先）

抽取用 OpenAI 兼容接口。境内服务优先，**邮件内容不出境**：

```ini
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-chat
```

未配置时抽取降级为「仅规则」模式并如实报告，不会假装成功。

## 配置

复制 `.env.example` 为 `.env` 后按需修改。所有项都有默认值，
**只有空白的项才需要你填写**。

关键策略项：

| 配置 | 默认 | 说明 |
|---|---|---|
| `USER_TIMEZONE` | `Asia/Shanghai` | 时间解析基准时区 |
| `CONFIDENCE_AUTO_PUSH_THRESHOLD` | `0.85` | 严格大于才自动入历；实际只有 0.95 档可过 |
| `AUTO_PUSH_DELAY_MINUTES` | `5` | 自动入历的可撤销窗口 |
| `AUTO_PUSH_LIMIT_PER_RUN` | `10` | 每轮推送上限，超出顺延 |
| `ICS_AUTO_PUSH_NON_CONTACT` | `false` | 非联系人 ICS 默认进待审（防投毒） |
| `EXTRACT_MAX_ATTEMPTS` | `3` | 逐邮件 LLM 尝试上限（防毒邮件持续烧钱） |
| `NOT_FOUND_POLICY` | `pending` | 远端 404 时的去向：`pending` / `recreate` / `fail` |
| `AMBIGUOUS_DATE_POLICY` | `pending` | 同日多候选的取舍：`pending` / `earliest` |
| `MARK_READ_POLICY` | `off` | 已读回写策略（唯一的邮箱写操作） |
| `EXCERPT_MAX_CHARS` | `4000` | 本地保留的正文片段上限 |
| `EXTRACT_MAX_ATTEMPTS` / `PUSH_MAX_ATTEMPTS` | `3` / `5` | 超过后升级为「需人工关注」 |

配置**保存在 `.env`**；图形界面里的账号密码写入 DPAPI 加密的
`data/secrets.dat`，并同步维护 `.env` 的对应键。

## 定时运行（Windows 任务计划）

一条命令注册全部任务：

```powershell
# 在项目根目录执行（默认：run 每 30 分钟、digest 每天 08:00、
# audit 每天 08:30、backup 每周日 03:00、登录后补处理一次）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks.ps1

# 卸载
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks.ps1 -Remove
```

**关机期间收到的邮件怎么处理**：注册一个「登录后跑一次」的补处理钩子。
脚本会按权限自动选机制——管理员用计划任务（`/sc onlogon`，登录后 2 分钟），
普通用户用**当前用户启动文件夹**里的快捷方式（Windows 为该场景提供的标准
机制，无需提权，因为 `schtasks /sc onlogon` 对未提权账号会返回「拒绝访问」）。
两种方式效果相同，`-Remove` 都会清理干净。

也可手动注册单个任务（`scripts/run.ps1` 固定了解释器路径与工作目录，
避免任务计划因环境不同而失败）：

```powershell
schtasks /create /tn "auto-mail run" /sc minute /mo 30 /F ^
  /tr "powershell -NoProfile -ExecutionPolicy Bypass -File D:\Workspace\auto-mail\scripts\run.ps1 run --apply"
```

> **间隔不要低于 15 分钟**：163 不支持 IDLE 只能轮询，过密会触发风控
> 而收到「阻止了一次不安全的收信请求」告警邮件。`install-tasks.ps1` 会
> 拒绝低于 15 分钟的设置。

> **任务只在登录时运行**：注册时不存储密码（`schtasks` 显示「只使用交互方式」），
> 因此关机或未登录期间不会执行。这是刻意的——为个人工具存密码换取后台运行，
> 安全代价大于收益。漏掉的运行会在**下次登录时**由补处理钩子补上
> （见上），日常增量同步也不会丢邮件。

### 阶段之间互不阻塞

`run` 按 sync → extract → push → mark-read 执行，**任一阶段失败不阻止后续阶段**：

| 情况 | 行为 |
|---|---|
| 邮箱风控断开 | 同步失败，但仍抽取本地已有邮件；**下次运行会重新同步**（游标未推进） |
| LLM 未配置 | 抽取降级为仅规则，但仍推送已批准事件 |
| 日历 API 限流 | 推送失败，事件转为 `push_failed`。**不会自动重试**，需 `events retry` 恢复（见下方「已知偏差」） |
| 缺邮箱凭据 | 跳过同步，其余照常（本地重跑很常见） |
| 已读回写未开启 | 该阶段报告「未开启」并跳过，不影响其余阶段 |

`mark-read` 必须在 extract/push **之后**：判定「处理完」依赖抽取结果与事件状态，
跑在前面会用上一轮的旧状态做决定——而那是会改变邮箱状态的操作。

### 已知偏差：推送失败不会自动重试

`push_approved()` 只选取 `status = 'approved'` 的事件，因此一旦失败转入
`push_failed`，它**不再被任何一轮 `push` 考虑**——`push_attempts` 只被累加
用于判断是否升级为「需人工关注」，并不触发重试。

这与规格的表述不一致（`models.py` 的注释写的是「按退避重试」），也与
`pipeline.py` 的注释（「下次运行会重试」）不符。实测确认：

```
事件置为 push_failed(attempts=2) → 跑一轮 push_approved(apply=True)
  ⇒ considered = 0，状态仍为 push_failed
```

**恢复方式**：`automail events retry <id>` 会把它重置为 `approved` 并清零
`push_attempts`，下一轮 push 才会重新尝试。注意界面**没有**「重试」按钮。

这是需要修的实现问题，已如实记录而非在文档里美化。临时应对：日历 API
限流多为短暂故障，事后执行一次 `automail events retry` 即可。

### 备份

自动备份只在「检测到待执行迁移」时触发，正常使用中可能很久不备份，
因此单独提供了命令与周任务：

```bash
automail backup              # 备份并清理超期备份
automail backup --prune-only # 只清理
```

保留策略由 `DB_BACKUP_KEEP`（数量）与 `DB_BACKUP_MAX_AGE_DAYS`（年龄）控制。
备份**不含邮件正文**（库里只存脱敏片段），因此不扩大隐私面，
但也意味着它不能替代原始邮件。

## 相关文档

* [便携版使用指南](portable-usage.md) —— 打包、放置、开机自启
* [图形界面使用指南](guide-gui.md) —— 只用界面的用法
* [技术说明](guide-technical.md) —— 架构、数据流、状态机、并发安全
* 设计规格：[事件状态机](spec-event-state-machine.md)、[IMAP 同步](spec-imap-sync.md)、
  [MIME 清洗](spec-mime-cleaning.md)、[并发安全](spec-pipeline-concurrency.md)、
  [日历所有权](spec-gcal-ownership.md)、[已读回写](spec-mark-read.md)
* [返回 README](../README.md)
