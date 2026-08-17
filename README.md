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
cd /path/to/mail-to-calendar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填入凭证
```

## 使用

```bash
# 列出本机 Apple 日历名
python3 -m mail_to_calendar list-apple

# 先干跑：只解析不写入
python3 -m mail_to_calendar sync --dry-run

# 正式同步（有游标后自动增量）
python3 -m mail_to_calendar sync

# 忽略游标，按 LOOKBACK_DAYS 重扫
python3 -m mail_to_calendar sync --full

# 同步后额外打印解析结果 JSON
python3 -m mail_to_calendar sync --dry-run --json
```

建议用 launchd / cron 每 15～30 分钟跑一次 `sync`。

## 行为说明

### 增量拉取

- 首次 / `--full`：按 `LOOKBACK_DAYS` + `MAIL_LIMIT` 扫窗口
- 之后：只拉 `UID > last_uid` 的新邮件（含非秋招信，用于推进游标）
- 游标存在 `data/synced.sqlite` 的 `sync_cursor`

### 解析与日程生命周期

1. **规则粗过滤**（`rules.coarse_filter`）：无招聘信号、或命中宣推 / 投递确认 / 福利等噪声信号时丢弃（不调 LLM）；主题命中宣讲会 / 直播预约的群发活动同样丢弃（`broadcast_signal`）；拒绝记录写入 `data/logs/coarse_filter.jsonl`
2. **精解析**：启用 `LLM_API_*`（需同时配置 `LLM_API_BASE` + `LLM_API_KEY`）时先走模型（I/O 写入 `data/logs/llm_parse.jsonl`）；模型明确拒绝（无关 / 选时间邀约）不兜底；仅在超时 / 坏 JSON / 字段残缺时回退启发式。未启用 LLM 时直接走启发式
3. 阶段：`schedule_invite`（选时间）→ 不建日程；`confirmed` → 建日程；开放窗口测评（无固定开考时刻）也不建日程
4. 动作：
   - `create`：新建（同公司同学段若已有不同时间，则更新旧日程）
   - `reschedule`：按公司名或邮件回复链匹配旧日程并更新
   - `cancel`：匹配旧日程并从 Apple 日历删除

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

推理模型（把思考过程写在 `<think>` 里或独立的 `reasoning_content` 字段）也可直接用：解析前会剥掉思考过程再取 JSON，`data/logs/llm_parse.jsonl` 里 `output_raw` 保留原始响应、`output_reasoning` 单独存思考过程。

### 匹配旧日程的方式

1. 优先：`In-Reply-To` / `References` 指向此前建日程的邮件
2. 其次：同一 `company` + `event_type` 的最新活跃日程

> 升级前用旧逻辑创建、且 Apple 未保存 uid 的日程，无法自动改期/删除，需手动清一次；之后新建的日程会保存 uid/event_id。

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
mail_to_calendar/
  mail_qq.py      # QQ IMAP（增量 UID）
  rules.py        # 规则粗过滤
  parser.py       # 启发式 / LLM 精解析、改期 / 取消
  apple.py        # macOS Calendar
  store.py        # 游标 + 去重 + 活跃日程
  config.py       # .env 配置
  models.py       # 数据结构
  llm_log.py      # LLM / 粗过滤日志
  cli.py          # 命令行
data/
  email_example/  # 评测用 .eml + labels.json
  synced.sqlite   # 运行后生成：游标与活跃日程
  logs/           # 运行后生成：coarse_filter / llm_parse
tests/
  eml_loader.py
  test_parser.py
  test_rules_corpus.py
```
