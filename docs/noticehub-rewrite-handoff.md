# 交接：noticehub 重写（给接手方 agent）

你在 `F:\qq-notice-hub` 的 `rewrite/clean-room` 分支上工作。你的任务是完成 G2/G3/G4：把 `qq_live_digest` 重写为独立包 `noticehub`，切换 Web 调用点，最后一次性改名。**行为不变**是唯一硬约束——HTTP 路由、状态码、JSON 键、数据文件、`.env` 键、托盘与打包入口，对用户完全一致。

**完成判据**：`rewrite/clean-room` 上跑 `.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t .` 得到 `OK`（skip 不算通过），且第 6 节的契约对账全过。达不到就不算交付。

---

## 1. 开工前先自查三件事

```
cd F:\qq-notice-hub
git rev-parse --abbrev-ref HEAD      # 必须是 rewrite/clean-room
git rev-parse --short HEAD           # 基线 a3b8deb
git status --porcelain               # 必须为空
```

任何一项不符，先停下说明，别改文件。

**已知事实**：`a3b8deb` 的提交说明写着「G2 已写完可以接 Web」，但 `noticehub/application/` 里只有 `ingest.py`、`schedule.py`、`history.py`、`attachments.py`；`04-application.md` 的核心 18 条用例**一条都不存在**（见第 5 节的现状列）。G3 从「写用例」开始，不是从「接线」开始。

`a3b8deb` 上曾有一次未提交的 G3 试做（`application/tasks.py`、`application/calendar.py`、`domain/calendar.py` 与 `webapp.py` 的接线改动），已由我方**全部回滚**、从未进入任何提交——不是丢失，你不用找。

---

## 2. 硬边界

**你改**：`noticehub/**`、`docs/rewrite/**`、`tests/**`（新增测试文件）；G3 阶段还要改 `qq_live_digest/webapp.py` 的接线。

**你不改**（主线负责，改了会在合并时冲突）：
- `qq_live_digest/version.py`（版本号只此一处）
- `main.py`、`packaging/**`、`launcher.py`、CI 配置 —— 这些是 G4 那次提交里主线改的
- `data/**`、`.env*`、`logs/**`
- 用户实例 `F:\notice-hub`（8766/8765，正在被真实使用）：不停、不重启、不连

**测试环境**：只能用 `F:\nh-clean-test`（8767）或 `%TEMP%` + 随机空闲端口。起临时实例**必须显式传端口**，不依赖默认值（有实例因 `.env` 缺端口落到 8766 撞上用户实例的记录）。

---

## 3. 现状基线

| 项 | 值 |
|---|---|
| 仓库 | `F:\qq-notice-hub` |
| 你的分支 | `rewrite/clean-room` @ `a3b8deb`，工作区干净 |
| 主线 | `main` @ `bd489f5`（beta.15，32 文件 / +2324−197，已过 1623 项门禁与独立验收） |
| 版本 | `qq_live_digest/version.py:12` `APP_VERSION = "1.1.0-beta.15"`、`:13` `APP_CHANNEL = "beta"` |
| 全量测试 | 1623 项 / 约 280 秒 |
| 契约文档 | `docs/rewrite/03-ports.md`（端口）、`04-application.md`（用例）、`05-config.md`、`06-migration.md`（阶段）、`07-risks.md` |
| 路由基线 | `qq_live_digest/routes.py`：`GET_ROUTES` 61 条、`POST_ROUTES` 39 条、前缀模式各 1 条（共 102 条） |

---

## 4. 阶段步骤

### G2 — 新架构与旧代码并存

1. 按 `02-module-map.md` 建齐 `noticehub/` 各层：`domain/`、`ports/`、`application/`、`infrastructure/`、`adapters/`、`bootstrap/`。
2. 端口实现：迁移期允许直接用旧 `qq_live_digest.store.Store` 做适配器（`06-migration.md` 明确允许），但 `application` 与 `webapp` 都不许 import SQLite。
3. 装配点：`bootstrap` 负责例注入依赖；`service.py` 不许 import `webapp`。

**G2 完成判据**：`noticehub/` 全部模块可 import；旧服务功能全部仍可用（全量测试绿）；`docs/handover/11-g2-two-env-gate.md` 的「无软依赖 / 全依赖」两环境各跑一遍通过，skip 不算通过。

### G3 — 切换调用点（18 条用例）

1. 按第 5 节实现 18 条用例。签名以 `04-application.md` 为准，**每一处与现有实现不一致的地方以契约为准，并在提交说明里写明为什么**。
2. 改 `qq_live_digest/webapp.py` 的对应 handler：handler 只保留 HTTP 边界（鉴权、body 大小、状态码、错误文案映射），业务逻辑全部进用例。
3. 旧实现只在调用点全部切走、全量测试仍绿之后删。

**G3 完成判据**：18 条用例全部可调用；对应 18 条路由的响应与基线逐字一致（见第 6 节对账方法）；全量测试绿。

### G4 — 改名（一次提交）

1. 包名 `noticehub`；`qq_live_digest/` 里已迁走的模块删除或改为从 `noticehub` 再导出。
2. 交给主线改：`main.py`、`packaging/*.spec`、`launcher.py`、CI 的 import。**你不要碰这些文件**，在提交说明里列出「需要主线同步修改的文件清单」即可。

**G4 完成判据**：一次提交完成、`bisect` 不会停在 import 断裂的中间态、`APP_VERSION` 仍只有一处定义。

---

## 5. 18 条核心用例（现状：0 条存在）

| # | 用例 | 路由 | 现状 | 必须注意 |
|---|---|---|---|---|
| 1 | `list_task_board(now=None)` | `GET /api/tasks` | **不存在** | 返回键 `headline, subline, progress, stats, candidates, today, week, later, done, pinned`；分组规则同 `webapp.group_tasks` + `overview`（跳过 dismissed/expired、candidate 最多 40、done 按 `done_at` 倒序最多 20、later/today/week 各最多 40、组内 `effective_urgent` 优先、群 id 换群名） |
| 2 | `create_task(summary, deadline="")` | `POST /api/tasks/create` | **不存在** | 三段错误文案逐字：「summary 必须是文本」「请填写待办内容」「待办内容请控制在 200 字以内」「截止时间格式不对，请重新选择」「deadline 必须是文本」；`task_key = manual:<16 hex>` |
| 3 | `apply_task_action(task_id, action, done=None, until="")` | `POST /api/tasks/<id>` | **不存在** | action ∈ `confirm\dismiss\done\reopen\snooze`；缺省时 `done:true/false` 映射 done/reopen；返回 `ok, id, action, done, status` |
| 4 | `correct_task(task_id, correction, value="")` | 同一路由 `action=correct` | **不存在** | `not_notice/not_task/duplicate` → `dismissed`；`not_urgent` → `urgent` 降为 `action` 且重要度 ≤3；`category` 只认 `urgent\|action\|academic\|info`；重要度映射只有一份 `{urgent:5, action:4, academic:3, info:3}` |
| 5 | `set_task_urgent(task_id, urgent)` | `POST /api/tasks/urgent` | **仅以 store 方法存在** | `urgent=None` 表示清除覆盖，与「字段缺失」是两回事；`task_id` 必须 ASCII 数字串、1–18 位、≥1，否则 `ValueError("task_id 必须是正整数文本")`；任务不存在返回 `ok:false, error="任务不存在"`（**不是 404**） |
| 6 | `set_task_pinned(task_id, pinned)` | `POST /api/tasks/pin` | **仅以 store 方法存在** | 已完成任务拒绝置顶，文案「已完成的任务不能置顶」 |
| 7 | `task_stats()` | `GET /api/tasks`、`GET /api/meta` 共用 | **仅以 store 方法存在** | 键 `total, candidate, open, done, dismissed, expired, overdue`；两处必须同一份实现 |
| 8 | `render_calendar_ics()` | `GET /calendar.ics` | **不存在** | VCALENDAR + VTIMEZONE；鉴权留在 Web（要 token）；渲染逻辑可参考 `qq_live_digest/ics.py` |
| 9 | `describe_calendar_subscription()` | `GET /api/sync/info` | **不存在** | 键 `ok, primary, lan_base, port, events, calendars[{name,kind,url,hint,virtual}]`；`primary` 选法：有 public 用 public，否则取 virtual=false 的第一条，全 true 退回第一条；局域网排序 192.168 → 10. → Tailscale 100.64 → 其它 → 虚拟网卡最后 |
| 10 | `list_notices()` | `GET /api/notices` | **不存在** | 固定最近 8 条；每项 `id, kind, created_at, summary, body`，body 最多 600 字 |
| 11 | `start_inbox_classify(groups, since="", until="", limit=1000)` | `POST /api/inbox/classify` | **不存在** | groups 1–500；日期 `YYYY-MM-DD` 或空；limit 钳 1–20000；与历史回填共用同一任务槽 |
| 12 | `promote_inbox_item(msg_id, title="", due="")` | `POST /api/inbox/promote` | **不存在** | msg_id 必填 ≤256，title ≤500；`task_key = msg_id`；返回 `ok, task_id, error` |
| 13 | `poll_job(kind)` | `GET /api/history/fetch/status`、`GET /api/inbox/classify/status` | **不存在** | kind ∈ `history\|classify`；键 `running, stage, done, total, note, result, detail`，detail 缺省 `{}` |
| 14 | `probe_history_floor(group_id)` | `GET /api/history/floor` | **不存在** | group_id 必填 ≤128 |
| 15 | `start_history_backfill(groups, since="", until="")` | `POST /api/history/fetch` | **不存在** | groups 1–500；日期成对时 `since <= until` |
| 16 | `cancel_job()` | `POST /api/history/fetch/cancel` | **不存在** | 正在跑历史任务 → `ok:true, cancelled:true`；没有 → Web 回 **409**「当前没有正在运行的回溯任务」；槽被占 → 409 且按占用者给不同文案 |
| 17 | `save_subscriptions(groups, aliases)` | `POST /api/subscriptions` | **不存在** | 写 `.env` 的 `QQ_DIGEST_GROUPS`、`QQ_DIGEST_GROUP_ALIASES` 并热更新 Settings；**全新安装的 `.env` 可能没有这两行，必须补上而不是失败** |
| 18 | `test_push_channels()` | `POST /api/settings/test-push` | **不存在** | 对每个已配置通道发一条测试消息；返回 `ok, results[{channel, target, ok, error}]` |

**不在本次范围**：`GET /api/insights` 归主线，不要收 `correction_stats` / `correction_insights`。NapCat、微信、钉钉、学习通、配对、隧道、防火墙、向导、托管、设置九域保存、更新、公告、社区计数、自检、概览、静态页也留在现有 Web 模块。

**任务槽**：历史回填与收件箱分类共用一个进程内槽，同时只跑一个，`poll_job` 与 `cancel_job` 看同一个状态；不落库，进程重启即空（与今天的 `_JOB_STATE` 一致）。

**用例的依赖形状**：用例接收 `Clock` 与端口，不读环境变量；依赖参数在文中省略，装配由 `bootstrap` 完成。非法参数抛 `ValueError`，由 Web 转成今天的 400 文案；缺资源返回 `ok:false`，不许把 `ValueError` 捕获后伪装成功。

---

## 6. 不可变契约（我们的验收基线）

1. **路由**：以 `qq_live_digest/routes.py` 为唯一事实来源（`GET_ROUTES` 61、`POST_ROUTES` 39、前缀模式各 1）。路径与免鉴权标记一条都不许变；`tests/test_routes.py` 会核对路由表与 handler 一一对应。
2. **状态码**：每条路由允许的状态码集合不变，含 400/401/404/409/500/503 的归属。例：`cancel_job` 无任务跑 → 409；`set_task_urgent` 任务不存在 → 200 + `ok:false`（不是 404）。
3. **JSON 键**：响应 `ok` 与其余顶层键不变；错误文案逐字保留（前端有按文案判断的分支）。
4. **数据兼容**：`data/digest.sqlite3` 直接可打开，不做破坏性迁移；表结构与现存数据不变。
5. **配置兼容**：`.env` 全部键不变；写 `.env` 仍是原子写（临时文件 + `os.replace`）。
6. **入口兼容**：`qq-live-digest.exe`、`QQ-Notice-Hub.exe`、托盘菜单、开机自启、桌面快捷方式行为不变。
7. **版本**：不新增版本号，`1.1.0-beta.15` 一直用到 G4 完成。

**对账方法**：改完一条路由，用同一份请求打旧实现与新实现，比 `status` + 响应 JSON 的键集合 + 关键值。基线可用 `git stash`/`git show bd489f5:<path>` 取回旧实现，或直接在 `main` 上跑一份对照。

---

## 7. 你必须先解决的 4 个缺口

| # | 缺口 | 怎么验证 | 要什么答复 |
|---|---|---|---|
| 1 | 提交说 G2 完成，但 18 条用例 0 条存在、`application/__init__.py` 只有 docstring | `ls noticehub/application/` + 逐名搜 18 个函数 | G2 的真实完成度与剩余清单 |
| 2 | `set_task_urgent` 等 4 条只有 SQLite store 方法 | `grep -n "def set_task_urgent" noticehub/infrastructure/sqlite/store.py` | 用例 = store 的薄包装，还是重新设计？ |
| 3 | 签名不符**三处**：实现 `tick(host, now)` vs 契约 `tick(now)`；实现 `import_existing_tasks(host, *, days, now)` vs 契约 `import_existing_tasks(days=7)`；实现 `accept_message(event, record, *, store, settings, logger, now)`（`noticehub/application/ingest.py:10`）vs 契约 `accept_message(event)` | `grep -n "def tick\|def import_existing_tasks\|def accept_message" noticehub/application/*.py` | 以哪边为准，开工前定 |
| 4 | `describe_calendar_subscription` 依赖网卡/隧道探测（基础设施） | 读 `qq_live_digest/webapp.py` 的 `_advertised_calendars` | 用例只做「给定地址列表选 primary」，探测留在 adapter？ |

---

## 8. 交付格式

- **一条用例一个 commit**，单 commit ≤400 行（`06-migration.md` 的约定）。
- 每个 commit 说明写三件事：改了什么、为什么、怎么验证。
- 每次交付附：`git diff --stat`、全量测试尾部（不截断）、单独取的退出码。
- 卡住超过一天：停下来，把问题写进 `docs/rewrite/` 的发现清单，不要带着不确定继续写。
- 发现契约本身有错：改契约 + 记一行发现清单，再改代码（`06-migration.md` 的规则）。

---

## 9. 我方角色（不要越界找我之外的人）

- 我方只做**验收与合并**：按第 6 节对账、跑全量门禁、决定何时合进 `main`。
- 我方**不写** `noticehub/**`，不改 G3 接线，不在你的分支上抢文件。
- 需要任何历史行为（某个接口在某状态下返回什么、某条文案为什么这么写）：问我方负责人，我方给准确答案与 `文件:行`。
- 用户真实实例 `F:\notice-hub` 不许碰；发版由我方做，且要用户单独同意。

---

## 10. 关键路径速查

```
F:\qq-notice-hub\docs\rewrite\     契约与阶段文档（你的依据）
F:\qq-notice-hub\noticehub\        你的产出
F:\qq-notice-hub\qq_live_digest\   旧实现（G3 里逐步被替换，G4 后删除或再导出）
F:\qq-notice-hub\tests\            全量测试 1623 项
F:\nh-clean-test\                  干净测试实例 8767（可用）
F:\notice-hub\                     用户实例（禁止触碰）
```

```powershell
# 全量门禁
cd F:\qq-notice-hub
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t .

# 版本号（只此一处）
# qq_live_digest/version.py: APP_VERSION / APP_CHANNEL

# 路由基线
# qq_live_digest/routes.py: GET_ROUTES / POST_ROUTES / GET_PATTERN_ROUTES / POST_PATTERN_ROUTES
```
