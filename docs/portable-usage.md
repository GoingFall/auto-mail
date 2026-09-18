# 便携版使用指南（Windows exe）

> 给「不想碰 Python、只想双击运行」的使用方式。
> 源码运行方式见 [README](../README.md)。

## 一、获取与放置

```bash
# 构建（在项目根目录，需要 .venv）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-exe.ps1
```

产物是 `dist\auto-mail\`（约 15 MB），**整个文件夹**复制到任意位置即可，
例如 `D:\Tools\auto-mail`。目标机器**不需要安装 Python**。

文件夹里有两个 exe：

| 文件 | 用途 |
|---|---|
| `auto-mail-gui.exe` | **图形界面**（双击这个） |
| `auto-mail.exe` | 命令行版，计划任务用它（需要拿退出码） |

两者共用同一份运行时依赖，因此**不是**双倍体积。

> 复制的是**文件夹**，不是单个 exe——`_internal\` 里是运行时依赖。

## 二、首次运行

双击 **`auto-mail-gui.exe`**。窗口会直接打开，没配置时自动停在「设置」页。

也可以在终端里跑 `auto-mail.exe`，它会：

1. 在 exe 同级目录创建 `data\`、`out\`、`logs\`
2. 从 `.env.example` 生成 `.env`
3. 打印引导，告诉你还缺什么

然后填入配置（图形界面里填，或编辑 `.env`）：

### 1. 163 邮箱授权码（`.env`）

网页版 163 → **设置** → **POP3/SMTP/IMAP** → 开启 IMAP →
**新增授权密码**（16 位，**只显示一次**）

```ini
IMAP_USER=yourname@163.com
IMAP_AUTH_CODE=十六位授权码
```

图形界面里填这个更省事：**设置 → 163 邮箱**，填完点「保存配置」即会加密存储。

### 2. Google 日历凭据

在 [Google Cloud Console](https://console.cloud.google.com/)：

1. 新建项目 → 启用 **Google Calendar API**
2. **OAuth 同意屏幕**（现名 Google Auth Platform）：
   - **Branding**：填应用名与用户支持邮箱
   - **Audience**：用户类型选「外部」，并在 **测试用户** 里
     **+ Add users** 添加你实际授权的 Google 账号
3. 创建凭据 → **OAuth 客户端 ID** → 类型 **桌面应用** → 下载 JSON
4. 把文件改名为 `credentials.json` 放在 exe 同级目录
5. 运行 `auto-mail.exe auth` 完成授权（会打开浏览器，需你点「允许」）

> ⚠️ **第 2 步的「测试用户」最容易漏。** `calendar.events` 属敏感 scope，
> 应用处于「测试」状态时只有名单里的账号能授权，而**项目所有者不会自动
> 进入名单**。漏了会看到「错误 403：access_denied」。

> ⚠️ 可能出现「Google 尚未验证此应用」——点**高级 → 继续前往**即可，
> 自建应用不会通过 Google 审核，单人自用也不需要。

### 3. LLM（可选，但推荐）

不配也能用，但抽取只有规则 + ICS，自由文本的时间识别率会明显偏低。
实测配置后**多发现 13 个规则完全漏掉的候选**：

```ini
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-flash
```

## 三、验证

```powershell
auto-mail.exe doctor           # 检查配置与连通性
```

三项凭据全绿时会输出「全部就绪 · 退出码 0」。

## 四、日常使用

**推荐：双击 `auto-mail-gui.exe`** —— 图形界面里点几下就行，不必记命令。

六个标签页：总览（待审数量、立即同步、暂停自动运行）、待审事件（批准/否决/
修正/接管）、邮件、日历、复盘、设置（改凭据、Google 授权、注册计划任务）。

界面上也会显示**为什么要你注意**：冲突、疑似重复、冻结态（被外部修改）
都标在行上。被冻结的事件要用「接管」而不是「批准」——状态机不允许直接批准，
界面会提示。

也可以继续用命令行（效果相同）：

```powershell
auto-mail.exe run --apply      # 同步 + 抽取 + 推送（主命令）
auto-mail.exe digest           # 生成摘要到 out\
auto-mail.exe events list      # 查看待审事件
auto-mail.exe events approve 12,15   # 批准（支持 1,2 或 1-5）
auto-mail.exe push --approved --apply  # 写入日历
auto-mail.exe pause            # 暂停计划任务的自动运行（--resume 恢复）
```

> **「批准」不等于「已写入日历」**。批准只记录你的决定，推送由 `push` 单独
> 执行——这样即使推送时断网，你的判断也不会丢。所以批准后事件会停在
> 「已批准，等待写入日历」，等下一次运行或你手动 `push`。

**所有写操作默认 dry-run**，加 `--apply` 才真正执行。

### 关于密码保存

图形界面里填的授权码与 API Key 会用 **Windows 加密**（DPAPI）存到
`data\secrets.dat`，密钥绑定你的 Windows 账号。首次保存时，程序会把 `.env`
里原有的明文迁进去并清空——顺序是**先加密写入、回读校验通过、才清明文**，
任何一步失败都保留明文（宁可继续用明文，也不会两边都没有）。

> 这个加密防的是**文件被拷走或被同步到云盘**。同一台机器上以你的账号运行的
> 任意程序都能解密——这是 DPAPI 的固有边界。要更强的隔离得靠独立的 Windows
> 账号或加密容器。

## 五、开机启动与定时运行

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks-exe.ps1
```

注册五个任务：

| 任务 | 触发 | 作用 |
|---|---|---|
| `auto-mail startup` | 登录时（延迟 2 分钟） | 补处理关机期间收到的邮件 |
| `auto-mail run` | 每 30 分钟 | 同步 + 抽取 + 推送 |
| `auto-mail digest` | 每天 08:00 | 生成摘要 |
| `auto-mail audit` | 每天 08:30 | 抽取复盘：最近 24h 哪些可能没抽对（只读） |
| `auto-mail backup` | 每周日 03:00 | 备份数据库 |

审核任务（`audit`）是**为了迭代抽取质量**：它把「哪些邮件可能理解错了」
写成 `out\audit-<日期>.md`，重点标出**静默漏抽**（预筛说该抽、结果什么都没有）。
不碰邮箱、不碰日历、不改数据库，默认也不调用 LLM。

卸载：加 `-Remove`。若 exe 不在 `dist\auto-mail`，用 `-ExeDir <目录>` 指定。

### 登录自动补处理（无需管理员权限）

关机/休眠期间收到的邮件需要一个「登录后立刻处理一次」的钩子。脚本会自动
选择可用的机制：

| 你的权限 | 用的机制 | 延迟 |
|---|---|---|
| 管理员 | 计划任务 `auto-mail startup`（`/sc onlogon`） | 登录后 2 分钟 |
| 普通用户 | **当前用户启动文件夹**里的快捷方式 | 无延迟 |

原因：`schtasks /sc onlogon` 对未提权账号返回「拒绝访问」（这是 Windows 的
限制，不是脚本的 bug）。**普通用户不需要为此提权**——启动文件夹是 Windows
为这个场景提供的标准机制，效果相同。

跑一次安装脚本即可，它会自己判断：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks-exe.ps1
```

普通用户会看到：

> schtasks cannot register an ONLOGON task without admin rights.
> Using the current user Startup folder instead (no admin needed):
>   startup shortcut: C:\Users\...\Startup\auto-mail (catch-up at logon).lnk

> 为什么不做一个真正的「开机启动」任务？`schtasks` 支持 `/sc onstart`，
> 但那需要存储账户密码或以 SYSTEM 运行——对个人工具来说安全代价大于收益。
> 「登录时运行」已经达到实际目的：开机后你一开始用，补处理就跑起来了。

启动项在 `-Remove` 时会一并清掉，不会残留。

### 任务只在登录时运行

注册时不存储密码，因此**关机或未登录期间不执行**（`schtasks` 显示
「只使用交互方式」）。这是刻意的。漏掉的运行会在下次运行时补上：
同步是增量的，抽取基于本地已有数据。

### 轮询间隔不要低于 15 分钟

163 不支持 IDLE 只能轮询，过密会触发风控并收到「阻止了一次不安全的
收信请求」告警邮件。脚本会拒绝低于 15 分钟的设置。

## 六、数据在哪里

全部在 exe 同级目录（打包后 `app_base_dir()` 解析为 exe 所在目录）：

```
auto-mail\
├─ auto-mail.exe
├─ .env                 ← 你的配置（含密钥，勿分享）
├─ credentials.json     ← Google OAuth 客户端
├─ token.json           ← 授权令牌（含 refresh token）
├─ auto-mail-run.cmd    ← 计划任务用的包装器
└─ data\
   ├─ automail.db       ← 邮件元数据、脱敏片段、事件、线程
   └─ backups\          ← 数据库备份（自动按数量/年龄清理）
```

**想放到别处**：设环境变量 `AUTOMAIL_HOME`，例如
`setx AUTOMAIL_HOME "E:\mail-data"`，数据就落到那里。
（建议放在**不受云盘同步**的目录——里面有你的邮件片段与密钥。）

## 七、常见问题

| 现象 | 原因与处理 |
|---|---|
| 双击后窗口一闪而过 | 程序出错会自动暂停让你看错误。若没停住，用终端运行看输出 |
| 授权页 403 access_denied | OAuth 测试用户名单没加自己（见上文） |
| 授权页「localhost 拒绝连接」 | 你在链接失效后才点完。重新运行 `auth`（默认不限时） |
| `doctor` 报 lacks credentials | 三个文件要放在 **exe 同级目录**，不是源码目录 |
| LLM 相关报错 | 确认 `LLM_BASE_URL` 不带 `/v1` 后缀（DeepSeek 用 `https://api.deepseek.com`） |
| 待审事件很多 | 正常——过期时间会被拦下待审。用 `events reject 1-5` 批量否决 |
| 「疑似重复」提示 | 只提示不合并：同一发件人的多笔独立交易标题也可能一样 |
| 如何让邮件自动变已读 | `.env` 设 `MARK_READ_POLICY=resolved`。默认关闭，开启前先跑 `auto-mail.exe mark-read` 看 dry-run |
| 界面里说「未配置」，但 `.env` 明明填了 | 配置与凭据必须放在 **exe 同级目录**。图形界面按 exe 位置找，不按当前目录 |
| 界面报「无法读取加密凭据」 | 换了 Windows 账号或换了机器（DPAPI 密钥绑定用户）。到设置页重新填授权码即可；`.env` 里手工填的值始终优先 |
| 想让自动任务先停几天 | 总览页勾「暂停自动运行」，或 `auto-mail.exe pause`。**只影响计划任务**，界面上的「立即同步」照常可用 |
| 点了「批准」但日历里没有 | 正常：批准只记录决定，写入由 `push` 执行。事件会显示「已批准，等待写入日历」，下次运行或手动 `push` 才进日历 |

## 八、当前能力边界（如实说明）

| 能力 | 状态 |
|---|---|
| 不做出危险动作（不误写/不覆盖用户改动/不重复创建） | ✅ 已在真实数据与真实 Google API 上验证 |
| 识别自由文本中的事件 | ✅ 有 LLM 时效果显著（实测多发现 13 个候选） |
| 识别**未来**事件 | ✅ 真实邀请函样本 3/3 命中；整体召回率未验证 |
| 跨邮件归并同一件事 | ⚠️ 只提示不合并（自动合并已验证为不安全） |
| 邮箱写操作 | ⚠️ 只有「标为已读」一项，默认关闭（见下） |

详见 [真实数据验证](eval-real-data.md) 与
[真实 Google 日历验证](eval-real-gcal.md)。

### 关于邮箱写操作

除「标为已读」外，程序**不会**删除、移动、标记或修改你邮箱里的任何东西。
同步全程只读（`EXAMINE` + `BODY.PEEK`）。

「标为已读」（`MARK_READ_POLICY`）默认**关闭**，开启后：

* 只改 `\Seen` 一个标志，不动别的
* 判断「处理完」时才标；有事件在等你、或主题像待办（邀请/确认/回复）→ 保持未读
* 可 `automail mark-read --undo --apply` 回退，且只恢复程序标记过的
* **启用后计划任务会自动执行**（每 30 分钟一次），不再逐次确认
