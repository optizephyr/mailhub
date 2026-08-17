# AGENTS.md

本地 CLI：从 QQ 邮箱增量拉取秋招相关邮件 → 粗过滤 → LLM/启发式解析 → 写入/更新/删除 **Apple 日历**。

面向 macOS；日历通过 AppleScript 操作。人读说明见 `README.md`。

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest          # 测试用，未列入 requirements
cp .env.example .env        # 已有 .env 勿覆盖
```

配置键见 `.env.example`。启用 LLM 需同时有 `LLM_API_BASE` + `LLM_API_KEY`。

## Commands

```bash
python -m qiuzhao_mail2calendar list-apple
python -m qiuzhao_mail2calendar scan-apple --days 60
python -m qiuzhao_mail2calendar sync --dry-run
python -m qiuzhao_mail2calendar sync --dry-run --json
python -m qiuzhao_mail2calendar sync
python -m qiuzhao_mail2calendar sync --full
python -m pytest tests/ -q
```

改解析 / 匹配 / 规则后：先跑相关单测，再 `sync --dry-run` 核对；不要默认对真实日历执行正式 `sync`。

## Architecture

流水线（`cli.cmd_sync`）：

1. `mail_qq.fetch_mails` — IMAP UID 增量（游标在 `data/synced.sqlite`）
2. `rules.coarse_filter` — 无招聘信号 / 噪声 / 宣讲群发 → 丢弃，不调 LLM
3. `parser.parse_mail` — 有 LLM 先模型；模型明确拒绝不兜底；超时/坏 JSON/字段残缺才回退启发式
4. 匹配旧日程 — 本地库 → 本轮 session → Apple 日历兜底（`calendar_match` + `CALENDAR_SCAN_DAYS`）
5. 写入 — `apple.create/update/delete_*`；dry-run 只规划不写库、不写日历、不推进游标

| 模块 | 职责 |
|------|------|
| `mail_qq.py` | QQ IMAP、`MailItem` |
| `rules.py` | 粗过滤 |
| `parser.py` | 启发式 + LLM 精解析 |
| `calendar_match.py` | 邮件 ↔ 已有日程匹配 |
| `apple.py` | macOS Calendar 读写 |
| `store.py` | 游标、去重、活跃日程 |
| `lifecycle_log.py` | `mail_lifecycle.jsonl` + `llm_io.jsonl`（`trace_id` 关联；每文件最多 100 行） |
| `cli.py` | 编排与命令行 |
| `models.py` | `CandidateEvent` 等数据结构 |
| `config.py` | `.env` → `Settings` |

状态与日志在 `data/`（gitignore）。评测语料：`data/email_example/` + `labels.json`，由 `tests/test_rules_corpus.py` 消费。

## Domain invariants

改行为时保持这些约定（详见 README「行为说明」）：

- 标题固定：`[面试|笔试|测评|其他] 公司名`；日历描述留空（匹配标记可埋在实现约定处，勿塞全文）
- `schedule_invite`（选时间）与开放窗口测评 → **不建日程**
- `create` / `reschedule` 必须同时有开始、结束、地点；线上用会议链接当地点
- 同公司同学段：相同开始时间 → 跳过；不同时间 → 更新旧日程
- 粗过滤拒绝或未调模型时只有主日志；LLM I/O 旁路保留完整 `output_raw` / `output_parsed`，不单独落 thinking
- 系统 Python 3.9 + LibreSSL：保持 `urllib3<2`（见 `requirements.txt` 注释）

## Code conventions

- Python 3.9+；`from __future__ import annotations`；dataclass 建模
- 新增配置：同步改 `config.Settings`、`.env.example`、必要时 README
- 解析/匹配逻辑优先纯函数，便于单测；副作用集中在 `cli` / `apple` / `store`
- 中文用户可见文案（CLI 输出、`outcome.summary`）保持简洁准确
- 改规则或启发式时：在 `data/email_example/` 补/改 `.eml`，并更新 `labels.json`

## Guardrails

- **勿提交 / 勿打印** `.env`、授权码、API key、真实邮件正文中的隐私
- **勿提交** `data/`（sqlite、日志、真实 `.eml`）
- 正式 `sync` 会改本机日历并推进游标；调试默认 `--dry-run`
- AppleScript / 日历权限失败时，引导查系统设置「自动化 / 日历」，勿伪造成功写入
- 不要把此工具改成非 macOS 通用日历后端，除非用户明确要求
