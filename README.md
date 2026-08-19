# 邮件处理中心（mailhub）

本地 CLI：拉取邮箱 → 筛选重要邮件 → 通过日程等方式提醒用户。

当前内置插件：QQ IMAP 取信、秋招重要性策略、Apple 日历 / 提醒事项分发。面向 macOS；日历与提醒事项经 AppleScript 操作。

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

### 3. Apple 提醒事项

开放窗口类任务（测评、任选时段笔试）写入「提醒事项」，可勾选完成。

1. 打开 macOS「提醒事项」App，看左侧列表名，填到 `APPLE_REMINDERS_LIST`（常见：`提醒事项`）
2. 首次写入时系统可能弹窗请求「自动化 / 提醒事项」权限
3. `uv run mailhub list-reminders` 可核对列表名

## 安装

```bash
cd /path/to/qiuzhao-mail2calendar
uv sync --extra dev
cp .env.example .env
# 编辑 .env 填入凭证
```

依赖由 `pyproject.toml` + `uv.lock` 管理；`uv sync` 会创建 `.venv` 并安装本包。

## 使用

```bash
# 列出本机 Apple 日历名
uv run mailhub list-apple

# 列出本机提醒事项列表名
uv run mailhub list-reminders

# 列出目标日历里已有的日程（核对匹配用）
uv run mailhub scan-apple --days 60

# 先干跑：只读匹配，展示最终动作与日程，不写入
uv run mailhub sync --dry-run

# 正式运行（有游标后自动增量）
uv run mailhub sync

# 忽略游标，按 LOOKBACK_DAYS 重扫
uv run mailhub sync --full

# 干跑并输出 JSON
uv run mailhub sync --dry-run --json

# 等价：uv run python -m mailhub ...
```

建议用 launchd / cron 每 15～30 分钟跑一次 `uv run mailhub sync`。
## 流水线

```text
Ingest（取信）→ Resolve（研判）→ Dispatch（分发）
```

- **Ingest**：`plugins/sources/qq_imap.py`，返回 `IngestBatch`（messages + next_checkpoint）
- **Resolve**：`plugins/policies/qiuzhao`，产出与渠道无关的 `ResolvedMail` / `IgnoredMail`
- **Dispatch**：Planner 生成 `ActionRequest`，Handler 执行（Apple 日历 + 提醒事项）

## 行为说明

### dry-run

- 解析后做只读匹配，判定将新建 / 更新 / 取消 / 跳过 / 失败
- 不写库、不写日历、不推进游标
- `--json` 每项为 `{apply, match_via, event}`

### 增量拉取

- 首次 / `--full`：按 `LOOKBACK_DAYS` + `MAIL_LIMIT` 扫窗口
- 之后：按 source checkpoint（QQ 为 IMAP UID）增量
- 游标在 `data/synced.sqlite`

### 秋招策略（插件约定）

1. 粗过滤：无招聘信号 / 噪声 / 宣讲群发 → 忽略
2. 精解析：启用 LLM 时先模型；模型明确拒绝不兜底；超时/坏 JSON 才回退启发式
3. `schedule_invite` 不建日程、不建提醒
4. 固定场次（已确认时刻 + 地点）→ 日历；开放窗口（测评 / 任选时段）→ 提醒事项
5. 日历新建/改期需开始、结束、地点；线上用会议链接当地点
6. 标题：`[面试|笔试|测评|其他] 公司名`

### 查日志

同步后日志在 `data/logs/`（JSONL，每文件最多 100 行）：

| 文件 | 内容 |
|------|------|
| `mail_lifecycle.jsonl` | 主日志：粗过滤 → 解析 → 分发 |
| `llm_io.jsonl` | LLM prompt / output（`trace_id` 关联） |

```bash
jq -r '.outcome.summary' data/logs/mail_lifecycle.jsonl | tail
```

## 目录结构

```text
mailhub/
  contracts/     # 阶段协议
  ingest/ resolve/ dispatch/
  runtime/       # run_once、配置
  store/ logging/
  plugins/
    sources/qq_imap.py
    policies/qiuzhao/
    dispatch/apple_calendar/
    dispatch/apple_reminders/
  cli/
```

## LLM 配置

见 `.env.example`。需同时配置 `LLM_API_BASE` + `LLM_API_KEY`。
