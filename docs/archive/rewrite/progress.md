# 改动记录

只追加、不覆盖。每条对应一次交付；五节固定：要求来源 / 改了什么 / 接线 / 验证 / 未做·存疑。
验证一律是原样命令 + 真实退出码，不接受「测试通过」四个字。

---

## [2026-10-31 01:12] 第 1 批 · 去掉 auto_setup 测试对「本机装没装 QQ」的隐式依赖

提交 `b422a6d643c465a50f79d519e7cf11a450b50004`（2026-10-30 14:16:04 +0800），1 文件 +29 −2。补记。

**要求来源**：CI（GitHub Actions）三平台全红，唯一失败是 `test_file_fallback_preserves_and_deduplicates` 的 `assertTrue(restart_required)`。根因不是产品缺陷，是测试的隐式环境依赖。

### 改了什么

- `tests/test_napcat_admin.py:57`（`NapcatAdminTests.test_live_root_wins_even_when_later`）——改前：只 patch `find_roots`/`detect_boot`/`_request`/`restart`；改后：追加 `patch.object(na, "find_qq_exe", return_value=first / "QQ.exe")`，把「本机装了 QQ」写成显式前提。
- `tests/test_napcat_admin.py:657`（`test_file_fallback_preserves_and_deduplicates`）——同上，追加 `find_qq_exe` 返回假路径；断言不变（仍然 `assertTrue(result["restart_required"])`）。
- `tests/test_napcat_admin.py:668` 新增 `test_file_fallback_without_qq_reports_qq_missing`（+22 行）——覆盖 CI 上真实走到的分支：`find_qq_exe` 返回 `None`，断言 `applied_via == "file"`、`ok is False`、`stage == "qq_missing"`、`error` 含「没找到官方 QQ」「下载官方 QQ」「重新检测」，且**不含**「QQ_DIGEST_NAPCAT_DIR」（缺 QQ 时不能把用户引去改 .env）。

### 接线

- 测试侧：`NapcatAdminTests.test_*` → `napcat_admin.auto_setup(S())`。
- 生产侧入口：`POST /api/napcat/autosetup` → `qq_live_digest/webapp.py:4193` → `napcat_admin.auto_setup` → `napcat_admin.find_qq_exe()`（`:543`）。
- 两条 `qq_missing` 分支：`napcat_admin.py:1282-1285`（启动前就缺 QQ）与 `:1449-1451`（启动失败且仍缺 QQ）。后者**不设** `restart_required`，正是 CI 走过去导致断言飘的那条。
- 本提交不改产品代码，只把「机器上有没有 QQ」这个前提写进测试。

### 验证

在 `git archive b422a6d` 导出的干净目录里跑（工作区当时的 HEAD 已被切走，为避免被测到别人的未提交改动，故用导出目录）：

```text
.venv\Scripts\python.exe -X utf8 -m unittest tests.test_napcat_admin
Ran 78 tests in 14.027s
OK
EXIT=0
```

单条新用例：

```text
.venv\Scripts\python.exe -X utf8 -m unittest tests.test_napcat_admin.NapcatAdminTests.test_file_fallback_without_qq_reports_qq_missing
Ran 1 test in 0.040s
OK
EXIT=0
```

### 未做 / 存疑

- 未在 CI 三平台复跑（只有本机 Windows）；「CI 上不再红」这一点本次没有直接证据，只证明了在「显式 patch 掉 QQ 存在性」后本机通过。
- 未反向验证「去掉 patch 后在没装 QQ 的机器上会红」——本机装了 QQ，构造不了该环境。

---

## [2026-10-31 01:12] 第 2 批 · 修 QQ 接入卡结构错位 + 状态驱动改造带出的指纹确认死路

提交 `ba1e09af4b63122c67d8f826da4be231a147c6ff`（2026-10-30 20:17:45 +0800），8 文件 +592 −134。补记。

**要求来源**：用户实测反馈「QQ 接入卡意义不明而且乱」；追下去发现 `#connect` 少一个 `</section>` 把五张卡包进了接入卡，以及状态驱动改造后冷装用户卡在指纹确认死路。

### 改了什么

产品代码（4 个文件）：

- `qq_live_digest/web_static/page.html`
  - `:724` 前后——改前：`<section id="groupbox">` 被包在 `#connect` 内部（因为 `</section>` 缺失）；改后：移位闭合标签，`#groupbox` 改为 `<section id="groupbox" class="surface rail-panel subscription-panel" …>`。接入卡因此从 2743px 回到正常高度，向导「去配置」落点也不再落到页头之上。
  - `:716-717`——改前：接入卡里同时摆 `#install-napcat`、`#qr-start`、`#auto-setup`、`#login-more` 与重复的三段说明；改后：只留 `#auto-setup` / `#go` / `#refresh-qr` / `#restart-napcat` 四个按钮由状态决定显示哪一个，重复说明合并进新增的 `#qq-next-step`（`:717`）。删除 `#install-napcat`、`#qr-start`、`#login-more` 三个节点。
  - `:796` 新增 `scrollTargetIntoView(target, behavior)`——改前：6 处各自写 `scrollIntoView({block:'center'})` 并用 `setTimeout` 赌渲染；改后：统一走这个 helper（矮目标 center、超高目标 start + 动态 `scroll-margin-top`）。
  - `correctionPanel(task)` 内——改前：手工待办也显示「不是通知/不是待办/标记重复」三个 AI 纠错键；改后：`task.task_key` 以 `manual:` 开头时换成「删除这条待办」两段式确认（4 秒内再点一次），并注明删除不写学习记录。
- `qq_live_digest/web_static/js/guide.js`
  - `:23` 新增 `QQ_CONNECT_BUTTON_IDS = ['auto-setup','go','refresh-qr','restart-napcat']`。
  - `:24` 新增 `applyQQConnectState(state, accountLabel, statusOwnsMessage)`——四个状态各只留一个主按钮，隐藏用 `hidden` 属性（不进 Tab 序）。
  - `:12` `applyNapcatQrState`——改前：同时管 `#qr-start` / `#qr-start-note`；改后：不再管这两个已删节点，只改 `#qr` 与 `#qr-note` 文案。
  - `:299` `highlightTarget`——改前：直接 `scrollIntoView({block:'center'})`；改后：调 `scrollTargetIntoView(target,'smooth')`。
  - `:330` 附近 `initJourney` 第 1 步——改前：`startHostingAction('start', action)` 直接开托管；改后：只 `highlightTarget(document.getElementById('host-quick'))`，顶栏「开始托管」成为唯一主入口。
- `qq_live_digest/web_static/js/settings.js`
  - `:62` 更新卡滚动——改前：写死 `card.scrollIntoView({block:'center'})` 并留注释说明页头 sticky 遮挡；改后：`if (card) scrollTargetIntoView(card, 'smooth')`。
  - `:1040-1044` `refreshHosting`——改前：只写状态文案；改后：按 `status.hosting_active` 隐藏 `#hosting-start` / `#hosting-stop` 中的一个，避免当前不该做的事也当主按钮。
- `qq_live_digest/store.py`
  - `:1084`——改前：`SELECT status FROM tasks WHERE id = ?`；改后：`SELECT status, task_key FROM tasks WHERE id = ?`。
  - `:1143-1144`——改前：`if current not in {"done","dismissed","expired"}: return False`；改后：先算 `manual_open = current == "open" and str(row["task_key"] or "").startswith("manual:")`，条件改为 `… and not manual_open`。真删行与 `task_events`，不写 `dismissed`。

测试与场景（4 个文件）：

- `tests/test_webapp.py` +357：新增 4 条用例 `test_manual_open_task_can_be_deleted_without_training_noise`、`test_normal_open_task_delete_is_still_refused`、`test_wizard_target_scroll_settles_and_corrects`、`test_manual_task_swaps_ai_corrections_for_delete`；新增辅助 `_napcat_launch_stub`。
- `tests/test_ui_browser.py`——改前：等 `#qr-start` 可见、stub `POST /api/napcat/autosetup`；改后：等 `#go` 可见、stub `POST /api/napcat/launch`（`_napcat_autosetup_stub` 改名 `_napcat_launch_stub`，返回体简化为 `{"ok": true}`）。
- `tests/test_page_beta11.py:43`——改前：`assertIn("/未找到电脑版 ?QQ|NapCat Shell/", PAGE_SOURCE)`；改后：`assertIn("eStage === 'qq_missing' ? (showQqMissingGuide()", PAGE_SOURCE)`（触发点跟着主按钮走）。
- `tools/e2e/scenarios/27_napcat_fingerprint.py` +113：新增 `_qq_state(app)`、`_resolve_primary(app)`、`_fingerprint_failed(app, selector)`。

### 接线

- 状态 → 按钮：`GET /api/napcat/status` → `guide.js:149 function checkSetup()` 的 `.then` → 由 `napcat_installed` / `napcat_running` / `ok` 算出 `engineState` → `applyQQConnectState(engineState, nickname||uin)`（调用点在 `guide.js:149` 那条长语句内）。
- 主按钮 → 后端：`#auto-setup`（冷装，`guide.js:222 runAutoSetup` 驱动安装进度轮询）、`#go` → `POST /api/napcat/launch`、`#refresh-qr` → 重取二维码、`#restart-napcat` → 重启引擎。指纹确认 `showFingerprintConfirm()` 挂在 `#auto-setup` 上——本提交把 `engine_missing` 的主按钮指回 `#auto-setup`，正是为了这条死路。
- 滚动 helper：定义在 `page.html:796`，调用点 6 处——`guide.js:71`、`guide.js:299`、`settings.js:62`、`page.html:1844`、`page.html:2684`、`page.html:2820`。
- 手工待办删除：前端 `correctionPanel(task)` → `sendCorrection(task,'delete')` → `POST /api/tasks/{id}/action` → `webapp.py:4259 self.store.apply_task_action(task_id, action, detail=detail)` → `store.py:1143` 的 `manual_open` 白名单 → `DELETE FROM tasks` / `DELETE FROM task_events`。

### 验证

```text
.venv\Scripts\python.exe -X utf8 -m unittest tests.test_page_beta11
Ran 21 tests in 0.435s
OK
EXIT=0
```

```text
.venv\Scripts\python.exe -X utf8 -m unittest tests.test_webapp
Ran 357 tests in 213.365s
FAILED (errors=1)
EXIT=1
```

复跑同一命令得到 `FAILED (errors=3)`；错误都是回环 HTTP 的 `ConnectionAbortedError [WinError 10053]`（如 `TaskApiTest.test_selfcheck_over_http_uses_overview_sources` 在 `tests/test_webapp.py:2334 self._get` 处连接被中止）。涉事用例单跑：

```text
.venv\Scripts\python.exe -X utf8 -m unittest tests.test_webapp.TaskApiTest.test_selfcheck_over_http_uses_overview_sources
Ran 1 test in 4.116s
OK
EXIT=0
```

### 未做 / 存疑

- 提交正文自述「`tests.test_webapp` 378」，本次实测 357（两次一致）。差异原因未查明，不排除当时含别的模块或用例选择不同。
- `tests.test_webapp` 全模块跑不绿：错误是连接被中止，单跑同一条用例通过，判定与全模块并发起真实 HTTP 服务 + 同机另有进程负载有关；没有证据表明是本提交引入。
- `tools/e2e/scenarios/27_napcat_fingerprint.py` 未跑：需要真实 QQ + NapCat 环境，本机不具备。
---

## [2026-10-31 01:12] 第 3 批 · 固定状态提示区高度消除轮换抖动 + 隔离 Playwright 临时目录

提交 `074c154b44e85622f53d29cfe4994f14a807878e`（2026-10-30 23:54:54 +0800），2 文件 +87 −2。补记。

**要求来源**：两件事。① `#status` 每 5 秒按后端阶段换文案，各条长度不同导致容器高度变化、下方按钮被挤得跳动（实测 1280 宽总高极差 58.5px、390 宽 78px）。② `tests.test_ui_browser` 两条用例在 `%TEMP%` 不可写的环境必挂。

### 改了什么

- `qq_live_digest/web_static/page.html:359-361`——改前：`#status` 只是一段普通文本节点，高度随文案变；改后：新增 `.status-slot{display:grid;align-items:stretch}` 与 `.status-slot>*{grid-area:1/1;min-width:0}`，加 `.status-slot-fit` 作为隐藏的最长文案占位，靠网格叠放锁定高度，文案仍完整不截断。
- `qq_live_digest/web_static/page.html:724`——改前：`<div id="status" role="status" aria-live="polite">…</div>` 直接裸露；改后：包进 `<div class="status-slot">` 并在其后加 `<p class="status-slot-fit" aria-hidden="true">` 占位。
- `qq_live_digest/web_static/page.html:519`——新增一行 `#update-status,#wx-status,#dt-scan,#wx-scan,#group-pull-status{min-height:3em}`，给同类短提示区兜底最小高度。
- `tests/test_ui_browser.py:136` `setUpClass` / `tearDownClass`——改前：无（用例直接依赖系统 `%TEMP%`）；改后：类级把 `TEMP`/`TMP`/`TMPDIR` 与 `tempfile.tempdir` 指向隔离目录，`tearDownClass` 还原，产品代码不动。
- `tests/test_ui_browser.py:208` 新增 `test_status_banner_height_is_stable`——在真实浏览器里把三段最长文案依次塞进 `#status`，断言卡片高度极差 ≤1px、占位槽高度极差 ≤1px、下方 `.connect-actions` 的 top 极差 ≤1px。

### 接线

- 无新增调用接线。`#status` 的文案仍由 `guide.js:149 checkSetup()` 写；本提交只改容器结构与样式，不改写入方。
- 新增用例走 `tests.test_ui_browser` 的 Playwright + msedge 路径，`setUpClass` 负责临时目录隔离。

### 验证

```text
.venv\Scripts\python.exe -X utf8 -m unittest tests.test_ui_browser
Ran 2 tests in 41.381s
OK
EXIT=0
```

全量门禁：

```text
.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t .
Ran 1634 tests in 426.712s
FAILED (failures=2)
EXIT=1
```

两条失败，均不在本提交触达的文件里：

```text
FAIL: test_deadline_reminders_fire_once_and_cover_tomorrow (tests.test_service.ServiceTest)
  tests/test_service.py:736  assertEqual(len(pusher.calls), 2)  ->  AssertionError: 3 != 2
FAIL: test_late_evening_start_skips_morning_reminder (tests.test_service.ServiceTest)
  tests/test_service.py:785  assertEqual(len(pusher.calls), 1)  ->  AssertionError: 2 != 1
```

同样两条用例在更早的提交上一样红（证明不是本批引入）：

```text
在 b422a6d 导出目录：Ran 1 test  FAILED (failures=1)  AssertionError: 3 != 2   EXIT=1
在 ba1e09a 导出目录：Ran 2 tests FAILED (failures=2)  AssertionError: 3 != 2 / 2 != 1   EXIT=1
```

### 未做 / 存疑

- 提交正文自述「全量门禁 Ran 1634 tests / OK / EXIT=0」，本次复跑同一命令是 `FAILED (failures=2)`（用例数 1634 对得上）。差异来自运行时段：这两条提醒用例依赖当前钟点，本次在 01:00 前后运行必然多推一条。
- 这两条是既有的时间依赖缺陷，本批没修，**需要单独排期**，且不应记在这三个提交头上。
- `.status-slot` 占位文案是手写的「最长文案」，将来 `#status` 出现更长文案时占位要同步加长（`page.html:358` 的注释已写明），否则高度保护会失效。已知上限，不是缺陷。

---

## 附：命名规则核对（第 1–3 批）

按 `naming-and-change-log` 的三条可直接判定的规则抽查这三个提交新增/改名的标识符：长度 ≤24 字符、禁用伪缩写、禁用同义词动词。共 139 个新增命名实体（去重）。

**结论：产品代码 2 条违规（禁用动词 `remove`）；测试代码 10 条违规（9 条超长 + 1 条伪缩写 `helper`）；无其他类型问题。**

超过 24 字符，9 条（全部在 `tests/`）：

| 标识符 | 长度 | 位置 |
|---|---|---|
| `test_manual_open_task_can_be_deleted_without_training_noise` | 59 | `tests/test_webapp.py` |
| `test_file_fallback_without_qq_reports_qq_missing` | 48 | `tests/test_napcat_admin.py` |
| `test_manual_task_swaps_ai_corrections_for_delete` | 48 | `tests/test_webapp.py` |
| `test_wizard_target_scroll_settles_and_corrects` | 46 | `tests/test_webapp.py` |
| `test_normal_open_task_delete_is_still_refused` | 45 | `tests/test_webapp.py` |
| `scenarioManualTaskCorrectionSwapped` | 35 | `tests/test_webapp.py` |
| `test_status_banner_height_is_stable` | 35 | `tests/test_ui_browser.py` |
| `scenarioQQConnectStateButtons` | 29 | `tests/test_webapp.py` |
| `scenarioWizardTargetScroll` | 26 | `tests/test_webapp.py` |

伪缩写，1 条：

| 标识符 | 命中 | 位置 |
|---|---|---|
| `helperModule` | `helper` | `tests/test_webapp.py` |

禁用同义词动词，3 条：

| 标识符 | 命中 | 位置 |
|---|---|---|
| `removeManual` | `remove`（该用 `delete`） | `qq_live_digest/web_static/page.html` |
| `removeManualTimer` | `remove`（该用 `delete`） | `qq_live_digest/web_static/page.html` |
| `makeTarget` | `make`（该用 `build`） | `tests/test_webapp.py` |

产品代码里合规的代表：`scrollTargetIntoView`（20）、`applyQQConnectState`（19）、`QQ_CONNECT_BUTTON_IDS`（21）、`showQqMissingGuide`（18）。

顺带发现（不属于这三批）：`bindSettingsDirtyTracking` 25 字符，超 1 字符，来自 `bd489f5`；`markSettingsDirty`（17）合规。

**未做 / 存疑**：

- 本仓库没有 `docs/NAMING.md`，三张表（动词表、领域名词表、缩写表）尚未建立，所以「名字里的词只能从表里取」这条无法判定，本次只核了可直接判定那三条。
- 上述 12 条违规**未改**（改名要单独提交、单独验证，不与逻辑混）。挂账在此。

---

## [2026-10-31 01:22] 更正第 2、3 批 · 根因、口径，并补齐命名挂账行号

按「只追加、不回改历史」的规矩，前两条记录里的偏差在此更正，原文保留不动。

### 更正 1：第 3 批说「时间依赖」不准，根因是**周日**

- 原文（第 3 批 · 未做 / 存疑）：「差异来自运行时段：这两条提醒用例依赖当前钟点，本次在 01:00 前后运行必然多推一条。」
- 更正为：**跟星期几有关，不跟几点运行有关。** 多出来的那一条推送是「每周通知复盘」，插桩输出：

```text
PUSH>> 今日截止 | 【QQ今日截止】2026-11-01 07:31
PUSH>> 截止提醒 | 【QQ截止提醒】2026-11-01 21:00
PUSH>> 每周通知复盘 | 新增候选 0 条，确认 0 条，忽略 0 条
```

`tests/test_service.py:36` 的 `day()` 以「今天」为锚；10-31 是周六，`day(1, 21, 0)` 落在 11-01（周日）21:00，正撞 `weekly_review_weekday=6` / `weekly_review_time=20:30`。**产品行为正确，是测试的日期脆弱性。** 已派后端3 修（非 weekly 用例关掉开关 + 加防复发用例）。

「不记在这三个提交头上」的结论不变。

### 更正 2：第 2 批的「378 vs 357」不成立，是口径差，此项撤回

- 原文（第 2 批 · 未做 / 存疑）：「提交正文自述 378，本次实测 357…差异原因未查明。」
- 更正为：**没有差异。** 378 是 `tests.test_webapp tests.test_page_beta11` 两个模块合跑的结果：357 + 21 = 378。本记录第 2 批「验证」一栏里 `tests.test_page_beta11` 正好是 `Ran 21 tests ... OK`，数字自洽。是口径不同，不是数据对不上，不用再追。

### 补充 3：回环 HTTP `ConnectionAbortedError [WinError 10053]` 已有主

不是无主抖动。后端3 正在把三条耗时 4.9 秒的用例的客户端超时从 5 秒抬到 20 秒——**余量只有 100 毫秒才是真问题**，与第 2 批记录的现象同源。

### 补充 4：12 条命名违规的准确行号（挂账，本轮不改）

行号取各自提交里的状态（`git show <sha>:<path>`），不随工作区在途改动漂移。

产品代码，2 条（禁用同义词动词）：

| 标识符 | 位置（ba1e09a） | 违规 | 建议 |
|---|---|---|---|
| `removeManual` | `qq_live_digest/web_static/page.html:1216`（另见 `:1217`、`:1218`、`:1220`、`:1222`、`:1225`、`:1233`） | 动词 `remove` | `deleteManual` |
| `removeManualTimer` | `qq_live_digest/web_static/page.html:1219`（另见 `:1221`、`:1223`、`:1224`、`:1229`、`:1230`） | 动词 `remove` | `deleteManualTimer` |

测试代码，10 条：

| 标识符 | 位置 | 违规 |
|---|---|---|
| `test_manual_open_task_can_be_deleted_without_training_noise` | `tests/test_webapp.py:2293` | 59 字符 > 24 |
| `test_normal_open_task_delete_is_still_refused` | `tests/test_webapp.py:2306` | 45 字符 > 24 |
| `test_wizard_target_scroll_settles_and_corrects` | `tests/test_webapp.py:7867` | 46 字符 > 24 |
| `test_manual_task_swaps_ai_corrections_for_delete` | `tests/test_webapp.py:7881` | 48 字符 > 24 |
| `test_file_fallback_without_qq_reports_qq_missing` | `tests/test_napcat_admin.py:671`（b422a6d） | 48 字符 > 24 |
| `test_status_banner_height_is_stable` | `tests/test_ui_browser.py:208`（074c154） | 35 字符 > 24 |
| `scenarioWizardTargetScroll` | `tests/test_webapp.py:7458` | 26 字符 > 24 |
| `scenarioQQConnectStateButtons` | `tests/test_webapp.py:7651` | 29 字符 > 24 |
| `scenarioManualTaskCorrectionSwapped` | `tests/test_webapp.py:7604` | 35 字符 > 24 |
| `helperModule` | `tests/test_webapp.py:7572`（调用 `:7583`） | 伪缩写 `helper` |
| `makeTarget` | `tests/test_webapp.py:7460`（调用 `:7508`） | 动词 `make` |

**本轮不改。** 理由：skill 要求「改名单独一个提交，不与逻辑混」，现在工作区有四个人的在途改动，此时改名会把 diff 搅乱。等这轮收口、打完包，单独开一个 rename 提交一次性做。

### 补充 5：`bindSettingsDirtyTracking` 判为**有意保留**

`settings.js:334`，25 字符，超 1 个字符，来自 `bd489f5`（不在这三批内）。判定不改：25 vs 24 的收益抵不上跨两个文件重命名的成本。这是**有意保留**，不是遗漏。

### 未做 / 存疑

- 上面 12 条 + `bindSettingsDirtyTracking` 共 13 处命名问题，全部挂账，一个都没动。
- 全量门禁当前仍是红（`Ran 1634 / FAILED (failures=2)`），归后端3 修日期脆弱性；修完需要复核的正是本记录第 3 批那条命令。
- 本文件只追加，不覆盖；上面「更正 1/2」是新增段落，原文未删。

---

## [2026-10-31 03:30] 第 4 批 · df2007e：收件箱真图、关键词标签输入、全站去 AI 味

提交 `df2007eecb626a33c317506d32555c548ed55210`（2026-10-31 02:48:00 +0800），**19 文件 +1304 −85**。这是本轮最大的一批。

顺带一个事实：本文件（`docs/progress.md`，299 行）就是被这一批一起提交进去的，所以本批记录写在同一文件里属于「自己记自己」。

**要求来源**：本轮四项要求——① 收件箱要能看到真图（不是「[图片]」两个字）；② 关键词、发件人、群 ID 改成标签式输入；③ 全站去 AI 味、产品名统一；④ 自查所有未落地事项。

### 改了什么

#### A. 收件箱真图（3 个文件）

**`qq_live_digest/attachments.py`（+114）** —— 新增 7 个模块函数、1 个方法、3 个常量：

| 位置 | 内容 |
|---|---|
| `:1232` | `IMAGE_MAX_BYTES = 8 * 1024 * 1024`（新增，单张上限 8MB） |
| `:1233` | `IMAGE_TYPES`（新增，`.jpg/.jpeg/.png/.gif/.webp` → MIME） |
| `:1239` | `_IMAGE_MAGIC`（新增，PNG/JPEG/GIF 魔数表） |
| `:1248` | `image_dir(data_dir)`（新增，`data/files/`） |
| `:1253` | `image_key(msg_id)`（新增，sha1 前 16 位，只用十六进制） |
| `:1258` | `image_suffix(attachment, head)`（新增，按魔数定后缀） |
| `:1269` | `save_image(data_dir, attachment, path, logger)`（新增，`O_EXCL` 写、同一消息最多 20 张） |
| `:1310` | `images_of(data_dir, msg_id)`（新增） |
| `:1318` | `has_image(data_dir, msg_id)`（新增） |
| `:1325` | `delete_images(data_dir, msg_ids)`（新增，返回删掉的张数） |
| `:1188` | `_keep_image()`（新增方法，内部转调 `save_image`） |
| `:1082-1083` | `process()` 里新增 `if attachment.kind == "image": self._keep_image(...)`，改前只有 `self.stats.skipped += 1` 那条早退路径 |

改前的行为：图片只在视觉模型开启时才被读，读完 `_discard()` 即删；视觉模型关闭时收件箱只有「[图片]」两个字、底下什么都没有。改后：下载到的原图先留一份，`_discard()` 照旧删临时文件，留档那份不受影响。

**`qq_live_digest/webapp.py`（+89 −?）**：

| 位置 | 改前 | 改后 |
|---|---|---|
| `:51` | 无 | `from .attachments import delete_images, has_image, image_key` |
| `:3061` | `/api/inbox` 每条不带图片信息 | `item["has_image"] = has_image(data_dir, str(item.get("msg_id") or ""))` |
| `:2670-2676` | 只校验 `msg_id` 非空 | 额外卡 `len > 256`、含 `..`、含 `/`、含 `\` → 一律 `400 {"ok":false,"error":"bad msg_id"}` |
| `:2682` | 只认 `onebot-attachment` 来源 | 先试 `onebot-attachment` 的精确对库，再退到 `files/<image_key>_<n>.<ext>`；文件在就返回 |
| `:2696` | 无 | 新增 `_inbox_image_of(files_dir, msg_id)`：只认 `files/` 下唯一常规文件，不跟符号链接、多张即歧义 |
| `:2688` | —— | 找不到或不唯一 → `404 {"ok":false,"error":"image not found"}` |
| `:4024` | 删收件箱条目只删库记录 | 追加 `delete_images(...)`，原图跟着一起删，`files/` 不留孤儿 |

**`qq_live_digest/web_static/page.html`（+72 −?）** `:2239` —— `renderInboxItem()` 里：改前条件是 `item.source === 'onebot-attachment'`，图片 append 进默认收起的 `<details>`（`#inbox-content`）里；改后条件是 `item.has_image`，图片挂在卡片 `row` 上、不进折叠块，外面包一层 `<a target="_blank">` 看原图，`onerror` 时把整层链接撤掉不留空占位。`alt` 也从固定「群图片附件」改成「群名/发件人 + 的图片」。

#### B. 关键词标签输入（1 个文件，+266）

**`qq_live_digest/web_static/js/lab-rules.js`**：

| 位置 | 内容 |
|---|---|
| `:126` | `CHIP_SEPARATOR = /[,，、;；\n\r]+/`（新增，回车/逗号/顿号/分号换行都切） |
| `:127` | `CHIP_PASTE_SEPARATOR = /[,，、;；\s]+/`（新增，粘贴时空白也切） |
| `:130` | `chipWords(text, splitSpaces)`（新增） |
| `:143` | `mergeChips(current, text, splitSpaces)`（新增，静默去重） |
| `:152` | `dropChip(current, index)`（新增，空输入框退格删最后一个） |
| `:158` | `CHIP_STYLE_ID = 'nh-chips-style'`（新增） |
| `:161` | `CHIP_STYLE`（新增，`×` 44×44 且可键盘操作） |
| `:173` | `ensureChipStyles(doc)`（新增） |
| `:184` | `CHIP_FIELDS`（新增，关键词/发件人/群 ID 三个字段） |
| `:192` | `enhanceChips(input, noun)`（新增，把普通 input 换成 chips） |
| `:332` | `bootKeywordChips()`（新增） |
| `:341` | `watchKeywordChips()`（新增，后插进来的卡片也增强） |

两处关键取舍：**正则字段故意不拆**（正则里可能有 `a{1,2}` 这种逗号）；**提交出去仍是字符串数组**，接口与数据结构未变。保存失败不丢标签。

#### C. 界面一致性（`page.html`）

| 位置 | 改前 | 改后 |
|---|---|---|
| `:13` | `<title>群消息待办</title>` | `<title>群务台</title>` |
| `:44-50` | `.stats` 是 `gap:8px` 的胶囊排；`.stat` 带 `background/border/radius:99px`（按钮外观）；`.stat.show{display:inline-block}` | `.stats` 改 `align-items:baseline;gap:14px`；`.stat` 去掉背景/描边/圆角只留灰字；新增 `.stat strong`（15px、`tabular-nums`）；`.stat.show{display:inline-flex}` |
| `:316` | `.local-badge` 带描边 | 去描边（`min-height:36px;padding:0 2px;border:0`），与旁边可点的托管按钮区分 |
| `:349` | 无 | `#tasks-clear-done{margin-left:12px}`，同一排只有「清空已完成」是真按钮 |
| `:530` | 深色主题选择器里含 `.local-badge` / `.stat` 描边覆盖 | 同步去掉，避免深色下又长回描边 |
| `:1452` | `showStat(id, text, show)`，整串塞进 `textContent` | `showStat(id, label, value, show)`，拆成 `<span>` 标签 + `<strong>` 数字 |
| `:1800-1802` | `showStat('stat-open', '未完成 ' + n, …)` 三处 | `showStat('stat-open', '未完成', n, …)` 三处，数字单独加权 |

#### D. 文案（8 个文件，逐一列出）

**web_static 侧**

- `page.html:896` `friendlyError()` —— 改前「本机服务没有响应，请确认 notice-hub 正在运行。」→ 改后「本机服务没有响应，先看看群务台是不是还在运行。」
- `page.html:2057` 回溯群提示 —— 「请确认 NapCat 已登录后重试。」→「等 NapCat 登录后再试。」
- `page.html:2196` 探测群数提示 —— 「请最多选择 20 个群进行探测。」→「请最多选 20 个群探测。」
- `page.html:2359` 收件箱空态 —— 「先去「设置」里确认消息来源和推送通道已经接上。」→「先去「设置」里看看消息来源和推送通道接上没有。」
- `js/pair.js:33` 防火墙提示 —— 「需要管理员才能确认是否已放行：非提权进程…」→「要有管理员权限才能确认放行没放行：没提权时…」
- `js/settings.js:568` 微信密钥提示 —— 删掉「（后端还没接上这一步，已回报）」
- `js/guide.js:150` `showQqMissingGuide` 文案 —— 「NapCat 只是一层壳」改为讲清「NapCat 不含 QQ.exe，要靠官方 QQ 才能跑」（`tests/test_page_beta11.py` 的断言同步改）

**Python 侧用户可见字符串**

- `webapp.py:749` 防火墙 state message —— 「需要管理员才能确认是否已放行…」→「有没有放行要管理员权限才查得到…」
- `webapp.py:851` 配对错误 —— 「手机无法访问当前广告地址。请将 QQ_DIGEST_WEB_HOST 改为 0.0.0.0 并重启服务。」→「手机连不上你给它的这个地址。把 QQ_DIGEST_WEB_HOST 改成 0.0.0.0 再重启服务。」
- `webapp.py:853` 配对错误 —— 「手机地址与服务监听地址不一致。请检查…」→「手机里的地址和服务实际监听的地址对不上。核对…」
- `webapp.py:3944` / `:3966` 409 文案 —— 「已有回溯任务在进行」「已有通知提取任务在进行」→「回溯任务已经在跑」「通知提取已经在跑」
- `webapp.py:213` / `:1141` 内嵌页标题 —— 「群消息待办」→「群务台」（重开页与月历页）
- `napcat_admin.py:900` 引擎配置失败 —— 「请检查 NapCat 目录权限和 .env 里的…然后重试「启动并登录」」→「看下 NapCat 目录权限和 .env 里的…对不对，再点「启动并登录」」
- `selfcheck.py:80` 钉钉自检 —— 「到「设置 → 钉钉」检查设置；保存钉钉设置会重新连接监听。」→「到「设置 → 钉钉」看开关和群填了没；保存后会重新连接监听。」
- `selfcheck.py:88` 微信自检 —— 「本机没找到微信；确认微信已安装并登录过。」→「本机没找到微信：先装好微信并登录一次。」
- `updater.py:1014` —— 「此平台不支持自动安装更新，请手动下载安装。」→「这个平台不能自动装更新，请手动下载安装。」
- `main.py:243` 诊断包提示 —— 「把它发给我们即可；…」→「把它发给我们；…」

日志文案保持原样，没动。

#### E. 测试（7 个文件）

- **`tests/test_service.py`（+39）** —— 4 处加 `weekly_review_enabled=False`（`:706`、`:775`、`:995`、`:1044`），另在 `:795` 新增 `test_weekly_review_only_fires_when_enabled`，开关两侧都锁（防复发）。
- **`tests/test_webapp.py`（+102）** —— `:2336` 共享客户端超时 `timeout=5` → `timeout=20`；新增 4 条：`test_inbox_renders_image_only_when_item_has_image`、`test_inbox_image_serves_plain_and_catchup_message_images`、`test_inbox_image_plain_message_without_file_is_404`、`test_inbox_image_rejects_traversal_and_ambiguous_plain_files`（辅助 `_write_plain_image`）。
- **`tests/test_ui_browser.py`（+135）** —— `:51-57` 在 `setUpClass` 把 `TEMP`/`TMP`/`TMPDIR` 指到确定可写目录并在 `tearDownClass` 还原（Playwright 的 Node 驱动读 `os.tmpdir()`，不认 Python 的 `tempfile.tempdir`）；新增 `test_task_stats_are_text_and_only_the_action_looks_like_a_button`。
- **`tests/test_attachments.py`（+132）** —— 新增 7 条：`test_image_is_kept_even_without_vision`、`test_two_images_of_same_message_use_indexes`、`test_file_attachment_is_not_kept_as_image`、`test_oversized_image_is_not_kept`、`test_plain_message_image_lands_under_its_message_id`、`test_delete_images_removes_saved_copies`、`test_unknown_image_bytes_are_not_kept`（辅助 `_worker`/`_image`/`_download`）。
- **`tests/test_lab_rules_form.py`（+111）** —— 新增 6 条：`test_keyword_chip_enter_commits_one_word`、`test_keyword_chip_comma_and_paste_split`、`test_keyword_chip_dedupes_silently`、`test_keyword_chip_delete_one`、`test_keyword_chip_submits_string_array`、`test_remove_button_is_a_real_button_with_aria_label`、`test_same_widget_covers_the_other_comma_fields`、`test_chip_and_remove_target_are_tappable`、`test_keyboard_and_paste_paths_are_wired`、`test_new_cards_are_enhanced_too`（共 10 条）。
- **`tests/test_page_beta11.py`（4 行）** —— 断言从 `"NapCat 只是一层壳"` 改为 `"NapCat 不含 QQ.exe，要靠官方 QQ 才能跑"`。
- **`tests/test_ux_fixes.py`（2 行）** —— 空态文案断言跟着改成「暂无判定结果。先去「设置」里看看消息来源和推送通道接上没有。」

### 接线

- **图片留存链**：`AttachmentWorker.process()`（`attachments.py:1082`）→ `_keep_image()`（`:1188`）→ `save_image()`（`:1269`）→ `data/files/<image_key(msg_id)>_<n>.<ext>`。
- **图片出图链**：`GET /api/inbox/image?msg_id=…` → `_route_get_api_inbox_image`（`webapp.py:2665`）→ 校验 `:2670-2676`（空/超 256/`..`/`/`/`\` → 400）→ `_inbox_image_of()`（`:2696`，`onebot-attachment` 先对库，其余按 `image_key` 前缀找唯一常规文件）→ 命中 200 回图、不命中 404。
- **列表带图链**：`GET /api/inbox` → `_route_get_api_inbox`（`:3039`）→ `:3061` 逐条补 `has_image` → 前端 `renderInboxItem()`（`page.html:2239`）据此决定渲不渲缩略图。
- **删图链**：`POST /api/inbox/delete` → handler 删库成功后在 `:4024` 调 `delete_images(settings.data_dir, msg_ids)`，按同一 `image_key` 前缀删文件。
- **chips 提交链**：`enhanceChips()`（`lab-rules.js:192`）把 `<input>` 换成 chips 组件 → 每次增删调用 `mergeChips`/`dropChip` 维护数组 → 提交时**仍写回原 `<input>` 的 value，逗号分隔字符串** → `requestSubmit` 走原有表单序列化 → `POST /api/settings/rules` 收到的仍是字符串数组，接口与数据结构未变。三个字段由 `CHIP_FIELDS`（`:184`）驱动，后插卡片由 `watchKeywordChips()`（`:341`）补挂。

### 验证

干净导出目录（`git archive df2007e` → `.tmp-verify-df2007e`，316 个文件，未受工作区在途改动影响）：

```text
.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t .
Ran 1657 tests in 755.976s
FAILED (failures=2)
EXIT=1
```

提交正文自述同一条命令为 `Ran 1657 tests / OK / EXIT=0`。用例数 **1657 与正文一致**，但结论不同：`FAILED (failures=2)`。两条都在 `tests/test_tasks_latency`，是性能预算用例，不是功能错误：

```text
FAIL: test_api_tasks_returns_within_budget_on_large_message_history
  AssertionError: 4586.7 not less than 3000 : /api/tasks 花了 4587 ms，超过 3000 ms 预算
FAIL: test_group_names_query_is_not_per_row
  AssertionError: 4373.1 not less than 2000 : group_names 花了 4373 ms
```

重复与对照：

```text
df2007e 导出目录，单跑 tests.test_tasks_latency 三次：
  第 1 次 EXIT=1  4568 ms / 4633 ms
  第 2 次 EXIT=1  4892 ms / 4843 ms
  全量那次        4587 ms / 4373 ms
父提交 df2007e^ 导出目录，同两条：EXIT=1  4409 ms / 4110 ms  (Ran 2 tests)
```

判定：**不是本批引入**（父提交上一样红，数值同量级），也不是临界抖动（重复三次都在 4.1–4.9 秒，预算是 3000 / 2000）。本机 32 逻辑核、磁盘队列 1、无 python 满载进程；同一台机器上另外 1655 条用例全过。结论是这台机器在「大消息历史下的 /api/tasks 与 group_names」这条路径上比作者机器慢约一倍，**所以「门禁全绿」这个结论在本机不成立**，但用例本身没有断言错误。

### 未做 / 存疑

1. **多图消息的歧义 404 还没解决**：同一条消息留了 2 张以上图时，`_inbox_image_of()`（`webapp.py:2696`）判 `len(candidates) != 1` 就抛 `OSError` → 前端只看到 404、看不到任何一张。已派后端4 改。
2. **`qq_live_digest/ics.py` 与 PWA manifest 里还有旧名**「群消息待办」没换（本批只换了 `page.html:13`、`webapp.py:213`、`:1141` 三处标题）。已派后端4 改。
3. **`tests/test_webapp.py:4342` 的断言判别力不够**：现在这条断言在行为退化时也会通过。已派前端收紧。
4. **12 条命名违规仍未改**（`removeManual`/`removeManualTimer` 用禁用动词 `remove`；测试里 9 条超 24 字符 + `helperModule` 伪缩写 + `makeTarget` 用禁用动词 `make`），行号见本文件「更正第 2、3 批」的补充 4。留到收口、打完包后单独开 rename 提交。
5. **`docs/progress.md` 本身是这一批提交进去的**：所以本批记录在提交 `df2007e` 里并不存在（它记的是第 1–3 批 + 更正段），本条第 4 批是提交之后追加的。

---

## [2026-10-31 03:39] 更正第 4 批「验证」的结论 · 门禁假红是并发负载，不是机器慢

同上次做法：只追加，原文不回改。

### 更正内容

- 原文（第 4 批 · 验证）判定为：**「这台机器在「大消息历史下的 /api/tasks 与 group_names」这条路径上比作者机器慢约一倍，所以『门禁全绿』这个结论在本机不成立」**。
- 该判定**不准，撤回**。真实原因是**当时这台机器上有 4 条全量门禁同时在跑**（`unittest discover -s tests -t .`，来自不同 Bot，随后由开发负责人全部终止：PID 72836 / 72932 / 86176 / 99776）。

### 证据

1. **同一台机器、同一条命令的耗时对比**：本批这次 `755.976s`，空闲时同一条为 `460s`，慢 64%。机器本身没变，变的是并发。
2. **`tests.test_tasks_latency` 是按秒计的预算**：`/api/tasks` 预算 3000 ms、`group_names` 预算 2000 ms。4 倍负载下必然超，与产品逻辑无关。
3. **我自己的现场输出就带着这 4 个 PID**：判定「无 python 满载进程」时打印的是 `python pid=72836 / 72932 / 86176 / 99776`，我看见了却没归因，错在把「CPU 秒数低」当成「空闲」——那几条当时在早跑阶段或阻塞在 IO 上。

### 更正后的结论

- 这两条预算用例**对机器负载敏感**，不是产品问题，也不是本机性能不足。
- **验收用的全量门禁必须在无并发测试时跑**，否则会得到假红。跑之前先确认没有别的 `unittest discover` 在跑。
- 原文那条「父提交 `df2007e^` 上也一样红」作为现象仍成立，但**不能作为「历来如此」的证据**：那次父提交验证同样发生在上述并发负载期间。

### 经验（记给自己）

判定「慢」之前先查有没有别的测试在跑，命令：

```text
Get-CimInstance Win32_Process -Filter "Name like 'python%'"
```

只看 CPU 秒数不够——短命或 IO 阻塞的进程 CPU 秒数也很低。更硬的判据是看 `CommandLine` 里有没有 `unittest discover`，以及全量的总耗时是否明显偏离基线（本机基线 460s）。

### 未做 / 存疑

- 本次没有重跑测试（按要求）。所以「空闲状态下 `Ran 1657 tests / OK / EXIT=0`」这一条**尚未由我实测**，只有作者自述与开发负责人给的 460s 基线；收口时要我在无并发时跑一次才算数。
- 本文件第 4 批那条「验证」段落的原文保持不动（含已撤回的判定），以本节为准。

## [2026-10-31 19:05] 合并 rewrite/clean-room 到 merge/clean-room-1031 · 5 文件 8 处冲突

### 要求来源

开发负责人派工：`origin/rewrite/clean-room` 合到一半，冲突是预期的、不许 abort。方案 B 已定——实现留 `noticehub/`（domain/application/ports/adapters/infrastructure/bootstrap），旧文件只做再导出兼容层；我方 `50dfe40` 相对对方基线 `f602860` 的 5 个文件改动（attachments +207、config +47、store +11、webapp +575、main 1 行文案）不能在冲突里丢掉。

### 改了什么

- `main.py:64`：取对方（整段命令函数删除，已搬进 `noticehub/bootstrap/cli.py`）；我方那行文案补进 `noticehub/bootstrap/cli.py:205`，去掉「即可」→「把它发给我们；包内已脱敏，不含密钥、Cookie 或消息内容。」
- `qq_live_digest/attachments.py:19/:980`：取对方（解析器 + `AttachmentWorker` 从 noticehub 再导出）；我方图片能力实现搬进 `noticehub/application/attachments.py`：`_keep_image`（`:251`，附件 key 前缀 + 普通消息 sha1 前缀硬链接）、`IMAGE_MAX_BYTES`/`IMAGE_TYPES` 常量与 `image_dir`/`image_key`/`image_suffix`/`save_image`/`images_of`/`_image_index`/`_image_candidates`/`pick_image`/`has_image`/`delete_images`（`:300-487`）；旧文件 import 段一次再导出全部名字（`qq_live_digest/attachments.py:970`）。
- `qq_live_digest/config.py:441`：取对方（`from_env` → `build_settings`）；`web_port_source`/`warn_if_env_overrides_dotenv_port` 搬进 `noticehub/bootstrap/settings.py`（并保留 `qq_live_digest.config` 日志频道名），`build_settings` 两处加载 .env 前调用告警；旧文件顶部再导出。
- `qq_live_digest/store.py:1`：取对方 shim（`Store`/`SCHEMA`/`SCHEMA_VERSION`/`IMPORTANCE_BY_CATEGORY`/`TASK_STATUSES` 等全量再导出）。
- `qq_live_digest/webapp.py:58`：取我方，保留 `delete_images, image_key, pick_image` 与 `web_port_source`，删掉解完后已无人使用的 `from .ics import render_calendar`；`:4023`/`:4064`：取对方，改调 `start_history_backfill`/`start_inbox_classify` 用例，409 文案随之变成「已有回溯任务在进行 / 已有通知提取任务在进行」（测试没锁旧文案，无需改断言）。
- 合并后追补（门禁抓到的两处我方功能丢失）：`noticehub/infrastructure/sqlite/store.py:1143-1155` 补回「手工新建（`manual:*`）且 open 的任务可直接删除」例外（SELECT 同时取 `task_key`）；`:48-51` 补回覆盖索引 `idx_messages_group_names`。

### 接线

- 全仓 grep `from qq_live_digest.(attachments|config|store) import` 逐个核对：`Settings`/`Store`/`IMPORTANCE_BY_CATEGORY`/`effective_urgent`/`SCHEMA_VERSION`/`AttachmentWorker`/`IMAGE_MAX_BYTES`/`image_key`/`images_of`/`has_image`/`pick_image`/`delete_images` 都能从旧路径取到。
- 冒烟：`import noticehub.config, qq_live_digest.attachments, qq_live_digest.webapp, main` 通过；`web_port_source(None) == "dotenv"`。
- `qq_live_digest/*` 不留实现副本：attachments 只剩解析器 + 再导出，store/config 为纯兼容层。

### 验证

- 定向：`tests.test_config tests.test_store tests.test_attachments tests.test_application_tasks` → `Ran 137 tests / OK`（首次红在 logger 名，改回 `qq_live_digest.config` 频道后转绿）。
- 首次全量门禁：`Ran 1767 tests / FAILED (errors=1)`，唯一红 `test_webapp.TaskApiTest.test_delete_manual_quiet`（手工待办删除 404）——正是丢失的 `manual_open` 例外；修复后该用例与其反面用例 `Ran 2 tests / OK`。
- 最终全量门禁（当前 HEAD，两次，每次跑前确认无 `unittest discover` 并发）：
  - `GATE_EXIT=0`，`Ran 1767 tests in 412.335s / OK`
  - `GATE_EXIT=0`，`Ran 1767 tests in 441.225s / OK`

### 未做 / 存疑

- 未 commit / 未 push / 未打包 / 未重启实例：提交与后续目录重构由开发负责人做（`a0fdc94` 合并提交、`cb2d8a5` 追补都已由其落地）。
- 跑门禁时机器上有 2 个 python：用户自己的 `TotK-Setup` 安装器（`D:\TotK-Recomp\...`），不是测试进程；两次全量均无并发 `unittest discover`。
- 存疑：当前暂存区把所有模块从 `qq_live_digest/` 移到 `noticehub/` 顶层（旧包名消失），我按派工只保留了「旧路径再导出」，没参与这次目录重构；若确定不再需要旧路径兼容，shim 可以再删，需开发负责人拍板。