# 议题跟踪：GitHub

本仓库的议题与 PRD 记在 GitHub Issues。所有操作使用 `gh` CLI。

## 约定

- **创建议题**：`gh issue create --title "..." --body "..."`。多行正文用 heredoc。
- **阅读议题**：`gh issue view <number> --comments`，用 `jq` 过滤评论并取出标签。
- **列出议题**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，按需要加 `--label` 和 `--state`。
- **评论议题**：`gh issue comment <number> --body "..."`
- **加减标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：`gh issue close <number> --comment "..."`

仓库从 `git remote -v` 推断——在 clone 目录里跑 `gh` 会自动识别。

## 把 PR 当作分诊入口

**PRs as a request surface: no.** （若本仓库把外部 PR 当作功能请求，改成 `yes`；`/triage` 会读这个开关。）

设为 `yes` 时，PR 走与议题相同的标签和状态，命令换成 `gh pr` 对应项：

- **阅读 PR**：`gh pr view <number> --comments`，diff 用 `gh pr diff <number>`。
- **列出待分诊的外部 PR**：`gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，只保留 `authorAssociation` 为 `CONTRIBUTOR`、`FIRST_TIME_CONTRIBUTOR` 或 `NONE` 的（丢掉 `OWNER` / `MEMBER` / `COLLABORATOR`）。
- **评论 / 打标签 / 关闭**：`gh pr comment`、`gh pr edit --add-label` / `--remove-label`、`gh pr close`。

GitHub 的 issue 与 PR 共用一套编号，光写 `#42` 可能是其中任意一种——先 `gh pr view 42`，不行再 `gh issue view 42`。

## 技能说「publish to the issue tracker」时

创建一条 GitHub issue。

## 技能说「fetch the relevant ticket」时

执行 `gh issue view <number> --comments`。

## Wayfinding 操作

供 `/wayfinder` 使用。**地图（map）** 是一条议题，**子议题** 作为工单。

- **地图**：一条带 `wayfinder:map` 标签的议题，正文含 Notes / Decisions-so-far / Fog。`gh issue create --label wayfinder:map`。
- **子工单**：通过 GitHub sub-issue（`gh api` 调 sub-issues 接口）挂到地图上。若仓库未开 sub-issue，把子项写进地图正文的任务列表，并在子议题正文顶部写 `Part of #<map>`。标签：`wayfinder:<type>`（`research` / `prototype` / `grilling` / `task`）。认领后指派给当前开发者。
- **阻塞**：以 GitHub **原生 issue 依赖** 为准（界面可见）。加边：`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`，其中 `<blocker-db-id>` 是阻塞方的数字 **database id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，不是 `#number` 也不是 `node_id`）。GitHub 用 `issue_dependencies_summary.blocked_by` 报告未关闭的阻塞（当前闸门）。依赖不可用时，退化为子议题正文顶部一行 `Blocked by: #<n>, #<n>`。所有阻塞方关闭后，该工单才算解除阻塞。
- **前沿查询**：列出地图下仍打开的子项（`gh issue list --state open`，范围限 sub-issue / 任务列表），丢掉仍有未关闭阻塞（`issue_dependencies_summary.blocked_by > 0`，或 `Blocked by` 行里还有打开的议题）或已有指派人的；按地图顺序取第一条。
- **认领**：`gh issue edit <n> --add-assignee @me`——本会话第一次写入。
- **办结**：`gh issue comment <n> --body "<answer>"`，再 `gh issue close <n>`，然后在地图的 Decisions-so-far 追加上下文指针（gist + 链接）。
