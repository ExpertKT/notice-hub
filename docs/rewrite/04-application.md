# G1 · 应用契约

Web 在 G3 改为调用本文件的函数。返回结构与今天的 HTTP JSON 对齐，主线控制器只做鉴权、参数校验和状态码。

用例接收 `Clock` 与端口，不读环境变量。下面省略每个函数都有的依赖参数，装配由 `bootstrap` 完成。

错误约定：非法参数抛 `ValueError`，由 Web 转成今天的 400 文案。缺少资源返回 `ok: false` 或 `False`，与现有函数一致。用例不捕获 `ValueError` 后再伪装成成功。

## 入站与调度（CLI 与适配器调用，Web 不直接调用）

```text
accept_message(event: MessageEvent) -> bool
```

等价于今天的 `DigestService.on_message`。

- 无 `msg_id`：记日志，返回 `False`。
- 群不在对应来源的白名单：返回 `False`。QQ 沿用 `accepts_group`（空群号放行，空白名单拒绝）。钉钉、微信、学习通沿用各自白名单。
- 入库失败：记日志，返回 `False`。不让接收线程退出。
- 新插入才执行规则接线（`02-module-map.md` 的 7 条）。
- 返回值是「是否新插入」，不是「是否建了待办」。

```text
tick(now: datetime | None = None) -> list[Digest]
```

处理未处理消息、合并事件、按资格建待办、归档、推送、到期提醒。`now` 缺省用 `Clock.now()`。

建待办门槛（替换 `service._sync_tasks` 时保持）：

- `kind` 为 `morning` 或 `evening` 的摘要不建任务。
- 先合并 `event_extract` 的 medium 及以上事件，再判断资格。LLM 已有 `deadline` 时事件不覆盖时间。地点追加到 `details`，同一条目不重复。
- 分类不是 `urgent`/`action`，且没有 `action`，且没有 `deadline`：跳过。
- 时间只由事件补出、事件级别 `medium`、没有 action、分类也不是 `urgent`/`action`：跳过。
- 摘要或正文含「链路自测」：跳过。
- `task_key` 优先用消息 id；没有则 `auto:` 加摘要、截止时间、正文前 200 字的 sha1 前 16 位。
- 来源：记录上的 `dingtalk` / `chaoxing` / `wechat` 保持；事件名含 `file` 则 `file`，含 `image` 则 `image`；规则路径是 `rule`；其余沿用分类给出的来源，缺省 `qq_message`。
- 建待办抛错：摘要和投递照常，日志说明可用历史回填补建。

```text
import_existing_tasks(days: int = 7) -> int
```

从历史摘要回填。与 `tick` 同一套门槛，另外丢掉早于当天 0 点的截止时间，以及以 `?` / `？` 结尾的疑问句。重复执行安全。

## 核心用例（20）

Web 路由名是主线的调用点，重写方不改路由表。

### 待办与日历

```text
list_task_board(now: datetime | None = None) -> TaskBoard
```

`GET /api/tasks`。

`TaskBoard` 键：`headline, subline, progress, stats, candidates, today, week, later, done, pinned`。

分组规则与 `webapp.group_tasks` + `overview` 相同：

- 跳过 `dismissed`、`expired`。
- `candidate` 单独成组，最多 40。附 `confidence_text`、`confidence_reason`。
- `done` 按 `done_at` 倒序，最多 20。
- 无截止时间，或截止晚于现在加 7 天：`later`。截止日在今天或更早：`today`。其余：`week`。各组最多 40。
- 组内紧急优先（`effective_urgent`）。
- `pinned` 来自全部任务里 `pinned` 为真的行，按截止时间、重要度、id 排序。
- 群 id 在出口换成群名。微信来源用微信别名，其余用 `group_aliases`。
- `stats` 即 `TaskStats`。
- `headline` / `subline` / `progress` 文案与 `webapp.overview` 现有分支一致，包括「今天 N 件 · 逾期 M 件」「N 条通知待确认」「所有待办都清空了」。

```text
create_task(summary: str, deadline: str = "") -> CreateTaskResult
```

`POST /api/tasks/create`。`summary` 必须是字符串，空白折叠后 1–200 字。否则 `ValueError`，文案依次为「summary 必须是文本」「请填写待办内容」「待办内容请控制在 200 字以内」。`deadline` 空，或可被解析的时间文本；非法时「截止时间格式不对，请重新选择」；类型不对时「deadline 必须是文本」。返回的 `deadline` 是规范化后的 ISO，未填则为空串。`task_key = manual:<16 hex>`，`source=manual`，`confidence=1.0`，`classification_reason=页面手动新建`。返回 `ok, task_id, summary, deadline`。

```text
apply_task_action(task_id: int, action: str, done: bool | None = None, until: str = "") -> ActionResult
```

`POST /api/tasks/<id>`。`action` 为 `confirm | dismiss | done | reopen | snooze`。缺省时 `done: true/false` 映射为 `done` / `reopen`。`snooze` 的 until 由端口规则计算。返回 `ok, id, action, done, status`。

```text
correct_task(task_id: int, correction: str, value: Any = "") -> ActionResult
```

同一路由、`action=correct`。纠错类型见端口。`not_notice` / `not_task` / `duplicate` 把状态改为 `dismissed`。`not_urgent` 把 `urgent` 降为 `action` 且重要度不超过 3。`category` 只允许 `urgent | action | academic | info`。改分类时重要度用 `{urgent: 5, action: 3, academic: 3, info: 1}`。这张表和 `summarizer.IMPORTANCE_BY_CATEGORY`（`action` 为 4、`info` 为 3）不是同一份，两份都保持。

```text
set_task_urgent(task_id: str, urgent: bool | None) -> TaskResult
set_task_pinned(task_id: str, pinned: bool) -> TaskResult
```

`task_id` 必须是 ASCII 数字字符串，长度 1–18，数值 ≥ 1，否则 `ValueError("task_id 必须是正整数文本")`。返回 `ok` 加整条任务。置顶拒绝已完成任务，错误文案为「已完成的任务不能置顶」。任务不存在时 `error` 为「任务不存在」。

```text
task_stats() -> TaskStats
```

`GET /api/tasks` 与 `GET /api/meta` 共用这一份，避免两处口径分开算。

```text
render_calendar_ics() -> str
```

`GET /calendar.ics`。VCALENDAR 文本，含 VTIMEZONE。鉴权仍在 Web（要 token）。

```text
describe_calendar_subscription() -> CalendarInfo
```

`GET /api/sync/info`。成功时键：`ok, lan_base, port, events, calendars`。`calendars` 每条有 `name, kind, url, hint`，返回前再写入同一个 `events` 数。`kind` 为 `public` 或 `lan`。没有单独的 `primary` 键，也没有 `virtual` 字段。推荐地址由前端取 `kind == public` 的第一条，否则取列表第一条。虚拟网卡地址在列局域网主机时丢掉，不进入 `calendars`。

对照 `e1efea9` 的 `webapp._sync_info_response`，仓库里没有 `_primary_calendar_url`。复核意见里的 `primary` / `virtual` 不补进本契约；那份代码进 `origin/main` 之后再改。

### 摘要与收件箱

```text
list_notices() -> NoticeList
```

`GET /api/notices`。固定最近 8 条摘要。每项 `id, kind, created_at, summary, body`，`body` 最多 600 字。

```text
start_inbox_classify(groups: Sequence[str], since: str = "", until: str = "", limit: int = 1000) -> JobStart
```

`POST /api/inbox/classify`。`groups` 1–500 个。`since` / `until` 为 `YYYY-MM-DD` 或空。`limit` 钳到 1–20000，默认 1000。返回 `ok, started`。后台线程；与历史回填共用一把任务槽。

```text
promote_inbox_item(msg_id: str, title: str = "", due: str = "") -> PromoteResult
```

`POST /api/inbox/promote`。`msg_id` 必填且不超过 256。`title` 不超过 500。`due` 为 ISO 或空。`task_key` 用 `msg_id`。返回 `ok, task_id, error`。

```text
poll_job(kind: str) -> JobSnapshot
```

`kind` 为 `history` 或 `classify`。`GET /api/history/fetch/status`、`GET /api/inbox/classify/status`。键：`running, stage, done, total, note, result`。两种任务读同一槽。

### 历史回溯

```text
probe_history_floor(group_id: str) -> FloorResult
```

`GET /api/history/floor`。`group_id` 必填，不超过 128。

```text
start_history_backfill(groups: Sequence[str], since: str = "", until: str = "") -> JobStart
```

`POST /api/history/fetch`。`groups` 1–500。日期成对出现时 `since <= until`。

```text
cancel_job() -> CancelResult
```

`POST /api/history/fetch/cancel`。当前槽是正在跑的历史任务时返回 `ok: true, cancelled: true`。没有正在跑的历史任务时，Web 回 409，`error` 为「当前没有正在运行的回溯任务」。启动时槽被占用也回 409：占用者是历史则为「已有回溯任务在进行」，是分类则为「已有通知提取任务在进行」。

### 订阅与试推

```text
save_subscriptions(groups: Sequence[str], aliases: Mapping[str, str]) -> SubscriptionResult
```

`POST /api/subscriptions`。两组都可以空。写 `.env` 的 `QQ_DIGEST_GROUPS` 与 `QQ_DIGEST_GROUP_ALIASES`，并热更新已装配的 Settings。返回 `ok, groups, applied`。

全新安装的 `.env` 可能没有这两行。保存必须补上这两行，不能因为缺行而失败。

```text
test_push_channels() -> PushTestResult
```

`POST /api/settings/test-push`。对每个已配置通道发一条测试消息。返回 `ok, results[{channel, target, ok, error}]`。

```text
correction_stats(days: int = 30) -> CorrectionStats
correction_insights(days: int = 30, min_group_hits: int = 3) -> list[str]
```

`GET /api/insights`。路由把 `days` 钳到 1–365，缺省 30，然后两次调用，返回 `{stats, insights}`。`min_group_hits` 路由不传，保持 3。返回键见端口。这两条原先既不在 18 条里，也不在下面的排除清单里，现补为第 19、20 条。

## 明确不进本次用例的路由

NapCat 安装与进程、微信、钉钉、学习通、配对、隧道、防火墙、向导、托管、设置九域保存、更新、公告、社区计数、自检、概览、静态页。它们留在现有 Web 模块。`GET /api/meta` 里来自服务运行时的那一半（推送通道、启动时间、预算）在 G3 由主线向用例取 `task_stats` 和健康快照，其余展示字段仍由 `settings_api` 组装。

## 任务槽

历史回填和收件箱分类共用一个进程内槽：同时只跑一个，`poll_job` 与 `cancel_job` 看的是同一个状态。重写不把槽放进数据库。进程重启后槽为空，与今天的 `_JOB_STATE` 一样。
