# AGENTS.md

本地 CLI「邮件处理中心」（包名 `mailhub`）：拉取邮箱 → 筛选重要邮件 → 经日程等渠道提醒用户。

当前插件：QQ IMAP、秋招策略、Apple 日历、Apple 提醒事项。面向 macOS。人读说明见 `README.md`。

## Setup

```bash
uv sync --extra dev
cp .env.example .env        # 已有 .env 勿覆盖
```

依赖：`pyproject.toml` + `uv.lock`。pytest 在 optional `dev` extra。配置键见 `.env.example`。启用 LLM 需同时有 `LLM_API_BASE` + `LLM_API_KEY`。

## Commands

```bash
uv run mailhub list-apple
uv run mailhub list-reminders
uv run mailhub scan-apple --days 60
uv run mailhub run --dry-run
uv run mailhub run --dry-run --json
uv run mailhub run
uv run mailhub run --full
uv run mailhub sync --dry-run   # sync 为 run 的别名
uv run pytest tests/ -q
```

改解析 / 匹配 / 规则后：先跑相关单测，再 `run --dry-run` 核对；不要默认对真实日历执行正式 `run`。
## Architecture

主链：`runtime.engine.run_once`

1. **Ingest** — `IngestSource.fetch` → `IngestBatch`
2. **Resolve** — `MailResolver.resolve` → `ResolvedMail | IgnoredMail | ResolveFailure`
3. **Dispatch** — `DispatchPlanner.plan` → `ActionRequest` → `ActionHandler.handle` → `ActionReceipt`

| 路径 | 职责 |
|------|------|
| `mailhub/contracts/` | 跨阶段 DTO 与 Protocol（无 IO） |
| `mailhub/runtime/` | `run_once`、Settings、RunContext |
| `mailhub/store/` | checkpoint、processed、action 幂等、日历行 |
| `mailhub/logging/` | lifecycle / llm_io JSONL |
| `mailhub/plugins/sources/qq_imap.py` | QQ IMAP |
| `mailhub/plugins/policies/qiuzhao/` | 秋招粗过滤 + 解析 |
| `mailhub/plugins/dispatch/apple_calendar/` | Apple 日历 Planner/Handler |
| `mailhub/plugins/dispatch/apple_reminders/` | Apple 提醒事项 Planner/Handler |
| `mailhub/cli/` | argparse 与展示 |

运行态在 `data/`（gitignore）。评测语料在 `tests/fixtures/email_corpus/`。

## Domain invariants

- **全局**：dry-run 不写渠道、不推游标；Ingest 不做重要性过滤；contracts 不依赖 plugins
- **秋招 policy 局部**：标题格式、`schedule_invite` 不建日程、日历 create 需起止+地点、开放窗口走提醒事项
- 系统 Python 3.9 + LibreSSL：保持 `urllib3<2`（见 `pyproject.toml` 注释）

## Code conventions

- Python 3.9+；`from __future__ import annotations`；dataclass 建模
- 新增配置：同步改 `runtime.config.Settings`、`.env.example`、必要时 README
- QQ / 秋招 / Apple 名字只出现在 `mailhub/plugins/`（及对应测试）
- 中文用户可见文案保持简洁准确
- 改秋招规则：在 `tests/fixtures/email_corpus/` 补/改 `.eml`，并更新 `labels.json`

## Guardrails

- **勿提交 / 勿打印** `.env`、授权码、API key、真实邮件正文中的隐私
- **勿提交** `data/`（sqlite、日志）；语料用脱敏样例
- 正式 `run` 会改本机日历并推进游标；调试默认 `--dry-run`
- AppleScript / 日历或提醒事项权限失败时，引导查系统设置「自动化 / 日历 / 提醒事项」，勿伪造成功写入
- 不要把核心改成绑死单一日历后端；新渠道以 Planner + Handler 插件形式加入
