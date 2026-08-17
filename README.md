# QQ 邮箱秋招邀约 → Apple 日历

本地小工具：扫描 QQ 邮箱里的面试 / 笔试 / 测评邮件，解析后写入 **Apple 日历**。

支持：正式通知建日程、改期更新、取消删除；按 IMAP UID 增量拉取新邮件。

## 准备

### 1. QQ 邮箱

1. 打开 [QQ 邮箱网页版](https://mail.qq.com) → **设置** → **账户**
2. 开启 **IMAP/SMTP 服务**
3. 生成 **授权码**（16 位，不是 QQ 密码）

### 2. Apple 日历

1. 打开 macOS「日历」App
2. 看左侧日历名称，填到 `APPLE_CALENDAR_NAME`（常见：`日历` / `Home` / iCloud 下某个日历）
3. 首次写入时，系统可能弹窗请求「自动化 / 日历」权限，点允许
4. 若 `list-apple` 卡住，到 **系统设置 → 隐私与安全性 → 自动化 / 日历**，允许终端或 Python 访问「日历」

## 安装

```bash
cd /path/to/qiuzhao-mail2calendar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入凭证
```

## 使用

```bash
# 列出本机 Apple 日历名
python3 -m qiuzhao_mail2calendar list-apple

# 列出目标日历里已有的日程（核对匹配用）
python3 -m qiuzhao_mail2calendar scan-apple --days 60

# 先干跑：只读匹配，展示最终动作与日程，不写入
python3 -m qiuzhao_mail2calendar sync --dry-run

# 正式同步（有游标后自动增量）
python3 -m qiuzhao_mail2calendar sync

# 忽略游标，按 LOOKBACK_DAYS 重扫
python3 -m qiuzhao_mail2calendar sync --full

# 干跑并输出 JSON（含 apply / match_via / event）
python3 -m qiuzhao_mail2calendar sync --dry-run --json
```

建议用 launchd / cron 每 15～30 分钟跑一次 `sync`。

## 行为说明

### dry-run

- 解析后做**只读匹配**（本地库 + Apple 日历兜底），判定将新建 / 更新 / 取消 / 跳过 / 失败
- 终端打印最终动作与完整日程字段；不写库、不写日历、不推进游标
- 已处理过的邮件仍报「将跳过(已处理)」，但会打印解析出的日程便于核对
- `--json` 每项为 `{apply, match_via, event}`；生命周期日志 `outcome.status=dry_run`，`apply.result` 为 `would_*`，并带 `planned_event`

### 增量拉取

- 首次 / `--full`：按 `LOOKBACK_DAYS` + `MAIL_LIMIT` 扫窗口
- 之后：只拉 `UID > last_uid` 的新邮件（含非秋招信，用于推进游标）
- 游标存在 `data/synced.sqlite` 的 `sync_cursor`

### 解析与日程生命周期

1. **规则粗过滤**（`rules.coarse_filter`）：无招聘信号、或命中宣推 / 投递确认 / 福利等噪声信号时丢弃（不调 LLM）；主题命中宣讲会 / 直播预约的群发活动同样丢弃（`broadcast_signal`）
2. **精解析**：启用 `LLM_API_*`（需同时配置 `LLM_API_BASE` + `LLM_API_KEY`）时先走模型；模型明确拒绝（无关 / 选时间邀约）不兜底；仅在超时 / 坏 JSON / 字段残缺时回退启发式。未启用 LLM 时直接走启发式
3. 阶段：`schedule_invite`（选时间）→ 不建日程；`confirmed` → 建日程；开放窗口测评（无固定开考时刻）也不建日程
4. 动作：
   - `create`：新建（同公司同学段若已有不同时间，则更新旧日程）
   - `reschedule`：匹配旧日程并更新
   - `cancel`：匹配旧日程并从 Apple 日历删除
5. 新建 / 改期必须同时提取开始时间、结束时间和地点；线上日程以会议链接作为地点
6. 标题固定为 `[面试|笔试|测评|其他] 公司名`，日历描述留空

### 查日志

同步后日志在 `data/logs/`（JSONL，一行一封邮件 / 一次 LLM 调用）。每个文件最多保留最近 **100** 行，超出则删掉最旧记录：

| 文件 | 内容 |
|------|------|
| `mail_lifecycle.jsonl` | 主日志：一封邮件从粗过滤 → 解析 → 写入的全流程 |
| `llm_io.jsonl` | 旁路：完整 prompt / `output_raw` / `output_parsed`（不含 thinking） |

两份日志用同一字段 **`trace_id`** 对应：处理一封邮件时生成一个 id，主日志必有；调了 LLM 时旁路也会写同一 id。粗过滤直接拒绝或纯启发式、未调模型时，只有主日志、没有旁路行。

**怎么读主日志**：先看 `outcome.summary`（中文结论），再扫 `stages`（`coarse_filter` → `parse` → `apply`）。`outcome.status` 常见值：`applied` / `rejected_coarse` / `rejected_parse` / `skipped_duplicate` / `skipped_same` / `dry_run` / `failed`。

```bash
# 最近几封的结论
jq -r '.outcome.summary' data/logs/mail_lifecycle.jsonl | tail

# 按主题查全流程
jq 'select(.mail.subject | contains("快手"))' data/logs/mail_lifecycle.jsonl

# 只看写入成功 / 失败
jq 'select(.outcome.status == "applied")' data/logs/mail_lifecycle.jsonl
jq 'select(.outcome.status == "failed")' data/logs/mail_lifecycle.jsonl

# 粗过滤被丢掉的
jq 'select(.outcome.status == "rejected_coarse") | {subject: .mail.subject, summary: .outcome.summary}' \
  data/logs/mail_lifecycle.jsonl

# 先取主日志的 trace_id，再对照 LLM 旁路
TRACE=$(jq -r 'select(.mail.subject | contains("快手")) | .trace_id' \
  data/logs/mail_lifecycle.jsonl | head -1)
jq --arg t "$TRACE" 'select(.trace_id == $t)' data/logs/llm_io.jsonl
```

未装 `jq` 时可：`brew install jq`，或直接用编辑器打开 jsonl（每行是一个完整 JSON）。

### LLM 配置

统一使用 OpenAI Chat Completions 兼容接口，只需配置地址、密钥和模型：

```dotenv
# DeepSeek
LLM_API_BASE=https://api.deepseek.com
LLM_API_KEY=sk-your-key
LLM_MODEL=deepseek-v4-flash

# MiniMax 国际站则使用：
# LLM_API_BASE=https://api.minimax.io/v1
# LLM_API_KEY=your-key
# LLM_MODEL=MiniMax-M3
```

推理模型（把思考过程写在 `<think>` 里或独立的 `reasoning_content` 字段）也可直接用：解析前会剥掉思考过程再取 JSON。`data/logs/llm_io.jsonl` 保留完整 `output_raw` 与 `output_parsed`，不单独落 thinking。

### 匹配旧日程的方式

先查本地库 `data/synced.sqlite`：

1. 优先：`In-Reply-To` / `References` 指向此前建日程的邮件
2. 其次：本轮 sync 已规划/已写入的同学段日程（公司名模糊匹配，如 `拼多多` ≈ `拼多多集团-PDD`）
3. 再次：本地库中同学段 + 公司名模糊匹配的最新活跃日程

本地库没命中时，**回到 Apple 日历里找已有日程并接管**（`CALENDAR_SCAN_DAYS` 天窗口，默认 90，设 0 关闭）：

4. 兼容旧日程：描述里的来源邮件 id（`[qiuzhao-mail2calendar] mid=...`，旧前缀 `[mail-to-calendar]` 仍可读）落在本封邮件的回复链上
5. 标题形如 `[面试] 公司名` 且公司名对得上、学段不冲突的最近一场（也兼容旧英文标签）

同公司同学段若已存在**相同开始时间**的日程，后到的邮件会跳过（不重复建）；时间不同则更新旧日程。同一轮里两封重复邀请（不同 message-id）会因此合并成一条。

接管后会把该日程的 Apple `uid` 写回本地库，后续改期 / 取消直接更新同一条，不会再扫日历。只有标题带 `[标签]` 前缀的日程才参与第 5 步，避免误改手动创建的同名日程。

核对日历里读到了什么：

```bash
python3 -m qiuzhao_mail2calendar scan-apple --days 60
```

## 规则引擎评测

真实 `.eml` 样例与金标索引在 `data/email_example/`（`labels.json` 一份索引）。默认只测规则 / 启发式，不调用 LLM。

```bash
source .venv/bin/activate
pip install pytest   # 若尚未安装
python -m pytest tests/test_rules_corpus.py -q
# 或跑全部测试
python -m pytest tests/ -q
```

新增样例：把 `.eml` 放进 `data/email_example/`，并在 `labels.json` 的 `cases` 里补一条期望。

## 目录

```
qiuzhao_mail2calendar/
  mail_qq.py      # QQ IMAP（增量 UID）
  rules.py        # 规则粗过滤
  parser.py       # 启发式 / LLM 精解析、改期 / 取消
  apple.py        # macOS Calendar 读写
  calendar_match.py # 邮件 ↔ 日历已有日程的匹配
  store.py        # 游标 + 去重 + 活跃日程
  config.py       # .env 配置
  models.py       # 数据结构
  lifecycle_log.py # 邮件生命周期 + LLM I/O 旁路
  cli.py          # 命令行
data/
  email_example/  # 评测用 .eml + labels.json
  synced.sqlite   # 运行后生成：游标与活跃日程
  logs/           # 运行后生成：mail_lifecycle / llm_io
tests/
  eml_loader.py
  test_parser.py
  test_calendar_match.py
  test_sync_lifecycle.py
  test_rules_corpus.py
```
