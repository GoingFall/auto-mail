# auto-mail

163 邮箱助手：从邮件正文抽取事件时间，写进 Google 日历。目标是不用每天翻邮件。

**误写日历的代价高于漏识别**，所以拆成三步：抽取候选 → 人工审批 → 受控写入。
LLM 结果一律待审，只有 ICS 与高置信规则能自动入历。

## 先做这件事

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e .
.venv/Scripts/automail.exe doctor          # 自检，不需要任何凭据
```

然后按你的用法选一条：

| 你是 | 打开 | 规模 |
|---|---|---|
| 想双击用 | [GUI 指南](docs/guide-gui.md) | 配置约 15 分钟，之后每次点两下 |
| 用命令行 / 上服务器 | [CLI 指南](docs/guide-cli.md) | 五步跑通，含定时任务 |
| 想读代码 | [技术说明](docs/guide-technical.md) | 架构、数据流、状态机、并发 |

## 它靠谱吗

| 指标 | 合成语料 17 封 | 真实邮件 89 封 |
|---|---|---|
| 自动入历准确率（最关键） | 100% | **100%**（0 个错误写入） |
| 召回率 | 100% | 未来事件样本 3/3 命中；整体**未验证** |
| 预筛过滤率 | — | 52% |

首批 89 封真实邮件恰好全已过期，所以只证明了「不会做危险动作」。后来一封真实的
未来事件邀请函补上了这个缺口：三个事件全部命中，同时修掉 8 个缺陷。详见
[真实数据验证](docs/eval-real-data.md)。

## 它会碰什么

写操作**默认全部 dry-run**，要 `--apply` 才执行。

| 会 | 不会 |
|---|---|
| 读邮箱（`EXAMINE` + `BODY.PEEK`，不标已读） | 标已读、移动、删除邮件（除非显式开启已读回写） |
| 写自己创建的日历事件（带 `auto_mail_key` 标记） | 碰别人的事件、覆盖你手改过的内容 |
| 本地存脱敏正文片段 + 全文哈希 | 存完整邮件正文、把邮件内容发出境 |

删除默认是**归档**（`cancelled`，可恢复），硬删除要显式 `--hard-delete`。

凭据用 Windows DPAPI 加密存 `data/secrets.dat`。`.env`、`credentials.json`、
`token.json`、`data/`、`out/`、`logs/` 都在 `.gitignore` 里。

## 已知问题

**163 服务端侧**：无 IDLE（只能轮询，间隔 ≥15 分钟）、无服务端 THREAD、
需发 IMAP `ID`、风控会静默阻断。

**本项目侧**（待修，实测确认）：

| 问题 | 应对 |
|---|---|
| `push_failed` 不会自动重试 | `automail events retry <id>` |
| 界面不展示跳过原因 | 改用命令行，它会打印原因 |

详见[技术说明 § 已知实现偏差](docs/guide-technical.md)。

## 许可

MIT
