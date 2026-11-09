# 群务台 HTTP 接口与命令行参考

适用版本：`1.1.0-beta.16`（`noticehub/version.py` 的 `APP_VERSION`）。

每条接口对应 `noticehub/routes.py` 路由表里的一项，参数字段和返回键来自 `noticehub/webapp.py` / `noticehub/settings_api.py` 的处理方法。命令、路径、字段名按原样抄。

## 0. 规模与数法

**共 105 条路由**：62 条 GET 字面量 + 41 条 POST 字面量 + 2 条前缀模式路由（`/js/*`、`/api/tasks/<id>`）。

数法（在仓库根目录执行，输出的四个数字相加即 105）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -c "import noticehub.routes as r; print(len(r.GET_ROUTES), len(r.POST_ROUTES), len(r.GET_PATTERN_ROUTES), len(r.POST_PATTERN_ROUTES))"
# 62 41 1 1
```

路由表本身在 `noticehub/routes.py:13`（GET）与 `noticehub/routes.py:78`（POST），每条是 `路径 → (处理方法名, 是否免鉴权)`。`do_GET` / `do_POST`（`noticehub/webapp.py:3465` / `:4391`）查表后调 `getattr(self, 方法名)(path)`。

## 1. 通用约定

### 1.1 鉴权

令牌来自 `.env` 的 `NOTICEHUB_WEB_TOKEN`（旧名 `QQ_DIGEST_WEB_TOKEN`）。三种带法，按判定顺序（`noticehub/webapp.py:1691`）：

1. `Cookie: nh_session=<值>` —— 会话 Cookie，值是 `HMAC(token, b"nh-session-v1")` 的十六进制（`webapp.py:87`）。确定性 HMAC，重启后旧 Cookie 仍有效。
2. `X-Token: <token>` —— 请求头。
3. `?token=<token>` —— 查询参数。命中后会在响应里下发 `Set-Cookie`，后续请求可以不带。

令牌为空时（只可能出现在绑定环回的部署），**只放行环回来源**（`127.0.0.1` / `::1` / `::ffff:127.0.0.1`）。

免鉴权路由共 14 条：11 条 GET（`/api/health`、`/health`、`/icon.svg`、`/manifest.webmanifest`、`/sw.js`、`/js/*`、`/api/donate/qr.png`、`/api/onboarding`、`/api/onboarding/next`、`/api/onboarding/status`、`/api/pair/status`）+ 3 条 POST（`/api/onboarding/accept`、`/api/pair/start`、`/api/session/exchange`）。这些是手机配对和首装向导要用的，必须能在还没令牌时访问。

鉴权失败：

```json
{"ok": false, "error": "invalid token"}
```

`Accept` 含 `text/html` 时（浏览器直接打开）回 401 的 HTML 说明页而不是 JSON（`webapp.py:3481`）。

### 1.2 错误与限流

- 未匹配到路由：`404 {"ok": false, "error": "not found"}`。
- 爆破防护：同一来源 60 秒内失败 30 次（`AUTH_FAILURE_LIMIT`，`webapp.py:75`）后直接 `429 {"ok": false, "error": "尝试过于频繁，请稍后再试"}`，连比对都不做（`webapp.py:1684`）。
- POST 请求体上限 `MAX_BODY_BYTES = 64 * 1024`（`noticehub/settings_api.py:39`）；超限先排空再回 `400 {"ok": false, "error": "bad body"}`。
- 多数接口失败时**仍然回 200**，用 `ok: false` 表示业务失败（页面按 `ok` 分支）。带明确状态码的是：参数错 400、鉴权 401、限流/配对频繁 429、任务不存在 404、模块不可用 503、上游失败 502。
- 所有 JSON 响应带 `Cache-Control: no-store`。

### 1.3 返回体形状约定

成功后多数接口含 `"ok": true`。列表型接口给 `items` 或 `groups`；状态型接口给 `state` + `detail` + `action`（`state` 只有四种：`off` / `needs_setup` / `ready` / `error`）。

有几处**故意不带 `ok`**：`GET /api/meta`、`GET /api/tasks`、`GET /api/insights`、`GET /api/notices`、`GET /api/settings` 直接返回业务结构，失败时才补 `{ok: false, error}`。判断成功与否以各表为准，别统一按 `ok` 写死。

## 2. 概览与状态

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/overview` | 需要 | — | `{ok, sources}`，`sources` 键：`qq`、`napcat`、`wechat`、`dingtalk`、`chaoxing`、`funnel`、`push`，每项 `{state, label, detail, action}` |
| `GET` | `/api/meta` | 需要 | — | `{groups, channels, insights, reminders, started_at, push_budget, quiet_hours, channel, web_port, web_port_source, push, push_help, llm, providers, stats}`，共 15 键，**不带 `ok`** |
| `GET` | `/api/setup/status` | 需要 | — | `{ok, platforms}`，`platforms` 键：`qq`、`wechat`、`dingtalk`、`chaoxing`、`push`、`llm`；每项含 `configured`、`status`，另按平台带 `headline`/`action`（下一步该点什么），`qq` 另有 `engine_state`、`login_state`，`push` 另有 `channels`，`llm` 另有 `provider` |
| `GET` | `/api/selfcheck` | 需要 | — | `{ok, items[], checking, cached, last_push_at}`，`items[]` 每项 `{key, label, state, value, hint}` |
| `GET` | `/api/health` | **公开** | — | `200/503 {ok, service, database, receivers{dingtalk,wechat,chaoxing}, last_push_at, update_source, channels}` |
| `GET` | `/health` | **公开** | — | 同上 |
| `GET` | `/api/announcements` | 需要 | `refresh=1\|true\|yes` | `{ok, items[], current_version, checked_at, checking, from_cache, error}` |
| `GET` | `/api/notices` | 需要 | — | `{items[], error?}`（推送记录） |
| `GET` | `/api/community/counts` | 需要 | — | `{ok, counts}`（读不到的项是 `null`） |
| `GET` | `/api/config/audit` | 需要 | — | `{ok, items[]}`（只报已生效的值，不重复探测） |
| `GET` | `/api/insights` | 需要 | `days`（1–365，默认 30） | `{stats, insights}` |

行为要点：

- **`/api/overview` 是通道判定的唯一来源**：设置页、服务自检、合并向导都读它，三处的结果因此一致。它不自己探测：自检已经算过的通道状态会挂在 `self._precomputed_status` 上被复用（`webapp.py:2047`），避免一次请求里把同一批外部探测跑两遍。
- **`/api/health` 会真查库**（`SELECT 1`），进程活着但库读不了时回 503（`webapp.py:2468`）。它只回粗粒度状态，不暴露路径、令牌、群 ID——这是公网可达的接口。`receivers` 里 `idle`（一次都没跑过）算 `unknown`，不给绿点。
- **`/api/selfcheck` 是单飞的**：同一时刻只算一遍，并发来的第二次等第一次算完复用结果（`webapp.py:2837`），最多等 `SELFCHECK_WAIT_SECONDS = 30` 秒。复用命中时 `cached: true`。
- `/api/community/counts` 在开关打开时**顺手起一个后台线程**上报「今日打开」，页面不等它（`webapp.py:2575`）。开关是 `NOTICEHUB_COMMUNITY_TELEMETRY`（默认 `0`）。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/overview?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/meta?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/setup/status?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/selfcheck?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/health"                    # 免鉴权
curl.exe -s "http://127.0.0.1:8766/health"
curl.exe -s "http://127.0.0.1:8766/api/announcements?refresh=1&token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/notices?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/community/counts?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/config/audit?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/insights?days=7&token=<token>"
```

## 3. 待办

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/tasks` | 需要 | — | `{headline, subline, progress, stats{total,candidate,open,done,dismissed,expired,overdue}, candidates[], today[], week[], later[], done[], pinned[]}` |
| `POST` | `/api/tasks/create` | 需要 | `summary`（必填，≤200 字）、`deadline`（可选） | `{ok, task_id, summary, deadline}` |
| `POST` | `/api/tasks/pin` | 需要 | `task_id`、`pinned`（必须是布尔） | `{ok, …}`；任务不存在回 404 |
| `POST` | `/api/tasks/urgent` | 需要 | `task_id`、`urgent`（`true`/`false`/`null`） | `{ok, …}`；不存在回 404 |
| `POST` | `/api/tasks/clear-finished` | 需要 | — | `{ok, removed}` |
| `POST` | `/api/tasks/<id>` | 需要 | `action`、`done`、`correction`、`value` | `{ok, id, action, done, status}` |

行为要点：

- `/api/tasks/<id>` 的 `action` 只有六个合法值（`noticehub/application/tasks.py:65`）：`confirm`、`dismiss`、`done`、`reopen`、`snooze`、`delete`。别的值回 `400 unsupported action`。
- 只给 `done` 不给 `action` 时，`true` 当 `done`、`false` 当 `reopen`（`tasks.py:93`）。
- `action: "correct"` 走纠错分支（`webapp.py:4357`），需要 `correction`；`value` 可选。
- **`snooze` 不接受自定义时间**：路由只透传 `done`，`until` 拿不到，一律用默认值=次日早晨的截止提醒时刻（`tasks.py:68`）。
- `/api/tasks/clear-finished` 只清已完成/已忽略/已过期，`open`、`candidate` 一条不动（`webapp.py:3919`）。幂等：重复调用只是 `removed: 0`。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/tasks?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/tasks/create?token=<token>" -H "Content-Type: application/json" --data-raw '{"summary":"交读书报告","deadline":"2026-11-05T23:59"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/tasks/pin?token=<token>" -H "Content-Type: application/json" --data-raw '{"task_id":"12","pinned":true}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/tasks/urgent?token=<token>" -H "Content-Type: application/json" --data-raw '{"task_id":"12","urgent":true}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/tasks/clear-finished?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/tasks/12?token=<token>" -H "Content-Type: application/json" --data-raw '{"action":"done"}'
```

## 4. 收件箱

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/inbox` | 需要 | `verdict`（`notice`/`suspect`/`noise`/`promoted`/空）、`group_id`、`q`、`limit`（1–100，默认 50）、`offset`（默认 0） | `{ok, items[], counts, total}` |
| `GET` | `/api/inbox/image` | 需要 | `msg_id`（必填、唯一） | 图片字节；缺失回 404 |
| `POST` | `/api/inbox/classify` | 需要 | `groups[]`、`since`、`until`、`limit`（1–20000，默认 1000） | `{ok, started}`；已有任务在跑回 409 |
| `GET` | `/api/inbox/classify/status` | 需要 | — | `{running, stage, done, total, note, result, detail, state}` |
| `POST` | `/api/inbox/classify/cancel` | 需要 | — | `{ok, cancelled}`（没在跑也回 200） |
| `POST` | `/api/inbox/promote` | 需要 | `msg_id`、`title`（≤500 字）、`due`（ISO 时间） | `{ok, task_id}` |
| `POST` | `/api/inbox/delete` | 需要 | `msg_ids[]`（1–200 个）或单个 `msg_id` | `{ok, deleted}` |

行为要点：

- `verdict=promoted` 是「已转待办」这个视图，不是第四种判定（`webapp.py:3183`）。
- `counts` 无论筛选与否都返回全量四类计数；`total` 在带 `group_id`/`q` 时是实际重新数出来的，可能比 `counts` 小。
- `items[]` 每项额外带 `has_image`（`webapp.py:3188`）。
- `/api/inbox/image` 只服务 `data/files/` 下的图片，拒绝 `..`、斜杠、非图片 MIME（含 `image/svg+xml`）。**副作用**：`POST /api/inbox/delete` 会把原图一起删掉，不留孤儿（`webapp.py:4083`）。
- 分类任务与历史回溯**共用一把任务锁**：一个在跑时另一个回 409，提示「已有回溯任务在进行」。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/inbox?verdict=notice&limit=20&token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/inbox/image?msg_id=123456&token=<token>" -o pic.jpg
curl.exe -s -X POST "http://127.0.0.1:8766/api/inbox/classify?token=<token>" -H "Content-Type: application/json" --data-raw '{"groups":["123456"],"limit":500}'
curl.exe -s "http://127.0.0.1:8766/api/inbox/classify/status?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/inbox/classify/cancel?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/inbox/promote?token=<token>" -H "Content-Type: application/json" --data-raw '{"msg_id":"123456","title":"交作业","due":"2026-11-05T23:59"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/inbox/delete?token=<token>" -H "Content-Type: application/json" --data-raw '{"msg_ids":["123456"]}'
```

## 5. 历史回溯

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `POST` | `/api/history/fetch` | 需要 | `groups[]`、`since`、`until`（`YYYY-MM-DD`） | `{ok, started}`；已有任务在跑回 409 |
| `POST` | `/api/history/fetch/cancel` | 需要 | — | `{ok, cancelled}`；没在跑回 409 |
| `GET` | `/api/history/fetch/status` | 需要 | — | `{running, stage, done, total, note, result, detail}` |
| `GET` | `/api/history/floor` | 需要 | `group_id`（≤128 字符） | `{ok, floor_ts, total_seen, pages, partial, note, error}` |

行为要点：

- 日期必须严格是 `YYYY-MM-DD`（`noticehub/application/jobs.py:23`），`since > until` 回 400。
- `/api/history/floor` 是「NapCat 缓存最老能追到哪天」的探测，最多翻 2 页、单页超时 15 秒（`webapp.py:3137`）——最坏约 30 秒，不是无限等。
- 取消接口在任务已结束时回 409（与收件箱那边的取消不同，后者总是 200）。

```powershell
curl.exe -s -X POST "http://127.0.0.1:8766/api/history/fetch?token=<token>" -H "Content-Type: application/json" --data-raw '{"groups":["123456"],"since":"2026-10-01","until":"2026-10-25"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/history/fetch/cancel?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/history/fetch/status?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/history/floor?group_id=123456&token=<token>"
```

## 6. 日历与订阅

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/sync/info` | 需要 | — | `{ok, calendars[], …}`；拿不到地址回 503 |
| `GET` | `/api/sync/qr.png` | 需要 | `text`（日历 URL，必填、唯一、≤512 字符） | PNG；参数错回 400 |
| `GET` | `/api/sync/test` | 需要 | `url`（必须是本服务自己公布的地址之一） | `{ok, events, url}`；上游失败 502 |
| `GET` | `/calendar.ics` | 需要 | — | iCalendar 文本 |
| `GET` | `/calendar` | 需要 | — | 302 回首页 |

行为要点：

- `/api/sync/test` 只允许测**本服务公布过的**地址（局域网、Tailscale）以及 `127.0.0.1` / `localhost` 的同端口 `/calendar.ics`（`webapp.py:3039`）。它是个 SSRF 防护：拒绝任意外部 URL。
- `/api/sync/qr.png` 只接受 `webcal:` / `http:` / `https:`，且不含凭据、不含空白字符。
- `calendar.ics` 的 token 用的是同一个 `NOTICEHUB_WEB_TOKEN`，订阅地址里带着它——分享给别人等于分享令牌。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/sync/info?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/sync/qr.png?text=webcal%3A%2F%2F127.0.0.1%3A8766%2Fcalendar.ics%3Ftoken%3D<token>&token=<token>" -o cal.png
curl.exe -s "http://127.0.0.1:8766/api/sync/test?url=http%3A%2F%2F127.0.0.1%3A8766%2Fcalendar.ics%3Ftoken%3D<token>&token=<token>"
curl.exe -s "http://127.0.0.1:8766/calendar.ics?token=<token>" -o cal.ics
curl.exe -s -o NUL -w "%{http_code}`n" "http://127.0.0.1:8766/calendar?token=<token>"
```

## 7. 摘要实验室

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/lab/status` | 需要 | — | `{ok, result{search,replies,events,rules}, enabled{…}}` |
| `GET` | `/api/lab/rules` | 需要 | — | `{ok, result}` |
| `POST` | `/api/lab/rules` | 需要 | `rules[]` | `{ok, …}` |
| `POST` | `/api/lab/rules/test` | 需要 | `message` | `{ok, …}` |
| `GET` | `/api/lab/search` | 需要 | `q`（必填，≤2000）、`group_id`、`since`、`until`、`limit`（1–500） | `{ok, result}` |
| `GET` | `/api/lab/replies` | 需要 | `limit`（1–500） | `{ok, result}` |
| `POST` | `/api/lab/replies/ack` | 需要 | `msg_id` | `{ok, …}` |
| `POST` | `/api/lab/events` | 需要 | `text`（≤2000） | `{ok, …}` |

行为要点：

- 实验室四个开关默认全关（`NOTICEHUB_LAB_SEARCH` / `_REPLIES` / `_EVENTS` / `_RULES`）。没开时**回 403**「未开启…」，不是 404 也不是 200（`webapp.py:1624`）。
- 参数非法回 400，模块内部失败回 503。这一层会把底层返回的 `{ok: false, error}` 摊平，避免把「第 2 条规则非法」当成保存成功。
- `POST /api/lab/events` 与 `POST /api/lab/replies/ack` **共用同一个处理方法**（`routes.py:91-92`），按路径分流。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/lab/status?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/lab/rules?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/lab/rules?token=<token>" -H "Content-Type: application/json" --data-raw '{"rules":[]}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/lab/rules/test?token=<token>" -H "Content-Type: application/json" --data-raw '{"message":"明天上午交实验报告"}'
curl.exe -s "http://127.0.0.1:8766/api/lab/search?q=实验报告&limit=20&token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/lab/replies?limit=20&token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/lab/replies/ack?token=<token>" -H "Content-Type: application/json" --data-raw '{"msg_id":"123456"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/lab/events?token=<token>" -H "Content-Type: application/json" --data-raw '{"text":"周五下午三点开组会"}'
```

## 8. QQ / NapCat

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/napcat/status` | 需要 | — | `{ok, stage, online, user_id, nickname, napcat_installed, napcat_running, napcat_root, qr_stamp, qr_age_seconds, qr_stale, waiting_seconds, good, error}` |
| `GET` | `/api/napcat/qrcode` | 需要 | — | PNG；没有二维码回 404 |
| `GET` | `/api/napcat/groups` | 需要 | — | `{ok, groups[{group_id, name, member_count, selected}]}` |
| `GET` | `/api/napcat/doctor` | 需要 | — | `{ok, steps[], hint, broken, error}` |
| `GET` | `/api/groups/suggest` | 需要 | — | `{ok, source, groups[], counts, error}` |
| `GET` | `/api/qq/groups/status` | 需要 | — | `{ok, group_count, is_fetching, last_attempt_at, last_success_at, last_error, next_run_at}` |
| `GET` | `/api/napcat/install/status` | 需要 | — | `{ok, running, stage, done, total, note, result, downloaded_bytes, total_bytes, extracted, entries}` |
| `POST` | `/api/napcat/install` | 需要 | `accept_sha256`（首次确认指纹时带） | `{ok, installed, already_installed, root, version, bytes, error}` |
| `POST` | `/api/napcat/launch` | 需要 | `uin`（可选） | `{ok, already_running, pid, command[], profile_dir, boot}` |
| `POST` | `/api/napcat/restart` | 需要 | `uin`（可选） | `{ok, stopped[], already_stopped, pid, api_up, waiting_for_login}` |
| `POST` | `/api/napcat/connect` | 需要 | —（请求体不读内容） | `{ok, api_url, tried[], persisted, persist_error, status}` |
| `POST` | `/api/napcat/autosetup` | 需要 | `accept_sha256`（可选） | `{ok, steps[], qrcode_path, uin, applied_via, restart_required, stage}` |

行为要点：

- `stage` 是这套接口的核心状态机：`online` / `waiting_qr` / `waiting_login` / `stopped` / `not_installed` / `error`。`waiting_qr` 时 `qr_age_seconds` 说明码有多旧，`qr_stale` 表示该换码了。
- **`/api/napcat/install` 是同步的**：请求会一直等到装完（可能几分钟）才返回最终结果，期间进度写进任务快照，前端并行轮询 `/api/napcat/install/status` 显示百分比（`webapp.py:4158`）。超时或断连时页面看着像卡住，这是设计如此。
- `POST /api/napcat/connect` 只读 NapCat 自己的配置、**绝不改**，写回的是我们自己的 `NOTICEHUB_NAPCAT_API_URL`（`webapp.py:4252`）。写不进 `.env` 时仍返回 `ok: true`，但 `persisted: false` 并带 `persist_error`——重启会回到旧地址。
- 四个「改本机状态」的接口（install/launch/restart/autosetup）**不做确认校验**，靠页面按钮；它们会真的装软件、起进程。
- `POST /api/napcat/connect` 成功后会清 NapCat 状态缓存，下一轮 `/api/napcat/status` 立刻反映新地址。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/napcat/status?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/napcat/qrcode?token=<token>" -o qr.png
curl.exe -s "http://127.0.0.1:8766/api/napcat/groups?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/napcat/doctor?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/groups/suggest?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/qq/groups/status?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/napcat/install/status?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/napcat/install?token=<token>" -H "Content-Type: application/json" --data-raw '{"accept_sha256":"<sha256>"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/napcat/launch?token=<token>" -H "Content-Type: application/json" --data-raw '{"uin":"10001"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/napcat/restart?token=<token>" -H "Content-Type: application/json" --data-raw '{"uin":"10001"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/napcat/connect?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/napcat/autosetup?token=<token>" -H "Content-Type: application/json" --data-raw '{}'
```

## 9. 钉钉

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/dingtalk/status` | 需要 | — | `{ok, enabled, running, groups, state, scan_state, scan_stage, scan_started_at, scan_elapsed_seconds}` |
| `GET` | `/api/dingtalk/groups` | 需要 | — | `{ok, groups[], suggest_source, suggest_error}`；失败时 `ok:false` + `category` |
| `POST` | `/api/dingtalk/scan` | 需要 | — | `{ok, started, source, scan_state, scan_stage, scan_elapsed_seconds, category?, message?}` |

行为要点：

- `state` 四种：`未启用` / `已连接实时监听 N 个群` / `钉钉已启用，监听未启动` / `已启用，但还没勾群`（白名单为空时）。
- `POST /api/dingtalk/scan` 的 `category` 有四类：`scan_start_failed`（抛异常）、`disabled`（没启用）、`no_groups_selected`（没选群）、`scanner_unavailable`（扫描器没起来）。前三类属于「还没配好」，仍回 `ok: true`，只有 `scanner_unavailable` 才 `ok: false`（`webapp.py:3777`）。
- 幂等：扫描已在跑时回 `{ok: true, started: false, already_running: true}`，不重复起。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/dingtalk/status?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/dingtalk/groups?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/dingtalk/scan?token=<token>"
```

## 10. 微信

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/wechat/status` | 需要 | — | `{ok, enabled, running, available, groups, state, hint, manual_key, storage_dir, interval_seconds, last_read_at, last_read_seconds, last_error, error_category, error_hint, scan_state, scan_stage, scan_started_at, scan_elapsed_seconds}` |
| `GET` | `/api/wechat/groups` | 需要 | — | `{ok, groups[{id, name, alias, selected}], suggest_source, suggest_error, error?, error_category?, error_hint?}` |
| `POST` | `/api/wechat/scan` | 需要 | — | 同钉钉 `/scan` 的形状，`source: "wechat"` |

行为要点：

- **`/api/wechat/groups` 有副作用**：先让微信存储位置探测缓存失效，再真读一次群名（首次要扫微信内存，可能好几分钟），并把结果写进进程内的读取状态（`webapp.py:3254`、`:3274`）。它不是纯查询接口，别在轮询里调。
- 0 个群时一并返回 `error`、`error_category`、`error_hint`——分类由 `wechat_receiver` 出，`error_hint` 是给用户看的下一步。
- `POST /api/wechat/scan` 与 `/api/dingtalk/scan` **共用同一个处理方法**（`routes.py:119`），按路径前缀区分来源。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/wechat/status?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/wechat/groups?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/wechat/scan?token=<token>"
```

## 11. 学习通

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/chaoxing/status` | 需要 | — | `{ok, has_cookie, expired, last_sync_at, last_error, …}` |
| `GET` | `/api/chaoxing/courses` | 需要 | — | `{ok, courses}`；没填凭据 400，登录失效 502 |
| `GET` | `/api/chaoxing/cookie/status` | 需要 | — | `{ok, has_cookie, result, last_checked_at, error}` |
| `POST` | `/api/chaoxing/cookie/validate` | 需要 | — | `{ok, result, has_cookie, last_checked_at, error, cookie_sha256}` |
| `POST` | `/api/chaoxing/auto_cookie` | 需要 | — | `{ok, saved, source, courses, attempts}` 或 `{ok:false, category, error, detail, next_step, attempts}` |

行为要点：

- `/api/chaoxing/auto_cookie` 会从浏览器读登录信息并**落盘**：写 `data/chaoxing.cookie`，同时改内存里的 `chaoxing_cookie` 和 `chaoxing_enabled = True`，再重载读取器（`webapp.py:3691`）。这是写接口。
- 失败分类五种：`browser_unavailable`、`browser_locked`、`no_chaoxing_cookie`、`decrypt_failed`，白名单外的一律归 `browser_unavailable`（`webapp.py:3663`）。每种都带 `next_step` 文案。
- 失败时**状态码仍是 200**，用 `ok` 判断成败。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/chaoxing/status?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/chaoxing/courses?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/chaoxing/cookie/status?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/chaoxing/cookie/validate?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/chaoxing/auto_cookie?token=<token>"
```

## 12. 配对与公网

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/pair/info` | 需要 | — | `{ok, public_url, public_enabled, tailscale_ready, base, lan[], candidates[], qr_error, firewall, steps[], no_public_hint}` |
| `GET` | `/api/pair/pending` | 需要 | — | `{ok, pending[]}` |
| `GET` | `/api/pair/status` | **公开** | `code`（≤16 字符）、`secret`（≤128 字符） | `{ok, status}`；批准后额外带 `token`；无效回 404 |
| `POST` | `/api/pair/start` | **公开** | —（设备名/来源由服务端取） | `{ok, code, secret, …}`；同来源 10 分钟超 3 次回 429 |
| `POST` | `/api/pair/approve` | 需要 | `code`（6 位数字） | `{ok, …}`；码不对 400 |
| `POST` | `/api/pair/deny` | 需要 | `code`（6 位数字） | `{ok, …}` |
| `POST` | `/api/pair/pending/clear` | 需要 | — | `{ok, cleared}` |
| `POST` | `/api/session/exchange` | **公开** | `token` | `{ok}` + `Set-Cookie`；令牌错 401 |
| `GET` | `/api/tunnel/status` | 需要 | — | `{ok, provider, state, installed, url, auth_url, message, retryable, providers, url_changes_on_restart}` |
| `POST` | `/api/tunnel/start` | 需要 | `confirm: true`（必须）；`provider`（可选，`cloudflare`/`tailscale`，缺省取设置里选中的那条） | `{ok, provider, state, …}`；缺 confirm 回 400 |
| `POST` | `/api/pair/public` | 需要 | —（旧接口，不要求 confirm） | 同 tunnel/start |
| `GET` | `/api/app/qr.png` | 需要 | `base`（手机访问地址，必填、唯一） | PNG；参数错 400 |

行为要点：

- **配对状态机**：`POST /api/pair/start` 发 6 位码 + 32 字符 secret → 手机拿码和 secret 轮询 `/api/pair/status` → 电脑上 `approve`/`deny`。批准后 `/api/pair/status` 才把真令牌交出去（`webapp.py:2569`），这是手机拿到令牌的唯一路径。
- `POST /api/pair/start` 是无鉴权接口，所以有频控：同一来源 10 分钟内最多 3 次，超了 429（`webapp.py:3544`）。
- **`/api/tunnel/start` 必须带 `{"confirm": true}`**，否则 400 + `needs_confirm: true`（`webapp.py:3826`）。老接口 `/api/pair/public` 为兼容旧页面**不要求** confirm，但两者走同一套执行路径，审计一样记。
- **没有令牌时不许开公网**：`/api/tunnel/start` 与 `/api/pair/public` 在令牌为空时回 400（`webapp.py:3818`）。Funnel 反代后来源变成 `127.0.0.1`，正好命中「空 token 放行环回」，整站就成了无鉴权公开。
- 开通是**异步**的：接口只把后台任务起起来就返回，页面轮询 `/api/tunnel/status`（状态机含 `installed`、`auth_url`、`retryable`）。
- **两条通道**：默认 `cloudflare`（免注册，地址每次重启都变，`url_changes_on_restart: true`；要 55 MB 的 cloudflared，首次开工会下约 19 MB 的压缩包，Gitee 优先、兜底 GitHub 的 55 MB），要固定地址就切 `tailscale`（Funnel，要登录账号）。`providers` 里是两条通道各自的状态，页面用它在胶囊按钮上标「已安装/已开通」。
- 审计：`/api/tunnel/start`、`/api/firewall/allow`、`/api/onboarding/run` 都会写 `data/host-actions.log`（`noticehub/host_actions.py:28`）。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/pair/info?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/pair/pending?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/pair/status?code=123456&secret=<secret>"          # 免鉴权
curl.exe -s -X POST "http://127.0.0.1:8766/api/pair/start"                              # 免鉴权
curl.exe -s -X POST "http://127.0.0.1:8766/api/pair/approve?token=<token>" -H "Content-Type: application/json" --data-raw '{"code":"123456"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/pair/deny?token=<token>" -H "Content-Type: application/json" --data-raw '{"code":"123456"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/pair/pending/clear?token=<token>"
curl.exe -s -i -X POST "http://127.0.0.1:8766/api/session/exchange" -H "Content-Type: application/json" --data-raw '{"token":"<token>"}'
curl.exe -s "http://127.0.0.1:8766/api/tunnel/status?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/tunnel/start?token=<token>" -H "Content-Type: application/json" --data-raw '{"confirm":true,"provider":"cloudflare"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/pair/public?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/app/qr.png?base=http%3A%2F%2F192.168.1.5%3A8766&token=<token>" -o app.png
```

## 13. 设置

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/settings` | 需要 | — | 托管偏好 + 各通道开关/白名单/别名/间隔 + `chaoxing_status` + `lab_*` + `community_telemetry` |
| `POST` | `/api/settings` | 需要 | 见下方「按组单独保存」 | `{ok, …}`；分组混在一起回 400 |
| `POST` | `/api/settings/test-llm` | 需要 | —（用当前配置） | `{ok, model, reply, seconds}` 或 `{ok:false, error, model, endpoint}` |
| `POST` | `/api/settings/test-push` | 需要 | —（用当前配置） | `{ok, results[]}` |
| `POST` | `/api/subscriptions` | 需要 | `groups[]`、`aliases{}` | `{ok, groups, applied}`；写 `.env` 失败回 400 |
| `GET` | `/api/firewall/status` | 需要 | — | `{ok, port, state, message, at, checked}` |
| `POST` | `/api/firewall/allow` | 需要 | `confirm: true`（必须） | `{ok, state: "pending", request_id, port}` |

`POST /api/settings` 的「按组单独保存」——一次只能带一组，混带会被拒（`noticehub/settings_api.py:278-308`）：

| 组 | 字段 |
|---|---|
| 托管 | `quit_qq`、`restore_qq`、`quit_restore_qq`、`auto_on_start`、`autostart` |
| 回溯 | `catchup_enabled`、`catchup_hours` |
| 推送通道 | `wxpusher_app_token`、`wxpusher_uids`、`wxpusher_topic_ids`、`serverchan_keys`、`pushplus_tokens`、`webhook_urls`（逐个含义见 18.2） |
| AI 接入 | `llm_provider`、`llm_api_key`、`llm_model`、`llm_endpoint` |
| 学习通 | `chaoxing_enabled`、`chaoxing_cookie`、`chaoxing_group_whitelist`、`chaoxing_clear_cookie` |
| 钉钉 | `dingtalk_enabled`、`dingtalk_group_whitelist` |
| 微信 | `wechat_enabled`、`wechat_group_whitelist`、`wechat_group_aliases`、`wechat_scan_interval_seconds`、`wechat_key`、`wechat_dir` |
| NapCat | `napcat_dir` |
| 实验室 | `lab_search`、`lab_replies`、`lab_events`、`lab_rules` |
| 社区在线数 | `community_telemetry` |
| 更新通道 | `update_channel` |

行为要点：

- 保存是**热更新**：写 `.env` 同时改进程内的 `Settings` 对象，多数设置保存即生效，不用重启（`settings_api.py:360`、`:472`、`:596`）。微信/钉钉/学习通保存后会重载各自的读取器（`reload_wechat` / `reload_dingtalk` / `reload_chaoxing`）。
- **一处例外**：学习通那一组（`chaoxing_enabled`、`chaoxing_group_whitelist`）保存后只改内存和 `data/chaoxing.cookie`，`_save_chaoxing_settings`（`settings_api.py:554-586`）里没有 `update_env_file` 调用 —— 当前进程里立刻生效，但重启会回到 `.env` 里的旧值。
- **`POST /api/firewall/allow` 不在服务进程里弹 UAC**：它写一个请求文件交给托盘执行，自己的 `state` 是 `pending`，页面轮询 `/api/firewall/status` 看结果（`webapp.py:3846`）。托盘没在跑时状态会落到 `unavailable`（「需要托盘程序在运行才能提权」）。
- `/api/firewall/status` 的 `checked` 表示「本机到底读没读到防火墙规则清单」——读不到时不能把「没查成」当成「没放行」。
- 测试/只读模式下，改本机网络设置的接口一律 403：「当前是测试/只读模式，改本机网络设置的操作已禁用。」

```powershell
curl.exe -s "http://127.0.0.1:8766/api/settings?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/settings?token=<token>" -H "Content-Type: application/json" --data-raw '{"dingtalk_enabled":true,"dingtalk_group_whitelist":["cid123"]}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/settings/test-llm?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/settings/test-push?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/subscriptions?token=<token>" -H "Content-Type: application/json" --data-raw '{"groups":["123456"],"aliases":{"123456":"学院通知群"}}'
curl.exe -s "http://127.0.0.1:8766/api/firewall/status?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/firewall/allow?token=<token>" -H "Content-Type: application/json" --data-raw '{"confirm":true}'
```

## 14. 托管与首次向导

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/hosting/status` | 需要 | — | `{ok, user_qq_path, user_qq_pids[], user_qq_running, napcat_running, napcat_online, napcat_login_error, hosting_active, hosting_uin, hosting_since, groups_selected, error}` |
| `POST` | `/api/hosting/start` | 需要 | `uin`（可选） | `{ok, …}` |
| `POST` | `/api/hosting/stop` | 需要 | — | `{ok, …}` |
| `GET` | `/api/onboarding` | **公开** | — | `{required, accepted_at}` |
| `GET` | `/api/onboarding/next` | **公开** | `skip`（可重复） | `{ok, step, index, total, title, body, action, action_label, state, blocked, reason, done, skipped, skippable, detail}` |
| `GET` | `/api/onboarding/status` | **公开** | — | `{ok, state, granted, pending, ready, failed, message, action, error, unavailable}` |
| `POST` | `/api/onboarding/accept` | **公开** | —（空体也行） | `{ok, accepted_at}` |
| `POST` | `/api/onboarding/run` | 需要 | 由向导当前步骤决定 | `{ok, state, action, …}`；测试/只读模式 403 |

行为要点：

- `/api/hosting/start` 与 `/api/hosting/stop` **共用同一个处理方法**（`routes.py:85-86`），按路径结尾是否 `/start` 分流（`webapp.py:4101`）。空请求体按 `{}` 处理（`Content-Length: 0` 也收），但发坏 JSON 回 `400 bad json`。
- `hosting_status` 里 `napcat_running`（端口开着）与 `napcat_online`（真的登录了）是两件事——6099 端口用户开日常 QQ 也会监听，不能据此判 NapCat 在跑（`noticehub/hosting.py:396`）。
- `/api/onboarding/accept` 是幂等的：已接受过就直接回原来的时间戳，不覆盖（`webapp.py:3533`）。
- `/api/onboarding/run` 会写审计（`onboarding.<action>`）。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/hosting/status?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/hosting/start?token=<token>" -H "Content-Type: application/json" --data-raw '{"uin":"10001"}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/hosting/stop?token=<token>" -H "Content-Type: application/json" --data-raw '{}'
curl.exe -s "http://127.0.0.1:8766/api/onboarding"                                        # 免鉴权
curl.exe -s "http://127.0.0.1:8766/api/onboarding/next?skip=push"                          # 免鉴权
curl.exe -s "http://127.0.0.1:8766/api/onboarding/status"                                  # 免鉴权
curl.exe -s -X POST "http://127.0.0.1:8766/api/onboarding/accept" -H "Content-Type: application/json" --data-raw '{}'
curl.exe -s -X POST "http://127.0.0.1:8766/api/onboarding/run?token=<token>" -H "Content-Type: application/json" --data-raw '{}'
```

## 15. 更新与备份

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/api/update` | 需要 | `check=1\|true\|yes`（强制真检查） | `{ok, current{version,tag}, latest, has_update, channel, checking, checked_at, error, notice, download, applying, pending}` |
| `POST` | `/api/update/download` | 需要 | — | 后台下载开始的结果 |
| `POST` | `/api/update/apply` | 需要 | — | 应用更新（会重启服务） |
| `GET` | `/api/backup` | 需要 | — | `application/zip` 附件；库不存在 404 `database_missing` |

行为要点：

- 不带 `check` 时读缓存，超过 `UPDATE_CHECK_SECONDS` 才在后台起一个检查线程，响应里 `checking: true` 表示「还在查」（`updates_api.py:522`）。带 `check=1` 是**同步**等结果，最多 8 秒。
- `/api/backup` 用 SQLite 在线备份 + 打包 `data/digest.sqlite3` 成 zip，不是直接拷文件（`webapp.py:2666`）。所以备份期间写入不会产生半个库。
- `/api/update/apply` 真的会换程序文件并重启——**别在别人正用的时候调**。

```powershell
curl.exe -s "http://127.0.0.1:8766/api/update?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/update?check=1&token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/update/download?token=<token>"
curl.exe -s -X POST "http://127.0.0.1:8766/api/update/apply?token=<token>"
curl.exe -s "http://127.0.0.1:8766/api/backup?token=<token>" -o backup.zip
```

## 16. 页面与静态资源

| 方法 | 路径 | 鉴权 | 参数 | 返回 |
|---|---|---|---|---|
| `GET` | `/` | 需要 | — | 主页面 HTML（注入版本 badge、渠道色） |
| `GET` | `/index.html` | 需要 | — | 同上 |
| `GET` | `/setup` | 需要 | — | 302 回首页 |
| `GET` | `/floatball` | 需要 | — | 浮球页 HTML |
| `GET` | `/floatball.html` | 需要 | — | 同上 |
| `GET` | `/icon.svg` | **公开** | — | SVG 图标（缓存 1 天） |
| `GET` | `/manifest.webmanifest` | **公开** | — | PWA 清单 |
| `GET` | `/sw.js` | **公开** | — | Service Worker 脚本 |
| `GET` | `/js/*` | **公开** | — | `web_static/js/` 下的脚本，只允许该目录下 `.js` |
| `GET` | `/api/donate/qr.png` | **公开** | — | 赞赏码 JPEG；没有文件回 404 |

行为要点：

- `page.html` 里的脚本已拆到 `web_static/js/*.js`，由 `/js/*` 提供。打包时 `packaging/qq_live_digest.spec` 的 `datas` 必须包含整个 `web_static/js` 目录，漏了就是装完 404、公告和配对静默失效。
- `/js/*` 做了目录逃逸防护：解析后父目录必须正好是 js_root，且后缀必须是 `.js`（`webapp.py:3457`）。

```powershell
curl.exe -s "http://127.0.0.1:8766/?token=<token>" -o page.html
curl.exe -s -o NUL -w "%{http_code}`n" "http://127.0.0.1:8766/index.html?token=<token>"
curl.exe -s -o NUL -w "%{http_code}`n" "http://127.0.0.1:8766/setup?token=<token>"
curl.exe -s "http://127.0.0.1:8766/floatball?token=<token>"
curl.exe -s "http://127.0.0.1:8766/floatball.html?token=<token>"
curl.exe -s "http://127.0.0.1:8766/icon.svg"
curl.exe -s "http://127.0.0.1:8766/manifest.webmanifest"
curl.exe -s "http://127.0.0.1:8766/sw.js"
curl.exe -s "http://127.0.0.1:8766/js/announce.js"
curl.exe -s "http://127.0.0.1:8766/api/donate/qr.png" -o donate.jpg
```

## 17. 命令行

入口 `main.py`。子命令写在 `main.py:30` 的 `_COMMANDS` 表里（每项是「名字 / 帮助 / 处理函数 / 参数表」），`build_parser()`（`main.py:139`）按这张表拼出 argparse，所以表就是唯一的命令行契约。

| 命令 | 参数 | 作用 |
|---|---|---|
| `--env <路径>` | 默认空（用项目根 `.env`） | **顶层参数，必须写在子命令前面**。指定 `.env` 文件 |
| `run` | `--poll <秒>`（默认 0 = 读配置） | 常驻运行（定时拉取、生成摘要、推送） |
| `tick` | `--no-bot` | 只执行一次调度；`--no-bot` 时不启动机器人，只处理库里已有消息 |
| `preview` | `--limit <N>`（默认 0） | 预览待处理消息会被推成什么 |
| `stats` | — | 查看统计 |
| `show` | `--limit <N>`（默认 5） | 回看最近几条摘要及判断依据 |
| `tasks` | `--all`、`--limit <N>`（默认 30）、`--import-existing`、`--days <N>`（默认 7）、`--reset` | 查看待办清单和手机访问地址；`--import-existing` 从历史摘要回填，`--reset` 先清库再回填 |
| `catchup` | — | 从 NapCat 补采最近的历史群消息 |
| `doctor` | `--online`、`--bundle [路径]` | 自检；`--online` 在线校验机器人凭证，`--bundle` 生成诊断包 zip（默认放桌面） |
| `send-test` | `--wait <秒>`（默认 25） | 发一条测试推送，并等机器人就绪 |
| `attach-test` | `[path]`、`--group`、`--file-id`、`--busid`、`--name` | 解析群文件/图片，只打印摘要不推送 |

```powershell
F:\notice-hub\qq-live-digest.exe --env F:\notice-hub\.env run
```

**`--env` 的位置是硬要求**：它是顶层参数，写在子命令后面会报 `unrecognized arguments: --env ...`（`packaging/launcher.py:673` 有原文注释）。

打包版另有 `QQ-Notice-Hub.exe --quit`：让这份安装里正跑着的托盘退出，等价于右键托盘→「退出」，返回 0 表示确认已退出（`packaging/launcher.py:248`）。它不属于 `main.py` 的子命令。

## 18. 配置项

**共 108 个环境变量**：103 个 `NOTICEHUB_*` + 5 个不带前缀的推送通道键（第三方名字，见 18.1）。

103 个 `NOTICEHUB_*` 里，98 个在 `noticehub/bootstrap/settings.py` 的 `env_value(...)` 里读，5 个在别处按需读（`NOTICEHUB_NAPCAT_DIR`、`NOTICEHUB_QQ_EXE`、`NOTICEHUB_HOST_ACTIONS`、`NOTICEHUB_UPDATE_MIRROR`、`NOTICEHUB_CHAOXING_BASE`）。

「改完要不要重启」这一列：

- **要重启**：只在启动时读一次、或决定监听/绑定/线程启动的项。
- **保存即生效**：设置页保存会同时写 `.env` 并更新进程内 `Settings`，下一轮就用新值。
- **下次启动生效**：写进 `.env` 但当前进程没有热更新路径的项。

| 变量 | 类型 | 默认值 | 作用 | 改了要不要重启 |
|---|---|---|---|---|
| `NOTICEHUB_ENV` | 字符串 | `<运行目录>\.env` | `.env` 文件路径 | 要重启 |
| `NOTICEHUB_DATA_DIR` | 字符串 | `<运行目录>\data` | 数据目录（库、附件、二维码） | 要重启 |
| `NOTICEHUB_LOG_DIR` | 字符串 | `<运行目录>\logs` | 日志目录 | 要重启 |
| `NOTICEHUB_GROUPS` | 列表 | 空 | **必填**：要监控的群号；空=不处理任何群（fail-closed） | 保存即生效 |
| `NOTICEHUB_GROUP_ALIASES` | 映射 | 空 | 群号→中文名 | 保存即生效 |
| `NOTICEHUB_QUIET_GROUPS` | 列表 | 空 | 低优先级群：不触发紧急立即推，不入摘要/待办 | 保存即生效 |
| `NOTICEHUB_IMMEDIATE_GROUPS` | 列表 | 空 | 即时处理群：不等合并窗口 | 保存即生效 |
| `NOTICEHUB_OFFICIAL_BOT_ENABLED` | bool | `1` | 官方 QQ 机器人开关（与 NapCat 链路二选一） | 要重启 |
| `NOTICEHUB_PUSH_OPENIDS` | 列表 | 空 | 官方机器人私聊推送目标 openid | 保存即生效 |
| `NOTICEHUB_WEBHOOKS` | 列表 | 空 | 通用 Webhook（POST JSON: title/content/source） | 保存即生效 |
| `NOTICEHUB_ONEBOT_ENABLED` | bool | `0` | 启用 OneBot HTTP 上报接收 | 要重启 |
| `NOTICEHUB_ONEBOT_HOST` | 字符串 | `127.0.0.1` | 接收端绑定地址 | 要重启 |
| `NOTICEHUB_ONEBOT_PORT` | int | `8765` | 接收端端口 | 要重启 |
| `NOTICEHUB_ONEBOT_TOKEN` | 字符串 | 空 | 接收端令牌；空时非环回来源的消息会被拒收 | 要重启 |
| `NOTICEHUB_NAPCAT_API_URL` | 字符串 | `http://127.0.0.1:3001` | NapCat HTTP API 地址 | 保存即生效 |
| `NOTICEHUB_NAPCAT_API_TOKEN` | 字符串 | 空（回退 `ONEBOT_TOKEN`） | NapCat API 令牌 | 保存即生效 |
| `NOTICEHUB_NAPCAT_QR_PATH` | 字符串 | `<data>\qrcode.png` | 当前登录二维码文件路径 | 保存即生效 |
| `NOTICEHUB_NAPCAT_DIR` | 字符串 | 空 | NapCat 安装目录（自动发现失败时手填） | 保存即生效 |
| `NOTICEHUB_QQ_EXE` | 字符串 | 空 | QQ 可执行文件路径（自动发现失败时手填） | 保存即生效 |
| `NOTICEHUB_HTTP_TIMEOUT` | int | `15` | 调 NapCat/学习通等外部接口超时（秒） | 保存即生效 |
| `NOTICEHUB_QQ_GROUP_REFRESH_INTERVAL_SECONDS` | int | `300` | 群列表刷新间隔（秒） | 保存即生效 |
| `NOTICEHUB_CATCHUP_ENABLED` | bool | `0` | 自动历史补采 | 保存即生效 |
| `NOTICEHUB_CATCHUP_HOURS` | int | `24` | 补采回溯多少小时 | 保存即生效 |
| `NOTICEHUB_CATCHUP_COUNT` | int | `50` | 每轮补采条数上限 | 保存即生效 |
| `NOTICEHUB_CATCHUP_INTERVAL_MINUTES` | int | `30` | 补采间隔（分钟） | 保存即生效 |
| `NOTICEHUB_DINGTALK_ENABLED` | bool | `0` | 启用钉钉接入 | 保存即生效 |
| `NOTICEHUB_DINGTALK_GROUP_WHITELIST` | 列表 | 空 | 要监听的钉钉群 | 保存即生效 |
| `NOTICEHUB_DINGTALK_SCAN_INTERVAL_SECONDS` | int | `30` | 钉钉扫描间隔（最小 5） | 保存即生效 |
| `NOTICEHUB_WECHAT_ENABLED` | bool | `0` | 启用微信接入 | 保存即生效 |
| `NOTICEHUB_WECHAT_GROUP_WHITELIST` | 列表 | 空 | 要读的微信群 | 保存即生效 |
| `NOTICEHUB_WECHAT_GROUP_ALIASES` | 映射 | 空 | 微信群 id→中文名 | 保存即生效 |
| `NOTICEHUB_WECHAT_SCAN_INTERVAL_SECONDS` | int | `300` | 微信扫描间隔（最小 30） | 保存即生效 |
| `NOTICEHUB_WECHAT_KEY` | 字符串 | 空 | 手填的微信数据库 raw key（64 位十六进制） | 保存即生效 |
| `NOTICEHUB_WECHAT_DIR` | 字符串 | 空 | 微信文件目录（改过存储位置时手填） | 保存即生效 |
| `NOTICEHUB_CHAOXING_ENABLED` | bool | `0` | 启用学习通同步 | 保存即生效 |
| `NOTICEHUB_CHAOXING_COOKIE` | 字符串 | 空（回退 `data/chaoxing.cookie`） | 学习通登录信息 | 保存即生效 |
| `NOTICEHUB_CHAOXING_GROUP_WHITELIST` | 列表 | 空 | 学习通课程/群白名单 | 保存即生效 |
| `NOTICEHUB_CHAOXING_SCAN_INTERVAL_SECONDS` | int | `1800` | 学习通同步间隔（最小 1800） | 保存即生效 |
| `NOTICEHUB_CHAOXING_BASE` | 字符串 | 不设（用内置主机名） | 覆盖学习通接口基地址；设成 `off`/空串之外的值必须带 `http(s)://` 且非空，否则启动时报错 | 要重启 |
| `NOTICEHUB_LLM` | bool | `1` | 启用 AI 精炼 | 保存即生效 |
| `NOTICEHUB_LLM_BACKEND` | 字符串 | `auto` | 提取后端：`auto` / `ollama` / `openai` / `codebuddy` | 保存即生效 |
| `NOTICEHUB_LLM_API_KEY` | 字符串 | 空（兼容 `DASHSCOPE_API_KEY`） | OpenAI 兼容厂商的 key | 保存即生效 |
| `NOTICEHUB_LLM_ENDPOINT` | 字符串 | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` | 精炼接口地址 | 保存即生效 |
| `NOTICEHUB_LLM_MODEL` | 字符串 | `qwen-plus` | 精炼模型 | 保存即生效 |
| `NOTICEHUB_CODEBUDDY_CLI` | 字符串 | 空 | CodeBuddy/WorkBuddy CLI 路径 | 保存即生效 |
| `NOTICEHUB_LLM_TIMEOUT` | int | `60` | 单次精炼超时（最小 10） | 保存即生效 |
| `NOTICEHUB_LLM_MAX_RETRIES` | int | `2` | 失败原地重试次数 | 保存即生效 |
| `NOTICEHUB_LLM_RETRY_BACKOFF` | float | `1.5` | 重试退避倍数 | 保存即生效 |
| `NOTICEHUB_LLM_DEFER_MAX_ATTEMPTS` | int | `3` | 可恢复失败最多推迟几次 | 保存即生效 |
| `NOTICEHUB_LLM_DEFER_WINDOW_MINUTES` | int | `15` | 推迟窗口（分钟，最小 1） | 保存即生效 |
| `NOTICEHUB_VISION` | bool | `1` | 是否读图 | 保存即生效 |
| `NOTICEHUB_VL_MODEL` | 字符串 | `qwen3-vl-plus` | 视觉模型名（换端点后必须改成该端点自己的模型） | 保存即生效 |
| `NOTICEHUB_INCLUDE_RAW` | bool | `0` | 推送里是否附原文 | 保存即生效 |
| `NOTICEHUB_ATTACHMENTS` | bool | `1` | 是否解析群文件/群图片 | 保存即生效 |
| `NOTICEHUB_ATTACHMENT_MAX_MB` | int | `10` | 单个附件大小上限（MB） | 保存即生效 |
| `NOTICEHUB_ATTACHMENT_MAX_PER_HOUR` | int | `30` | 每小时最多处理几个附件 | 保存即生效 |
| `NOTICEHUB_ATTACHMENT_RETENTION_DAYS` | int | `7` | 附件保留天数 | 保存即生效 |
| `NOTICEHUB_DOCUMENT_MAX_CHARS` | int | `30000` | 文档提取字符上限（最小 1000） | 保存即生效 |
| `NOTICEHUB_PDF_OCR_MAX_PAGES` | int | `20` | PDF OCR 最多页数 | 保存即生效 |
| `NOTICEHUB_DEDUPE_HOURS` | int | `6` | 同一条通知多少小时内不重复推，0=关闭 | 保存即生效 |
| `NOTICEHUB_PUSH_HTML` | bool | `1` | 推送卡片用 HTML 排版 | 保存即生效 |
| `NOTICEHUB_PUSH_DAILY_BUDGET` | int | `12` | 每日非紧急推送预算，0=不限 | 保存即生效 |
| `NOTICEHUB_QUIET_HOURS` | 时段 | `23:00-07:00` | 夜间静默（紧急事项仍放行），留空=关闭 | 保存即生效 |
| `NOTICEHUB_DEADLINE_REMINDERS` | bool | `1` | 截止提醒开关 | 保存即生效 |
| `NOTICEHUB_DEADLINE_MORNING` | 时间 | `07:30` | 早上提醒时刻 | 保存即生效 |
| `NOTICEHUB_DEADLINE_EVENING` | 时间 | `21:00` | 晚上提前看明天到期的时刻 | 保存即生效 |
| `NOTICEHUB_CANDIDATE_PUSH` | bool | `1` | 候选确认卡开关 | 保存即生效 |
| `NOTICEHUB_CANDIDATE_MIN_CONFIDENCE` | float | `0.55` | 候选最低置信度 | 保存即生效 |
| `NOTICEHUB_CANDIDATE_REMINDER_HOURS` | int | `24` | 候选提醒间隔（最小 6） | 保存即生效 |
| `NOTICEHUB_CANDIDATE_MAX_PER_TICK` | int | `2` | 每轮最多推几个候选 | 保存即生效 |
| `NOTICEHUB_CANDIDATE_MAX_PER_DAY` | int | `4` | 每天最多推几个候选 | 保存即生效 |
| `NOTICEHUB_WEEKLY_REVIEW` | bool | `1` | 每周复盘开关 | 保存即生效 |
| `NOTICEHUB_WEEKLY_REVIEW_WEEKDAY` | int | `6` | 复盘日（0=周一，6=周日） | 保存即生效 |
| `NOTICEHUB_WEEKLY_REVIEW_TIME` | 时间 | `20:30` | 复盘时刻 | 保存即生效 |
| `NOTICEHUB_WINDOW_MINUTES` | int | `10` | 普通通知合并窗口（分钟） | 保存即生效 |
| `NOTICEHUB_POLL_SECONDS` | int | `20` | 轮询间隔（秒，最小 5） | 保存即生效 |
| `NOTICEHUB_URGENT_IMMEDIATE` | bool | `1` | 紧急通知立即推送 | 保存即生效 |
| `NOTICEHUB_MIN_SCORE` | int | `3` | 分数阈值，越高越只推重要通知 | 保存即生效 |
| `NOTICEHUB_MAX_ITEMS` | int | `30` | 单条摘要最多几件事 | 保存即生效 |
| `NOTICEHUB_MAX_BATCH` | int | `400` | 单批最多处理多少条消息（最小 50） | 保存即生效 |
| `NOTICEHUB_MAX_ATTEMPTS` | int | `4` | 投递失败重试次数 | 保存即生效 |
| `NOTICEHUB_RETRY_SECONDS` | int | `90` | 投递重试间隔（秒，最小 10） | 保存即生效 |
| `NOTICEHUB_RETENTION_DAYS` | int | `30` | 消息保留天数 | 保存即生效 |
| `NOTICEHUB_WEB` | bool | `1` | 启用网页/待办台 | 要重启 |
| `NOTICEHUB_WEB_HOST` | 字符串 | `0.0.0.0`（Windows）/ `127.0.0.1` | 网页绑定地址 | 要重启 |
| `NOTICEHUB_WEB_PORT` | int | `8766` | 网页端口 | 要重启 |
| `NOTICEHUB_WEB_TOKEN` | 字符串 | 空（非环回绑定时自动生成） | 网页访问令牌 | 要重启 |
| `NOTICEHUB_WEB_BASE_URL` | 字符串 | 空 | 固定外部访问地址（如 Tailscale Serve 的 HTTPS 地址） | 保存即生效 |
| `NOTICEHUB_HOSTING_QUIT_QQ` | bool | `1` | 开始托管时先关电脑版 QQ | 保存即生效 |
| `NOTICEHUB_HOSTING_RESTORE_QQ` | bool | `1` | 结束托管时把 QQ 开回来 | 保存即生效 |
| `NOTICEHUB_QUIT_RESTORE_QQ` | bool | `0` | 退出程序时是否把 QQ 开回来 | 保存即生效 |
| `NOTICEHUB_HOSTING_AUTO_ON_START` | bool | `0` | 程序一启动就自动开始托管 | 要重启 |
| `NOTICEHUB_AUTOSTART` | bool | `0` | 开机自动启动（只写当前用户启动项） | 保存即生效 |
| `NOTICEHUB_HOST_ACTIONS` | 字符串 | 空（=允许） | 设成 `off`/`0`/`false`/`no` 时禁用所有「会改本机网络/系统」的写操作（403）；另有 `NH_TEST_MODE` 同效 | 要重启 |
| `NOTICEHUB_LAB_SEARCH` | bool | `0` | 实验室：历史搜索 | 保存即生效 |
| `NOTICEHUB_LAB_REPLIES` | bool | `0` | 实验室：回复建议 | 保存即生效 |
| `NOTICEHUB_LAB_EVENTS` | bool | `0` | 实验室：事件抽取 | 保存即生效 |
| `NOTICEHUB_LAB_RULES` | bool | `0` | 实验室：自定义规则 | 保存即生效 |
| `NOTICEHUB_COMMUNITY_TELEMETRY` | bool | `0` | 社区在线数上报 | 保存即生效 |
| `NOTICEHUB_UPDATE_CHANNEL` | 字符串 | 空（按 `APP_CHANNEL`） | 更新通道：`stable` / `beta`；其它值当未设置 | 保存即生效 |
| `NOTICEHUB_UPDATE_MIRROR` | 字符串 | 空 | 更新镜像仓库，格式 `owner/repo`（另有旧名 `QQ_DIGEST_UPDATE_MIRROR`） | 保存即生效 |
| `NOTICEHUB_SELF_QQ` | 字符串 | 空 | 自己的 QQ 号（判断是否 @ 到自己） | 保存即生效 |
| `NOTICEHUB_SELF_NICKNAME` | 字符串 | 空 | 自己的昵称（同上） | 保存即生效 |

### 18.1 不带 `NOTICEHUB_` 前缀的变量

这些是第三方通道自己的名字，**故意不加前缀**（加了就没人认得了）：

| 变量 | 作用 |
|---|---|
| `QQ_BOT_APPID` / `QQ_BOT_SECRET` / `QQ_BOT_SANDBOX` | 官方 QQ 机器人凭据 |
| `WXPUSHER_APP_TOKEN` / `WXPUSHER_UIDS` / `WXPUSHER_TOPIC_IDS` | WxPusher 推送（逐个见 18.2） |
| `SERVERCHAN_KEYS` | Server酱推送（见 18.2） |
| `PUSHPLUS_TOKENS` | PushPlus 推送（见 18.2） |
| `DASHSCOPE_API_KEY` | 旧版 LLM key（`NOTICEHUB_LLM_API_KEY` 为空时回退） |

### 18.2 推送通道的六个键（设置页可改）

设置页「提醒怎么送到手机」保存的就是这一组。代码里的常量叫 `_PUSH_SETTING_ENV_KEYS`（`noticehub/settings_api.py:61`），六项一一对应：

| 环境变量 | 请求体字段 | 类型 | 默认值 | 含义 | 网页可改 |
|---|---|---|---|---|---|
| `WXPUSHER_APP_TOKEN` | `wxpusher_app_token` | 字符串（**只能一个**） | 空 | WxPusher 应用 token，微信接收 | ✅ 设置 →「提醒怎么送到手机」 |
| `WXPUSHER_UIDS` | `wxpusher_uids` | 列表（逗号分隔） | 空 | WxPusher 关注者 UID，指定发给哪些人 | ✅ 同上 |
| `WXPUSHER_TOPIC_IDS` | `wxpusher_topic_ids` | 整数列表 | 空 | WxPusher 主题 ID，**只能是正整数** | ✅ 同上 |
| `SERVERCHAN_KEYS` | `serverchan_keys` | 列表 | 空 | Server酱 SendKey，微信接收 | ✅ 同上 |
| `PUSHPLUS_TOKENS` | `pushplus_tokens` | 列表 | 空 | PushPlus token | ✅ 同上 |
| `NOTICEHUB_WEBHOOKS` | `webhook_urls` | 列表 | 空 | 通用回调地址，POST JSON（`title` / `content` / `source`） | ✅ 同上 |

规则（`settings_api.py:94`）：

- 这六个一起构成「推送通道」这一组。请求体里带其中任意一个，就按这一组处理；和别的组混着提交会被拒（400「推送通道设置请单独保存」）。
- **任何一项非法整体拒绝，不做部分写入**。校验规则：App Token 不能填多个（逗号/空格分隔会被当成两个）；Topic ID 只能是正整数；Webhook 必须以 `http://` 或 `https://` 开头。
- 保存后写 `.env` 并热更新内存里的 `Settings`，不用重启。
- 页面回显是脱敏的：只给「配了几个」和每项尾 4 位（`_mask_push_secret`），不回明文。
- 五个第三方键不带 `NOTICEHUB_` 前缀，只有 `NOTICEHUB_WEBHOOKS` 带；第三方名改了就对不上它们的文档。

### 18.3 旧名兼容（`QQ_DIGEST_*`）

规则只有一条：**`NOTICEHUB_<X>` 优先，没有才回退 `QQ_DIGEST_<X>`**。两个都写时按新名走，哪怕新名是空串也算用户显式选择（`noticehub/bootstrap/settings.py:26`）。

首次读到旧名会记一条 warning：`检测到旧环境变量名 QQ_DIGEST_*，已按兼容读取；建议改成 NOTICEHUB_*`（只警告一次）。

所以 `QQ_DIGEST_WEB_PORT`、`QQ_DIGEST_GROUPS`、`QQ_DIGEST_WEB_TOKEN`… 这些旧名全部仍然可用，名字与上表一一对应（去掉前缀换成 `QQ_DIGEST_`）。**改配置时别两套都写**：`.env` 里同时留着 `QQ_DIGEST_X` 和 `NOTICEHUB_X` 时，用户改旧行不会生效。

## 19. 自检命令

文档里的路由条数与配置项数都能复算，发版前跑一遍：

```powershell
# 路由条数：期望 62 41 1 1
.\.venv\Scripts\python.exe -X utf8 -c "import noticehub.routes as r; print(len(r.GET_ROUTES), len(r.POST_ROUTES), len(r.GET_PATTERN_ROUTES), len(r.POST_PATTERN_ROUTES))"

# NOTICEHUB_ 变量数：期望 98
.\.venv\Scripts\python.exe -X utf8 -c "import ast,io;t=ast.parse(io.open('noticehub/bootstrap/settings.py',encoding='utf-8').read());print(len({n.args[1].value for n in ast.walk(t) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='env_value' and len(n.args)>1 and isinstance(n.args[1],ast.Constant)}))"
```

第二个命令数出来是 98；剩下 5 个（`NAPCAT_DIR`、`QQ_EXE`、`HOST_ACTIONS`、`UPDATE_MIRROR`、`CHAOXING_BASE`）不在 `settings.py` 里，按需在各自模块读（98 + 5 = 103 个 `NOTICEHUB_*`）：

```powershell
Select-String -Path noticehub\*.py -Pattern 'NOTICEHUB_(NAPCAT_DIR|QQ_EXE|HOST_ACTIONS|UPDATE_MIRROR|CHAOXING_BASE)' -Encoding UTF8 |
  ForEach-Object { $_.Line.Trim() } | Sort-Object -Unique
```

再数不带前缀的推送键（期望 5 个）：103 + 5 = **108**：

```powershell
# 打印推送通道的 6 个键（其中 5 个不带 NOTICEHUB_ 前缀）
.\.venv\Scripts\python.exe -X utf8 -c "from noticehub.settings_api import _PUSH_SETTING_ENV_KEYS as k; [print(v) for v in k.values()]"
```

```
WXPUSHER_APP_TOKEN
WXPUSHER_UIDS
WXPUSHER_TOPIC_IDS
SERVERCHAN_KEYS
PUSHPLUS_TOKENS
NOTICEHUB_WEBHOOKS
```

