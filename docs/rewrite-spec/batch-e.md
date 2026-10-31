# 批次 E 行为规格（服务编排）

只描述**行为与契约**。实现者不看旧代码、只照本文写，扫出来的程序必须能与现有 `store` / `summarizer` /
`push` 三个模块拼接运行，并通过 `tests/test_service.py`。

覆盖一个类：`DigestService`（文件 `noticehub/service.py`）。它不自己解析消息、不自己调 HTTP 通道，
只负责**编排**：收消息 → 落库去重 → 攒窗口 → 出摘要 → 投递 → 记账，外加定时任务、历史补采、
收件箱规则、以及持有 Web 服务器。

---

## 0. 通用约定

- 时间一律本地 naive、秒精度（`YYYY-MM-DD HH:MM:SS`）；「今天/明天」按当地日期算。
- 时钟注入方式：公开方法带 `now: datetime | None = None`，缺省取服务时钟 `now_local()`；**测试会传固定时间**，所以实现里所有「现在」都必须走这个参数，不许直接调系统时钟。
- 自定义异常继承 `RuntimeError`；本类对外基本不抛业务异常（见 §6「失败语义」）。
- 日志通道名 = 模块名（`noticehub.service`），可由构造参数 `logger` 覆盖。
- 推送结果对象有 `channel`、`target`、`ok`、`skipped`、`error`、`attempts`、`status` 七个字段；`skipped=True` 表示去重跳过（不是失败）。

---

## 1. 组件装配与构造

- **E-01** 构造函数签名：`DigestService(settings, *, store=None, logger=None, bot=None, receiver=None, pushers=None)`。除 `settings` 外**全部关键字注入**。
- **E-02** 构造第一步先准备好数据目录（`ensure_dirs(settings)`）。
- **E-03** `store` 缺省时自建：路径 `settings.data_dir / "digest.sqlite3"`，保留期用 `settings.message_retention_days`。
- **E-04** `logger` 缺省用模块 logger；`bot`/`receiver`/`pushers` 为「注入即锁定」：给了 `pushers` 就**永不重建**（测试用注入的假推送器统计调用次数）。
- **E-05** 构造时初始化两张「扫描状态」字典（钉钉、微信），键固定七个：`scan_state`、`scan_stage`、`scan_started_monotonic`、`scan_started_at`、`last_read_at`、`last_read_seconds`、`last_error`；开关打开时 `scan_state="idle"`、`scan_stage="等待扫描"`，关闭时 `"disabled"`/`"未启用"`；已有值不覆盖，除非当前不是 `running`。
- **E-06** 构造后必须存在的公开状态字段（测试与健康检查直接读）：`started_at`、`last_tick_at`、`last_catchup_at`、`last_catchup_attempt_at`、`catchup_state`、`last_catchup_ok_at`、`last_catchup_error`、`last_llm_error`、`last_llm_error_at`、`last_defer_reason`、`last_defer_at`、`last_push_at`。
- **E-07** 懒加载属性：`bot`、`receiver`、`attachment_worker`（首次访问才构造，之后复用同一实例）；`push_manager()` 是**方法**不是属性，供诊断页与「发送测试」复用同一套通道与去重表。
- **E-08** 推送管理器构建规则：已有实例且通道未被锁定且机器人 API 可用时，若通道列表里没有 `qq-bot` 则重建一次（机器人上线后补上 QQ 通道）；`pushers` 被注入时永不重建。

---

## 2. 公开方法契约

- **E-09** `on_message(record: dict) -> bool`：返回「是否新插入」。重复 `msg_id` 返回 `False` 且**不**跑规则；来源白名单判定按来源分别做（钉钉/学习通/微信各自用自己那组开关与白名单；其它来源用通用群白名单，`group_id` 为空视为放行）；`group_name` 为空时用配置里的别名回填。
- **E-10** `on_message` 的容错：入库异常只记 `消息入库失败：{msg_id}` 并返回 `False`，**绝不**把异常抛给采集线程；新插入成功时先记 `收到消息 group=… sender=… id=… text=…`（正文截 60 字、换行换空格），再执行「自动整理规则」。
- **E-11** 自动整理规则（读规则文件 → 匹配 → 命中就建待办）：开关关着直接返回；规则文件坏了只记异常、按未命中处理；命中后建待办，任务键为 `rule:{规则 id}:{msg_id}`（同一条消息重复命中**只留一条**任务），状态 `candidate`，来源 `rule`，`classification_reason="规则 {id} 命中"`；建待办失败也只记异常。规则执行不得影响收消息的返回值。
- **E-12** `start(*, start_bot=True) -> None`：重复调用（`started_at` 非空）直接返回，不重复起线程/服务器。
- **E-13** `start` 的启动顺序：记 `started_at` → 清停止事件 → 官方机器人（需开关 + AppID + Secret，缺一记日志、不阻断）→ OneBot 接收器（启动失败只记 `OneBot 接收器启动失败：{错误}`）→ 钉钉读取器（需开关 + 非空白名单）→ 学习通读取器（需开关 + Cookie）→ 微信读取器（需开关 + 非空白名单）→ 附件后台队列（需开关）→ Web 服务器（需开关）→ 历史补采（需开关）→ 监督线程 → 记 `服务已启动，窗口 {N} 分钟，轮询 {M} 秒。`
- **E-14** Web 服务器必须拿到五个回调：`reload_chaoxing`、`reload_dingtalk`、`reload_wechat`、`dingtalk_reader_provider`、`wechat_reader_provider`、`start_scan`；其中 `start_scan` 必须是**安全**的：来源未知、未启用、白名单为空、读取器为空时一律返回 `False` 而不抛异常。
- **E-15** `stop() -> None`：置停止事件 → 等补采线程最多 5 秒 → 停附件队列 → 停 Web → 等监督线程最多 5 秒 → 停机器人 / 接收器 / 三个读取器（每处异常只记日志，不中断后续停止）→ `started_at=None` → 记 `服务已停止。`。停止后 `status()` 的 `started_at` 必须是空串。
- **E-16** `run_forever(poll_seconds=None) -> None`：轮询间隔取 `max(5, poll_seconds or settings.poll_seconds)`；先 `start()`，然后「等间隔 → tick」循环，单轮 tick 异常只记 `调度轮询失败` 并继续；收到键盘中断记 `收到中断信号，正在停止。`；退出时必调 `stop()`。
- **E-17** `tick(now=None) -> list[Digest]`：返回**本轮真正产出的摘要**（含静默归档那一条）。顺序与提前返回点见 §3。
- **E-18** `run_catchup(*, force=False, now=None) -> int`：返回本轮补采插入的消息条数；状态机见 §5。
- **E-19** `import_existing_tasks(*, days=7) -> int`：把历史摘要里的行动项回填成待办，返回新建任务数；重复执行安全（同一任务键不重复建）；已过期的条目跳过，同一条消息若有多个事件取「还没过期且最早」的那个。
- **E-20** `health_payload() -> dict` 与 `status() -> dict`：字段集合见 §6；两者都必须能直接 JSON 序列化（时间字段一律字符串，空状态给空串）。
- **E-21** `start_scan(source) -> bool`：只接受 `dingtalk` 与 `wechat`；未启用/白名单空/读取器不存在返回 `False`；有读取器时调用其 `start_scan()` 并转成布尔。
- **E-22** `reload_dingtalk_reader()` / `reload_chaoxing_reader()` / `reload_wechat_reader()`：未启动（`started_at` 为空）时直接返回；否则先停旧读取器再按 E-13 的条件重建；钉钉与微信重建时同步刷新 E-05 的扫描状态。
- **E-23** `push_manager()`：返回与投递流程同一个实例（不是新对象），未构建时按 E-08 构建。

---

## 3. 一轮 `tick` 的完整顺序（实现者照抄这一步）

- **E-24** 记 `last_tick_at = now`。
- **E-25** 补采调度：调补采「是否该跑」的判断（§5）；若当前在「等引擎」且机器人已就绪，立刻转为立即补采。
- **E-26** 截止提醒 → 每周复盘 → 候选确认提醒，三个定时任务依次执行（各自自带开关与「每天/每周只做一次」的键，见 §4）。
- **E-27** 维护：按周期做数据裁剪，再做数据库快照（同一天只做一次）。
- **E-28** 取待处理消息：`store.unprocessed_messages(limit=settings.max_batch)`。
- **E-29** 若取到了消息、而补采状态是「等引擎」或「失败」，说明引擎肯定上线了（能收到实时消息），立刻触发一次补采。
- **E-30** 没有待处理消息：执行一轮「挂起投递重试」后返回空列表。
- **E-31** 有待处理消息：先做分析（`summarizer.analyse_records`），取最近去重窗口的历史条目（`store.recent_item_texts(hours=settings.dedupe_hours)`），再算紧急条目（`summarizer.urgent_analyses(..., now=now)`）。
- **E-32** 有紧急条目：按这些消息 id 取子集 → `summarizer.build_digest(settings, 子集, kind="urgent", now=now, history=历史)` → 投递（§4 的 `_publish`）→ 结果加入返回值 → 从待处理里剔除这批；若已空，重试一轮挂起投递后返回。
- **E-33** 即时群：挑出属于「立即推送群」的消息；有的话先过推送闸门（§4）。放行则按 `kind="window"` 出摘要并投递、剔除这批（空则重试后返回）；不放行则整批挂起（记原因）继续往下走。
- **E-34** 窗口判定：取剩余消息里最早 `received_at` 算已等待秒数；窗口时长 = `settings.window_minutes × 60`；强制刷新的条件是「剩余条数 ≥ `settings.max_batch`」或「已等待 ≥ 窗口时长 × **6**」。
- **E-35** 未到窗口且未强制：**直接返回**（注意：这一步**不**做挂起投递重试）。
- **E-36** 到点后先过推送闸门；不放行则挂起整批、做一轮挂起投递重试、返回。
- **E-37** 放行则出窗口摘要：**有条目**→ 投递；**没条目**→ 写一条 `kind="silent"` 的摘要（正文空、payload 空、`llm_used=0`）、把消息标已处理、记 `窗口内 {N} 条消息无重点，静默归档（未推送）。`
- **E-38** 追加本轮产物 → 做一轮挂起投递重试 → 返回。
- **E-39** 幂等：同一批消息被连续 `tick` 两次，第二次的返回值必须是空列表、推送次数不增加、`counts()["unprocessed"]` 归零。靠「先归档摘要再标已处理」的顺序保证；`mark_processed` 幂等（重复标记不报错）。
- **E-40** 投递失败不阻塞标记：消息在投递之前就已经标成已处理并归档，所以推送失败只影响「用户有没有收到」，不导致下轮重复推送；重发由投递表的重试负责（§4）。

---

## 4. 投递、闸门与三条定时任务

### 4.1 推送闸门

- **E-41** 闸门返回「放行/不放行 + 原因」。紧急推送**无条件放行**（绕过免打扰与预算）；非紧急先看免打扰时段（`settings.in_quiet_hours(now)`），再看待发预算：当日已发数 ≥ `settings.push_daily_budget`（>0 时生效）则拦下。
- **E-42** 当日计数存在元信息键 `push_budget:{YYYY-MM-DD}`；只有「真的发出去了」才 +1（`ok and not skipped`），并刷新 `last_push_at`。
- **E-43** 挂起（不放行）时记 `last_defer_reason` = `quiet_hours` 或 `daily_budget`、`last_defer_at` = now，并记日志 `本批 {N} 条消息暂不推送（{原因}），留到下次窗口。`；消息**保持未处理**，下轮继续尝试。
- **E-44** 截止提醒与每周复盘忽略免打扰（`ignore_quiet`），候选确认提醒**不**忽略。

### 4.2 `_publish`（内部唯一投递入口）

- **E-45** 空摘要：写 `silent` 摘要（正文空）、标已处理、记 `本次 {N} 条消息无可推送内容（重复或无需动手），静默归档未推送。`、返回 `False`。
- **E-46** 有 LLM 错误：记 `last_llm_error`（截 400）与时间、`llm_failures_total` +1；可重试错误先问「推迟还是降级」（§4.3），推迟则 `llm_deferred_total` +1、挂起本批（原因文本 `大模型失败，稍后重试：{错误}`）、返回 `False`；否则 `llm_fallbacks_total` +1、清推迟计数，并按可重试与否记 error/warning（文案见 §7）。
- **E-47** 落库：`store.insert_digest(kind, window_start, window_end, body, payload=摘要自带的 payload 列表, message_count, llm_used)`，再 `store.mark_processed(message_ids, digest_id)`。**顺序不能反**。
- **E-48** 同步待办：在独立 try 里调「把摘要条目写成待办」；**任何异常都只记 error**（`摘要 #{id} 任务同步失败，本批摘要照常投递；可用 import_existing_tasks 从摘要补建任务。`，带堆栈）并把新增任务数当 0，继续投递。测试会强制让这一步抛异常并断言仍然推送成功。
- **E-49** 富文本：`settings.html_push` 为真时用 `summarizer.html_push_text` 现算一次 HTML 覆盖摘要上的 html。
- **E-50** 无通道：`push_manager().has_channels` 为假 → 记 error `没有可用推送通道，摘要仅归档到数据库（digest_id={id}）。`、返回 `False`（摘要已在库里）。
- **E-51** 投递：调 `push_manager().send_digest(digest, dedupe_key=传入的键)`；统计成功（`ok and not skipped`）、失败（`not ok`）、静默跳过；成功过就记一次当日推送；最后记 `摘要 #{id}（{kind}）投递：成功 {a}，失败 {b}，静默跳过 {c}，新增任务 {d}`；对每条失败记 `待重试 {channel}:{target} -> {error}`；返回「是否至少有一个通道成功」（静默跳过不算成功）。

### 4.3 LLM 推迟决策

- **E-52** 只有**可重试**的 LLM 失败才可能推迟；鉴权/参数这类不可重试错误直接降级推送，避免卡住用户。
- **E-53** 推迟规则：`settings.llm_defer_max_attempts <= 0` 时永不推迟。窗口 `llm_defer_window_minutes`（至少 1 分钟）内：第一次失败记 `llm_defer:started=now`、`llm_defer:count=1` 并推迟；窗口过期同样重置为 1 并推迟；窗口内已累计到上限则降级。
- **E-54** 降级后必须清零推迟计数（`llm_defer:started` 空、`llm_defer:count=0`），避免影响后续批次。

### 4.4 三条定时任务（每轮 `tick` 都调，自带去重键）

- **E-55** 截止提醒（`deadline_reminders_enabled`）：两个时刻 `deadline_morning`、`deadline_evening`；当前时间未到该时刻则跳过；键 `deadline_reminder:{morning|evening}:{YYYY-MM-DD}`，已写则跳过。
- **E-56** 晚开机规则：早上那次若已经过了晚上时刻（即一开机就是晚上），**不补发**早上提醒，直接把早上的键写掉。
- **E-57** 取待提醒事项：只取开放任务（`store.list_open_tasks(limit=2000)`）；早上看「截止日期 ≤ 今天」（含逾期），晚上只看「截止日期 = 明天」；跳过今天已提醒过的、跳过「用户点了稍后提醒且未到点」的；按截止时间升序取前 **5** 条（常量 `DEADLINE_MAX_ITEMS=5`）。
- **E-58** 无通道时不写键（否则当天提醒被永久吞掉），并按「状态变化或距上次 ≥30 分钟」限流地记 `截止提醒无可用推送通道，保留待下次重试。`；测试要求连续 5 次调用内这类警告不超过 2 条。
- **E-59** 过闸门（忽略免打扰）→ 组装摘要（`kind=morning|evening`，窗口起止都是 now，`message_count` = 条目数）→ 用去重键 `deadline:{kind}:{YYYY-MM-DD}` 投递；失败记 `截止提醒投递失败，保留待下次重试。` 且不写键。
- **E-60** 成功后写键、对每条事项 `store.mark_task_reminded(task_id, detail={"kind": kind, "deadline": …}, when=now)`，并记 `已推送{标题}，涵盖 {N} 条截止事项。`（标题分别是「今日截止」「截止提醒」）。
- **E-61** 候选确认提醒（`candidate_push_enabled`）：每日上限 `candidate_push_max_per_day`（键 `candidate_push:{YYYY-MM-DD}`），每轮上限 `candidate_push_max_per_tick`；只取 `candidate` 状态、置信度 ≥ `candidate_min_confidence`、未被稍后提醒、距上次提醒 ≥ `candidate_reminder_hours` 小时的任务（最多看 30 条）。
- **E-62** 候选提醒的正文本固定五行：`可能要做：{摘要}` / `来源：{群名或「来源群未知」}` / `推测截止：{截止或「未识别」}` / `依据：{证据}` / `打开待办台确认：{链接}`；卡片标题 `有一条通知可能需要处理`，通知栏摘要 `待确认：{摘要}`；HTML 卡片含「待确认」徽标与「在待办台确认或忽略」链接。
- **E-63** 候选提醒的记账：投递成功**或**被判定为「之前已经发过」（`skipped` 且状态为已发）都算触达，都要 `mark_task_reminded(..., detail={"kind": "candidate_confirmation", "confidence": …})`，并计入本轮与当日额度；只有真的发出去了才记一次当日推送计数。
- **E-64** 每周复盘（`weekly_review_enabled`）：只在 `weekly_review_weekday`（0=周一）且时间 ≥ `weekly_review_time` 时触发；键 `weekly_review:{ISO 年}-{ISO 周}`；正文三行固定：`新增候选 {n} 条，确认 {n} 条，忽略 {n} 条` / `完成 {n} 条，提醒 {n} 次，提醒后完成 {n} 条` / `确认率 {x}%，忽略率 {x}%，逾期未完成 {n} 条`；再接最多三条 `纠错提示：{文本}`（来自 `store.correction_insights(days=7, min_group_hits=2)`）；指标来自 `store.task_metrics(start=7 天前零点, end=now)`。
- **E-65** 每周复盘无通道记 `每周复盘无可用推送通道，保留待下次重试。`；闸门拦截记 `每周复盘暂不推送（{原因}），保留待下次重试。`；全部通道失败记 `每周复盘投递失败，保留待下次重试。`；成功才写键并记 `已生成每周复盘。`。
- **E-66** 默认值下这三条任务可能在同一轮 `tick` 里各推一条（晚上 21:00 之后同时命中截止提醒与周日晚的复盘），实现不得互相吞掉。

---

## 5. 历史补采与维护任务

- **E-67** 补采状态机只有五个值：`idle`（尚未补采）、`running`（正在补采）、`waiting_engine`（引擎未就绪）、`failed`（上次失败）、`ok`（成功过）。
- **E-68** `run_catchup` 一开始就记 `last_catchup_attempt_at = now`；先用引擎探针判断「可达且已登录」（读状态接口返回里的登录布尔），不就绪 → 状态转 `waiting_engine`、记下原因、**不抓取、不计失败、不推进退避**、记 `历史补采暂不执行：{原因}，引擎上线后自动补采。`、返回 0。
- **E-69** 就绪 → 状态 `running` → 调补采本体（传入设置、库、日志、客户端、now、force、统计字典、附件回调）；抛异常 → 状态 `failed`、错误文本 `历史补采异常`、记异常、返回 0。
- **E-70** 抓取结果判定：`groups > 0 且 ok_groups == 0` 视为**全部失败** → 失败计数 +1、状态 `failed`、错误文本 `历史补采全部失败（{N} 个群）`、记 `历史补采全部失败（{N} 个群），{M} 分钟后自动重试。`、返回 0。
- **E-71** 否则算成功：失败计数归零、`last_catchup_at` 与 `last_catchup_ok_at` = now、错误清空、状态 `ok`、返回插入条数。
- **E-72** 退避阶梯按「失败次数」取下标：60 / 120 / 300 / 600 秒，600 封顶（4 次及以上都是 600）。
- **E-73** 是否该补采的判定：未启用 → 不跑；`running` → 不重入；`waiting_engine` → 距上次尝试满 **20 秒**就跑（不看失败退避）；其余情况：有成功记录时按 `catchup_interval_minutes`（至少 5 分钟）间隔；没有成功记录时按退避阶梯判断。
- **E-74** 「引擎刚可用」：只有需要补采且状态是 `waiting_engine` 或 `failed` 时才动作——失败计数归零、状态转 `idle`、立即起一次补采。
- **E-75** 补采线程同一时刻最多一个：起线程前检查已有线程是否存活。
- **E-76** 数据裁剪：键 `last_prune`，间隔 6 小时；先 `store.prune()`，再清理过期附件文件（保留 `attachment_retention_days` 天）；有删除时分别记 `清理 {N} 条过期消息。`、`清理 {N} 个过期附件文件。`。
- **E-77** 数据库快照：键 `last_snapshot` 存 `YYYYMMDD`，同一天只做一次；用 SQLite 在线备份写到 `data_dir/snapshots/digest-{YYYYMMDD}.sqlite3`，只保留最近 **7** 份；任何 IO/SQLite 失败只记 `数据库快照失败（{路径}）：{错误}` 并返回（不写键，下次再试）。

---

## 6. 外部依赖失败语义与状态载荷

- **E-78** 推送通道失败：不在本类重试同一通道；失败记入投递表（`failed`），本轮结束前调一次「挂起投递重试」（同一轮内可重试成功，取决于通道冷却 `delivery_retry_seconds`）；重试次数上限与冷却由投递表控制（`delivery_max_attempts` / `delivery_retry_seconds`）。
- **E-79** 一轮里挂起重试的调用点：无消息时（E-30）、紧急/即时批处理完后（E-32/E-33）、闸门拦截后（E-36）、窗口批处理完（E-38）；**窗口未到时不做**（E-35）。重试成功记 `重试成功 {channel}:{target}（第 {n} 次）`，失败记 `重试失败 {channel}:{target} -> {error}`。
- **E-80** LLM 失败不会让本类抛异常：全部降级为「推迟本批」或「用本地结果照常推送」，并计入 `llm_failures_total` / `llm_deferred_total` / `llm_fallbacks_total` 三个计数。
- **E-81** HTTP/引擎失败只影响补采状态机，不影响实时消息处理。
- **E-82** `health_payload()` 固定键：`started_at`、`last_tick_at`、`catchup_enabled`、`attachments`、`tasks`、`web`、`last_catchup_at`、`last_catchup_attempt_at`、`catchup_state`、`catchup_waiting_engine`、`last_catchup_ok_at`、`last_catchup_error`、`catchup_failures`、`catchup_next_retry_at`、`catchup_hint`、`llm`、`push`、`counts`。
- **E-83** 补采健康提示文案固定（`catchup_hint`）：`waiting_engine`→`引擎还没上线，登录成功后会自动补采`；`running`→`正在补采历史消息`；`failed`→`上次补采失败，稍后自动重试`；`ok`→`历史消息已补采`；`idle`→`尚未补采`。`catchup_next_retry_at` 在「等引擎」时按 20 秒、在「失败」时按退避阶梯推算，其它情况空串。
- **E-84** `llm` 子字典固定七键：`enabled`、`model`、`failures_total`、`deferred_total`、`fallbacks_total`、`last_error`、`last_error_at`。
- **E-85** `push` 子字典固定八键：`channels`、`sent_today`、`daily_budget`、`quiet_hours`、`last_push_at`、`last_defer_reason`、`last_defer_at`、`deliveries`；`channels` 在还没实例化推送器时退回「配置里写了哪些通道」。
- **E-86** `status()` 至少包含：`started_at`、`last_tick_at`、`catchup_enabled`、`last_catchup_at`、`last_catchup_attempt_at`、E-83 的五个补采字段、`bot_alive`、`bot_ready`、`bot_error`、`receiver_alive`、`channels`（每项是 `{通道名}:{目标}` 形式的描述字符串）、`counts`、`tasks`。
- **E-87** 任务链接：`_task_url(task_id)` 拼成「基址 + `?token=…` + `#task-{id}`」；基址取元信息 `web_base_url`（去尾部斜杠），没有就用配置里的公开地址；令牌优先配置里的（`web_token`），其次元信息。示例（测试断言）：配置公开地址 `https://example.test` + 令牌 `secret` + id 7 → `https://example.test/?token=secret#task-7`。

---

## 7. 日志文本（运维按文本 grep，算契约）

通道名：模块 logger。以下文本**原样**保留（占位符按实际填）：

| 场景 | 级别 | 文本 |
|---|---|---|
| 收到新消息 | info | `收到消息 group={群} sender={人} id={id} text={前60字}` |
| 忽略无 id | warning | `忽略没有 msg_id 的消息：{记录前200字}` |
| 入库失败 | exception | `消息入库失败：{msg_id}` |
| 规则匹配失败 | exception | `规则匹配失败，本条消息按未命中处理：{msg_id}` |
| 规则建待办失败 | exception | `规则命中的消息建待办失败：{msg_id}` |
| 服务启动 | info | `服务已启动，窗口 {N} 分钟，轮询 {M} 秒。` |
| 服务停止 | info | `服务已停止。` |
| 机器人启动结果 | info | `官方机器人启动：{bool}` |
| 缺机器人凭据 | error | `缺少 AppID/AppSecret，官方机器人未启动。` |
| 官方机器人禁用 | info | `官方 QQ 机器人已禁用，仅使用 OneBot/NapCat。` |
| 接收器启动失败 | error | `OneBot 接收器启动失败：{错误}` |
| 附件解析启用 | info | `附件解析已启用：单文件上限 {N}MB、每小时最多 {N} 个、图片OCR={开/关}（{模型}）、扫描PDF限{N}页。` |
| 监督线程重启机器人 | warning | `检测到 QQ 机器人未运行（{原因}），尝试重启。` |
| 轮询异常 | exception | `调度轮询失败` |
| 键盘中断 | info | `收到中断信号，正在停止。` |
| 静默归档（窗口） | info | `窗口内 {N} 条消息无重点，静默归档（未推送）。` |
| 静默归档（空摘要） | info | `本次 {N} 条消息无可推送内容（重复或无需动手），静默归档未推送。` |
| 本批挂起 | info | `本批 {N} 条消息暂不推送（{原因}），留到下次窗口。` |
| LLM 可重试耗尽 | error | `大模型重试耗尽，本批 {N} 条消息回退本地规则推送：{错误}` |
| LLM 不可用（不可重试） | warning | `大模型不可用（{错误}），本批 {N} 条消息使用本地规则推送。` |
| 任务同步失败 | error | `摘要 #{id} 任务同步失败，本批摘要照常投递；可用 import_existing_tasks 从摘要补建任务。` |
| 无推送通道 | error | `没有可用推送通道，摘要仅归档到数据库（digest_id={id}）。` |
| 投递汇总 | info | `摘要 #{id}（{kind}）投递：成功 {a}，失败 {b}，静默跳过 {c}，新增任务 {d}` |
| 单通道失败 | warning | `待重试 {channel}:{target} -> {error}` |
| 重试成功 | info | `重试成功 {channel}:{target}（第 {n} 次）` |
| 重试失败 | warning | `重试失败 {channel}:{target} -> {error}` |
| 截止提醒无通道 | warning | `截止提醒无可用推送通道，保留待下次重试。` |
| 截止提醒被闸门拦 | info | `截止提醒暂不推送（{原因}），保留待下次重试。` |
| 截止提醒投递失败 | warning | `截止提醒投递失败，保留待下次重试。` |
| 截止提醒成功 | info | `已推送{标题}，涵盖 {N} 条截止事项。` |
| 候选提醒被闸门拦 | info | `候选确认提醒暂不推送（{原因}）。` |
| 每周复盘无通道 | warning | `每周复盘无可用推送通道，保留待下次重试。` |
| 每周复盘被拦 | info | `每周复盘暂不推送（{原因}），保留待下次重试。` |
| 每周复盘失败 | warning | `每周复盘投递失败，保留待下次重试。` |
| 每周复盘成功 | info | `已生成每周复盘。` |
| 补采不执行 | info | `历史补采暂不执行：{原因}，引擎上线后自动补采。` |
| 补采异常 | exception | `历史补采异常` |
| 补采全部失败 | warning | `历史补采全部失败（{N} 个群），{M} 分钟后自动重试。` |
| 清理消息 | info | `清理 {N} 条过期消息。` |
| 清理附件 | info | `清理 {N} 个过期附件文件。` |
| 快照失败 | warning | `数据库快照失败（{路径}）：{错误}` |

---

## 8. 配置键（默认值固定）

| 键 | 默认 | 语义 |
|---|---|---|
| `max_batch` | 400 | 单轮最多处理的消息数 |
| `window_minutes` | 10 | 普通消息攒批窗口（分钟） |
| `poll_seconds` | 20 | 轮询间隔（秒），下限 5 |
| `immediate_groups` | 空 | 不等窗口、立即推送的群 |
| `quiet_hours` | `23:00-07:00` | 免打扰时段 |
| `push_daily_budget` | 12 | 每日推送条数上限（0 = 不限） |
| `dedupe_hours` | 6 | 跨群去重窗口（传给摘要） |
| `html_push` | 真 | 是否生成 HTML 卡片 |
| `delivery_retry_seconds` | 90 | 投递失败冷却 |
| `delivery_max_attempts` | — | 投递最大尝试次数（投递表用） |
| `llm_defer_max_attempts` | 3 | 可重试 LLM 失败最多推迟几次（≤0 不推迟） |
| `llm_defer_window_minutes` | 15 | 推迟计数窗口 |
| `deadline_reminders_enabled` | 真 | 截止提醒总开关 |
| `deadline_morning` / `deadline_evening` | `07:30` / `21:00` | 两个提醒时刻 |
| `candidate_push_enabled` | 真 | 候选确认提醒开关 |
| `candidate_push_max_per_day` | 4 | 候选提醒每日上限 |
| `candidate_push_max_per_tick` | 2 | 候选提醒每轮上限（≤0 关闭） |
| `candidate_reminder_hours` | 24 | 同一候选两次提醒的最小间隔（小时） |
| `candidate_min_confidence` | 0.55 | 低于此置信度不提醒 |
| `weekly_review_enabled` | 真 | 每周复盘开关 |
| `weekly_review_weekday` | 6 | 复盘日（0=周一） |
| `weekly_review_time` | `20:30` | 复盘时刻 |
| `catchup_enabled` | 假 | 历史补采开关 |
| `catchup_interval_minutes` | 30 | 补采间隔（至少 5） |
| `message_retention_days` | 30 | 消息保留天数 |
| `attachment_retention_days` | — | 附件保留天数 |
| `data_dir` | 项目内 `data` | 库、快照、附件根 |
| `web_enabled` / `web_token` / `web_base_url` | 真 / 空 / 空 | Web 服务器与任务链接 |

---

## 9. 测试锁死的事实（`tests/test_service.py`，52 个用例）

1. Web 服务器拿到的 `start_scan` 对 `dingtalk`/`wechat`/`unknown` 在未启用时都返回 `False`，且不抛异常。
2. `start_scan` 只认启用的来源：`unknown`→假、未启用来源→假、启用且白名单非空→调读取器 `start_scan()` 一次并返回真；两张扫描状态字典里都有 `scan_state`。
3. 规则命中：消息入库后建 1 条任务，摘要=正文、分类/重要度取规则 payload、来源 `rule`。
4. 规则不命中、开关关闭、规则文件损坏三种情况下：消息照常入库（返回真），但**一条待办都不建**。
5. 同一条消息反复触发规则：任务仍只有 1 条（任务键幂等）。
6. `_task_url(7)` 在配置公开地址与令牌后等于 `https://…/?token=secret#task-7`。
7. 窗口刷出：11 分钟前的一条通知 → `tick` 产出 1 条 `window`、推送 1 次、正文含正文关键词；紧接着再 `tick` 返回空列表、推送次数不变、未处理归零。
8. 摘要里能行动的条目写成 `open` 待办，摘要含动作短语，来源群名进 `groups`。
9. 事件补时间：LLM 没给截止时间时，从「明天下午3点半在A楼202开会」补出「明天 15:30」并把地点写进 details。
10. LLM 给了截止时间时以 LLM 为准，事件只补条件（`condition` 含地点），置信度不超过 0.80。
11. 只有地点没有时间（低置信事件）不得变成待办，且不得写 `deadline_dt`。
12. 历史回填：一条能补出未来时间的建成 1 条任务，另一条截止在 2020 年的整条跳过。
13. 同一事件合并两次，地点只保留一条（`["地点：A楼202"]`）。
14. 一条消息两个事件：截止取**最早**，地点全部保留（`地点：A楼202`、`地点：B楼301`）。
15. 「今天下午3点放假，停电通知」这类只到中置信、没有动作的事件**不**建待办（但 `deadline_dt` 确实被补上了，证明是门槛收紧而不是没抽到）。
16. 多事件中有一条已过期、一条在未来：按未来那条建任务，并保留其中地点。
17. 建任务抛异常时仍然推送成功：产出 1 条摘要、推送 1 次、未处理归零、待办为空。
18. 窗口未到：`tick` 返回空、未处理仍是 1；过 10 分钟后 `tick` 产出 1 条并推送。
19. 「立即推送群」里的消息不等窗口：当场产出 1 条 `window`、推送 1 次、未处理归零。
20. 纯闲聊：产出 1 条 `silent`、不推送、未处理归零、摘要表 1 条。
21. 紧急消息：当场产出 1 条 `urgent`、推送 1 次、正文含「截止」、条目分类为 `urgent`。
22. 低优先级群的闲聊：窗口未到时返回空；窗口到后产出 `silent`、不推送、**不建待办**。
23. 低优先级群的真通知：走 `window` 正常推送，并建 1 条待办。
24. 补采全失败：不写 `last_catchup_at`、写 `last_catchup_attempt_at`、状态 `failed`、失败计数 1；30 秒内不重试，61 秒后起线程。
25. 退避阶梯 60/120/300/600/600：不足不重试，满了才重试。
26. 补采成功：失败计数归零、状态 `ok`、`last_catchup_ok_at` 写到当前时间、错误清空、退避回到 60 秒。
27. 引擎未就绪：连续三次都不抓取、失败计数保持 0、不写 `last_catchup_at`、状态 `waiting_engine`、错误里含「引擎还没上线」。
28. 等引擎时按 20 秒短轮询：19 秒不跑、21 秒跑。
29. 引擎刚可用：失败计数归零并立即调度一次补采。
30. 状态载荷（`health_payload` 与 `status`）都暴露 `catchup_state`、`catchup_waiting_engine`、`last_catchup_error`、非空 `catchup_next_retry_at`，且 `catchup_hint` 含「引擎」。
31. 重复消息返回假、白名单外群返回假，库里只有 1 条消息。
32. 推送失败一次 + 冷却 0：同一轮内重试并成功，推送 2 次，投递统计 `sent=1`、`failed=0`。
33. 推送失败 + 冷却 3600：30 秒后不再重试、`failed=1`；待重试列表里 1 条；把冷却置 0 手动重试成功后推送次数变 2。
34. `status()` 含 `counts`，`channels` 为 `["webhook:test-target"]`。
35. `start(start_bot=False)` → `stop()` 之后 `status()["started_at"]` 为空串。
36. 截止提醒：早上 07:31 只推「今日截止」且只含今天的截止时间（不含明天）；08:00 再跑不重复推；21:00 推「截止提醒」且只含明天的截止时间。
37. 截止提醒无通道的警告限流：连续 5 次调用内「无可用推送通道」类警告不超过 2 条。
38. 22:00 才开机：只推晚上的「截止提醒」，不补发早上的「今日截止」。
39. 每周复盘：开关关着一条都不推，开着必推且正文含「新增候选」。
40. 模棱两可的行动（「记得明天可能要交报名表」）建成 `candidate`，置信度 < 0.75。
41. 候选提醒：第一轮推送失败不算触达（`last_reminded_at` 仍空、状态仍 `candidate`）；下一轮重试成功后推送计数到 2、`last_reminded_at` 有值、状态不变；2 小时后再跑不重复推（每日上限生效）。
42. 逾期任务会被提醒而忽略掉的不提醒：只推 1 条、含逾期那条、不含忽略那条；逾期那条 `last_reminded_at` 有值、被忽略那条 `remind_count` 为 0；同一天再跑不重复提醒。
43. 截止提醒无通道时**不写**当天去重键；补上通道再跑就推出去并写键。
44. 可重试 LLM 失败：第一次 `tick` 不推送、未处理仍是 1、`llm_defer:count=1`；失败恢复后下一轮推送成功、未处理归零、推迟计数归零。
45. 不可重试 LLM 失败（401）：仍然推送、未处理归零、`llm_fallbacks_total=1`。
46. 免打扰 23:00-07:00：23:30 不推、未处理仍是 1、`last_defer_reason="quiet_hours"`；次日 07:01 推出去、未处理归零。
47. 每日预算 1：第一轮推 1 条并写入 `push_budget:{日期}=1`；第二轮不推、未处理留 1、`last_defer_reason="daily_budget"`。
48. 紧急消息 23:30 照推（绕过免打扰）。
49. 钉钉：启用但白名单为空 → 不构造读取器；白名单非空 → 构造并 `start()` 一次，且 `_dingtalk_reader` 非空。

---

## 10. 需要拍板（测试没说清、实现前必须定）

- **E-D1** `tick` 没有进程内互斥：`run_forever` 与「手动触发」同时进来时，两轮都可能读到同一批未处理消息并各推一次。现在靠「先归档再标记已处理」把窗口压到很小，但不是零。是否要求加一把「同一时刻只允许一轮 tick」的锁（会改变并发语义，也会让长轮询里的人工触发排队）？
- **E-D2** E-35（窗口未到直接返回、不跑挂起重试）是有意设计还是遗漏？后果是：一条失败投递可能要等到下一批消息到点才被重试。若要求「每轮都重试」，需要改测试里对推送次数的断言。
- **E-D3** 静默归档要写摘要表（现有测试断言摘要表条数），等于把「没推送」也记成一次摘要。是否保留？保留则摘要列表会混入无内容条目。
- **E-D4** 候选提醒的「已发过」也计入每日额度（E-63）。若改为只算真发出，会放宽每日总量。
- **E-D5** 三条定时任务独立去重键，晚上 21:00 之后可能一轮出两条推送（截止提醒 + 周日晚复盘）。产品上是否接受「同一时刻两条推送」？
- **E-D6** LLM 推迟口径：15 分钟窗口内最多 3 次，超了降级。这两个数是否符合「用户宁可晚一点也要 AI 摘要」的预期？改成 0 就等于永不推迟。
- **E-D7** 补采「引擎就绪」的判定依赖外部状态接口返回的登录布尔字段。这是跨模块契约，重写时必须与采集层对齐（字段名、缺失时的语义）。
- **E-D8** 监督线程只重启官方机器人，不重启 OneBot 接收器与三个读取器。是否要一并接管？
- **E-D9** 停服顺序里补采线程只等 5 秒、监督线程只等 5 秒；超时后仍继续停其它组件。是否需要「等不到就强制中断」的语义？
- **E-D10** `status()` 的 `channels` 是「通道名:目标」描述串，含目标（如 webhook 地址片段）。要不要脱敏？
- **E-D11** 快照固定保留 7 份且与裁剪同节奏；磁盘吃紧的机器上是否需要可配置。
- **E-D12** 日志文本按 §7 视为契约，重写不得改写标点与占位顺序（运维按文本 grep）。确认这条不被「统一文案」的改动破坏。
