# 邮件处理中心（mailhub）

CLI：拉取邮箱 → 筛选重要邮件 → 通过日程等方式提醒用户。

当前内置插件：QQ IMAP 取信、秋招重要性策略、CalDAV 日历 / 提醒事项及 Bark 分发。支持在 Linux、容器等无头环境定时运行。

## 准备

### 1. QQ 邮箱

1. 打开 [QQ 邮箱网页版](https://mail.qq.com) → **设置** → **账户**
2. 开启 **IMAP/SMTP 服务**
3. 生成 **授权码**（16 位，不是 QQ 密码）

### 2. CalDAV

准备一个同时支持 `VEVENT` 和 `VTODO` 的第三方 CalDAV 账户，例如 Radicale 或 Nextcloud。mailhub 不直连 iCloud，也不再通过本机 App / AppleScript 写入。

1. 在账户中建一本日历和一个任务列表。
2. 配置 `caldav_url`、`caldav_username`、`caldav_password`。
3. 分别把显示名称精确填入 `calendar_name` 与 `reminders_list`。
4. 名称留空表示该渠道未启用；对应邮件会被消耗，不改道、以后也不补送。
5. 在需要查看的设备上添加同一个 CalDAV 账户。

`uv run mailhub list-calendars` 与 `uv run mailhub list-reminders` 可核对 collection 名称。固定场次写入日历；开放窗口类任务写入提醒事项。

### 3. Bark

Bark 是可选的即时推送渠道，默认未启用。当前只有 `schedule_invite` 进入 Bark；已确认场次、改期、取消和开放窗口不会改道到 Bark。

- 仅支持自建服务器和一个设备密钥，不使用官方默认地址
- 在 `config.yaml` 设置 `bark_enabled: true` 时，必须同时填写 `bark_server_url` 与 `bark_key`
- 启用后缺少任一项，`sync` 和 `sync --dry-run` 都会在拉信前失败
- dry-run 只校验配置，不向 Bark 服务器发请求

## 安装

```bash
cd /path/to/qiuzhao-mail2calendar
uv sync --extra dev
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入凭证
```

依赖由 `pyproject.toml` + `uv.lock` 管理；`uv sync` 会创建 `.venv` 并安装本包。

### 从 AppleScript 版本迁移

- 旧的 `apple_calendar_name` / `apple_reminders_list` 配置不再使用，改成 CalDAV 配置。
- 本机 App 返回的旧资源 ID 不能用于第三方 CalDAV。服务器部署不要复制旧 `data/`；已有日程和提醒不会自动迁移或删除。
- 第一次正式运行前，用专门的测试日历和任务列表执行 `sync --dry-run`，再核对创建、改期和取消。
- 不要让旧 Mac 实例和服务器实例同时处理同一个 `source_id`。

## 使用

```bash
# 列出 CalDAV 日历名
uv run mailhub list-calendars

# 列出 CalDAV 提醒事项列表名
uv run mailhub list-reminders

# 列出目标日历里已有的日程（核对匹配用）
uv run mailhub scan-calendar --days 60

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

建议用 cron / systemd timer 每 15～30 分钟跑一次 `uv run mailhub sync`。同一邮箱实例任意时刻只运行一个写入方，并让它独占 `data/` 里的游标与幂等状态。
## 流水线

```text
Ingest（取信）→ Resolve（研判）→ Dispatch（分发）
```

- **Ingest**：`plugins/sources/qq_imap.py`，返回 `IngestBatch`（messages + next_checkpoint）
- **Resolve**：`plugins/policies/qiuzhao`，产出与渠道无关的 `ResolvedMail` / `IgnoredMail`
- **Dispatch**：Planner 生成 `ActionRequest`，Handler 执行（日历 + 提醒事项 + Bark）

## 行为说明

### dry-run

- 解析后做只读匹配，判定将新建 / 更新 / 取消 / 跳过 / 失败
- 不写库、不写日历、不推进游标
- 校验已启用渠道的配置；可只读发现 / 扫描 CalDAV，但不写 CalDAV，也不请求 Bark
- `--json` 每项为 `{apply, match_via, event}`

### 增量拉取

- 首次 / `--full`：按 `LOOKBACK_DAYS` + `MAIL_LIMIT` 扫窗口
- 之后：按 source checkpoint（QQ 为 IMAP UID）增量
- 游标在 `data/synced.sqlite`

### 秋招策略（插件约定）

1. 粗过滤：无招聘信号 / 噪声 / 宣讲群发 → 忽略
2. 精解析：启用 LLM 时先模型；模型明确拒绝不兜底；超时/坏 JSON 才回退启发式
3. `schedule_invite` 不建日程、不建提醒；启用 Bark 时即时推送
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
    dispatch/calendar/
    dispatch/reminders/
    dispatch/bark/
  cli/
```

## LLM 配置

见 `config.example.yaml`。需同时配置 `llm_api_base` + `llm_api_key`。
