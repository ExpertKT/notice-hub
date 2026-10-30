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
