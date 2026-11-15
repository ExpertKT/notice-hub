# 对《G2 完成度与 G3 起点》的确认

**结论：四点方向都对，可以照此推进 G3。但有两处照原文做会漏实现，另有一处签名不符没列进来，第二条之后再动手。**

本文所有行号基于 `rewrite/clean-room` 的 `a3b8deb`；路径都是相对仓库根。

---

## 一、四点逐条确认

### 1. G2 的真实完成度 —— 对

- 分层、旧入口转调、CLI 都已落地，全在 `a3b8deb`。
- `qq_live_digest/store.py` 此时只剩 21 行 re-export；真实现在 `noticehub/infrastructure/sqlite/store.py`。
- `noticehub/application/` 只有 `attachments.py`、`history.py`、`ingest.py`、`schedule.py`，`__init__.py` 只有 docstring。
- 18 条核心用例一条都不以应用用例形式存在。其中 4 个名字确实只是 SQLite store 层方法：
  - `set_task_urgent` — `noticehub/infrastructure/sqlite/store.py:885`
  - `set_task_pinned` — `:902`
  - `apply_task_action` — `:1084`
  - `task_stats` — `:1157`
- 所以 **G3 从写这 18 条开始，不是从接线开始**。这一点完全同意。

### 2. 三个方法留在 Store、用例做薄包装 —— 对，但 `task_id` 不能透传

库表行为留在 store 方法上、不改 SQL，同意。但有一处必须写清，否则会写成 500：

**契约签名收 `str`，store 方法收 `int`。**

```
契约：set_task_urgent(task_id: str, urgent: bool | None) -> TaskResult   # 04-application.md:91
      set_task_pinned(task_id: str, pinned: bool) -> TaskResult         # 04-application.md:92
现状：def set_task_urgent(self, task_id: int, value: bool | None)        # store.py:885
      def set_task_pinned(self, task_id: int, value: bool)               # store.py:902
```

所以用例不能直接透传，必须自己解析并校验：**ASCII 数字、长度 1–18、数值 ≥ 1**，否则抛 `ValueError("task_id 必须是正整数文本")`。直接透传的话，`task_id="abc"` 会在 `int()` 处炸成 500。

另外两点照现状对齐即可，别另写文案：

- 任务不存在 → `{"ok": false, "error": "任务不存在"}`（`store.py:892`、`:909`），不是 404。
- 置顶拒绝已完成任务 → 错误文案「已完成的任务不能置顶」（`store.py:911` 已有）。

`task_stats` 的返回键固定 7 个，不能少也不能改名：

```
total, candidate, open, done, dismissed, expired, overdue
```

其中 `overdue` 的定义是 `status='open' AND deadline<>'' AND deadline<now`（`store.py:1168`）。这份统计要被 `GET /api/tasks` 与 `GET /api/meta` 共用，两处口径不能分开算。

### 3. 签名以契约为准 —— 对，但是三处不是两处

同意的两条：

```
tick(now: datetime | None = None) -> list[Digest]          # 04-application.md:24
import_existing_tasks(days: int = 7) -> int                # 04-application.md:41
```

现在的 `tick(host, now)`（`noticehub/application/schedule.py:25`）与 `import_existing_tasks(host, *, days, now)`（`schedule.py:181`）确实是并存期让旧 `DigestService` 转调的内部写法，依赖由 bootstrap 注入，不进用例签名。这点理解正确。

**遗漏的第三处：`accept_message`。**

```
契约：accept_message(event)                                                      # 04-application.md
现状：accept_message(event, record, *, store, settings, logger, now)             # noticehub/application/ingest.py:10
```

按同一原则处理：对外签名用契约的 `accept_message(event)`，依赖走注入，不摊在签名上。

### 4. 日历订阅 —— 对，但返回结构不止六个顶层键

网卡与隧道探测留在现有适配代码、用例只接收已探测好的地址列表，同意。返回也不只是组 6 个顶层键，下面都要落到实现里：

**顶层成功键**：`ok, primary, lan_base, port, events, calendars`（`04-application.md:113`）。

**`calendars` 每条五个键**：`name, kind, url, hint, virtual`；并且返回前要把同一个 `events` 数写进每一条。

**`kind`**：只有 `public` 或 `lan` 两种值。

**`virtual`**：布尔。地址落在 VirtualBox、Docker、libvirt、WSL2 这些网段时为真；前端据此置灰并排后。

**`primary` 选取**：有 `kind == public` 就用公网地址；否则用排序后第一条 `virtual` 为假的；全部为真则退回第一条。

**局域网排序**：真实 `192.168.0.0/16` → `10.0.0.0/8` → Tailscale `100.64.0.0/10` → 其它 → 虚拟网卡最后；同一档按地址字符串排序。

---

## 二、动手顺序

1. 先按上面第 2 条把三个任务用例的 `task_id` 解析与校验写对，跑 `GET /api/tasks`、`POST /api/tasks/urgent`、`POST /api/tasks/pin` 三条路由的回归。
2. 再写 `task_stats`，同时检查 `/api/tasks` 与 `/api/meta` 两处读数是否一致。
3. 然后 `describe_calendar_subscription`，先写排序与 `primary` 选取的纯函数并单测它（不依赖网卡），再接探测结果。
4. `tick` / `import_existing_tasks` / `accept_message` 三个签名按契约收口，依赖注入不动。
5. 其余用例按契约顺序推进。

---

## 三、验收硬条件（不变的那部分）

- **路由路径、免鉴权标记、状态码、JSON 顶层键一律不变。** 基线以 `qq_live_digest/routes.py` 为准：`GET_ROUTES` 61 条、`POST_ROUTES` 39 条、前缀模式各 1 条。`tests/test_routes.py` 会核对路由表与 handler 一一对应。
- **全量测试必须绿**，skip 不算通过：
  ```
  .venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t .
  ```
- **一条用例一个 commit，单 commit ≤ 400 行**，附 `git diff --stat`、未截断的测试尾部、单独取的退出码。
- 删旧实现只发生在调用点全部切走、全量测试仍绿之后。
- G3 期间不要 merge 回 `main`；`main` 上新的提交，动手前先快进吸收。
- **不要碰真实用户实例 `F:\notice-hub`**（网页 8766、QQ 8765）。

---

## 四、事实基线

| 项 | 值 |
|---|---|
| 仓库 | `F:\qq-notice-hub` |
| 分支 | `rewrite/clean-room` |
| HEAD | `a3b8debfead65f3c7607703d1e39cf5fc8db520b` |
| 该提交说明 | `docs(rewrite): 告诉主线 G2 已写完可以接 Web` |
| 主线 `main` | `b422a6d`（已含 beta.15 与 CI 修复，六个 CI job 全绿） |
| 路由基线 | `GET_ROUTES` 61 + `POST_ROUTES` 39 + 前缀各 1 |
| 测试解释器 | `.venv\Scripts\python.exe`（3.14）或 `F:\nh-py312-venv`（3.12，与 CI 同版本） |

有疑问直接回这份文档，别猜。
