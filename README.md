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
2. 在 `.env` 填写 `CALDAV_URL`、`CALDAV_USERNAME`、`CALDAV_PASSWORD`。
3. 分别把显示名称精确填入 `calendar_name` 与 `reminders_list`。
4. 名称留空表示该渠道未启用；对应邮件会被消耗，不改道、以后也不补送。
5. 在需要查看的设备上添加同一个 CalDAV 账户。

`uv run mailhub list-calendars` 与 `uv run mailhub list-reminders` 可核对 collection 名称。固定场次写入日历；开放窗口类任务写入提醒事项。

### 3. Bark

Bark 是可选的即时推送渠道，默认未启用。当前只有 `schedule_invite` 进入 Bark；已确认场次、改期、取消和开放窗口不会改道到 Bark。

- 仅支持自建服务器和一个设备密钥，不使用官方默认地址
- 环境变量同时提供 `BARK_SERVER_URL` 与 `BARK_KEY` 即启用；只填其中一项则 `sync` / `sync --dry-run` 在拉信前失败
- 两项都空则为未启用
- dry-run 只校验配置，不向 Bark 服务器发请求

## 安装

```bash
cd /path/to/qiuzhao-mail2calendar
uv sync --extra dev
cp .env.example .env
# 编辑 .env 填入邮箱、授权码、CalDAV 等部署项
# 行为旋钮在仓库内 config.yaml（可随版本修改）
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

# 首次升级身份模型时，从现有 CalDAV UID 回填本地事项身份
uv run mailhub migrate-identities --dry-run
uv run mailhub migrate-identities

# 从 IMAP 重拉原邮件，预览并迁移已有提醒事项的窗口和预计耗时标题
# 新邮件按 IMAP 原生定位键精确取回，旧记录按 Message-ID 本地匹配
# 覆盖同一条，不会新建；输出会区分更新与原邮件未找到
uv run mailhub migrate-reminder-titles --dry-run
uv run mailhub migrate-reminder-titles

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

mailhub 分开保存两类身份：iCalendar UID 标识可跨改期延续的事项；邮箱来源、Message-ID 与 IMAP 原生定位键标识具体邮件。CalDAV href 只是远端资源地址。事项与邮件是多对多关联，不能互相替代。

## Docker

仓库根目录即可构建镜像。部署项用 `--env-file` / 环境变量传入，**不要**打进镜像。行为旋钮在 `config.yaml`：构建时复制进镜像；Compose 会把仓库里的文件只读挂进去，改旋钮不必重建。

```bash
docker build -t mailhub:local .
mkdir -p data
docker run --rm --env-file .env -e TZ=Asia/Shanghai \
  -v "$PWD/data:/app/data" \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  mailhub:local mailhub sync --dry-run
```

正式跑一次（会写 CalDAV、推进游标）：

```bash
docker run --rm --env-file .env -e TZ=Asia/Shanghai \
  -v "$PWD/data:/app/data" \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  mailhub:local
```

推送到 `master` 后，GitHub Actions 会把镜像发到 [ghcr.io/optizephyr/mailhub](https://github.com/optizephyr/mailhub/pkgs/container/mailhub)。能访问 GitHub 的机器可以只拉镜像、不克隆源码（仍需自备 `.env`、`config.yaml` 和 `data` 目录）：

```bash
docker pull ghcr.io/optizephyr/mailhub:latest
```

包默认私有时，先 `echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin`。

国内服务器直连 `ghcr.io` 往往超时。可在阿里云 ACR 个人版绑定本仓库、开「海外机器构建」和「代码变更自动构建」，镜像进 ACR 后只拉国内地址。Compose 用 `.env` 里的 `MAILHUB_IMAGE` 覆盖默认 GHCR（地域、命名空间以控制台为准）：

```bash
docker login registry.cn-hangzhou.aliyuncs.com
# .env: MAILHUB_IMAGE=registry.cn-hangzhou.aliyuncs.com/<命名空间>/mailhub:latest
docker compose pull && docker compose up -d
```

登录用控制台「访问凭证」，不是 GitHub token。

### Compose（定时循环）

服务器上若有本仓库：

```bash
cp .env.example .env   # 已有 .env 勿覆盖
# 编辑 .env
```

能访问 GitHub、要在本机构建时：`docker compose up -d --build`。

只拉已发布镜像（GHCR 或 ACR）时不要加 `--build`，否则会按 `Dockerfile` 在本机构建并访问 `ghcr.io`：

```bash
docker compose pull && docker compose up -d
```

默认每 15 分钟执行一次 `mailhub sync`；游标和日志在宿主机 `./data`。只改代码且在本机构建时再 `--build`；只改 `config.yaml` 后 `docker compose up -d` 即可。`.env` 不用打进镜像。

一次性命令（不进循环）：

```bash
docker compose run --rm mailhub mailhub list-calendars
docker compose run --rm mailhub mailhub sync --dry-run
```
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

见 `.env.example`。需同时配置 `LLM_API_BASE` 与 `LLM_API_KEY`；模型名在 `config.yaml` 的 `llm_model`。
