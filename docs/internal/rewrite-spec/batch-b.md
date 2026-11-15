# 批次 B 行为规格（clean-room 重写输入）

只描述**行为与契约**。实现者不读旧代码，照本文写出来的东西必须能通过现有测试，并且与用户已有的
`data/digest.sqlite3` 兼容。测试没锁死、又会影响兼容性的点，集中列在 §4「需要拍板」。

通用约定沿用批次 A：时间一律本地 naive、秒精度；自定义异常继承 `RuntimeError`；模块不读环境变量
（配置对象由外部注入）；日志文本属运维契约。

覆盖两个模块：

| 模块 | 职责 |
|---|---|
| `noticehub/store.py` | SQLite 持久化：消息去重、摘要归档、投递去重与重试、待办与纠错统计、群名缓存、收件箱判定、元信息与清理。**本模块是再导出层**，实现可以放在基础设施子包里，但这份公开面必须原样可用（常量、`Store`、`effective_urgent`、`IMPORTANCE_BY_CATEGORY` 等 8 个名字） |
| `noticehub/summarizer.py` | 把消息记录筛成「重点条目」、可选调 LLM 精炼、渲染三种推送文本（纯文本 / 通知栏一行 / HTML 卡片） |

---

## 1. `noticehub/store.py`

### 1.1 公开面（再导出清单，缺一不可）

`Store` 类、`effective_urgent(task) -> bool`、常量 `SCHEMA`、`SCHEMA_VERSION`（值 `1`）、`TASK_STATUSES`
（值集合 `{"candidate","open","done","dismissed","expired"}`）、`IMPORTANCE_BY_CATEGORY`
（值 `{"urgent":5,"action":4,"academic":3,"info":3}`）、`MESSAGE_COLUMN_MIGRATIONS`、
`TASK_COLUMN_MIGRATIONS`。

`IMPORTANCE_BY_CATEGORY` 必须是**唯一一份对象**：摘要模块与存储模块拿到的是同一个实例（有测试用
`is` 断言），任何一方复制字面量都算违约。

### 1.2 契约条目：库、连接与旁路文件

- **S-01** `Store(path, *, retention_days: int = 30)`：构造即建父目录；构造过程中**自动执行** `init()`，调用方不需要另外初始化。
- **S-02** 旁路审计文件固定在库文件同目录：消息审计 `messages.jsonl`、摘要审计 `digests.jsonl`。写入用追加模式，一行一个 JSON 对象（UTF-8、不转义中文）；写失败只记 WARNING `写入 JSONL 失败 {路径}: {错误}`，**不得**让主流程失败。
- **S-03** 每个公开方法各开一条 SQLite 连接（`timeout=15`），行工厂用「按列名取值」的行对象；连接建立后立即 `PRAGMA journal_mode=WAL` 与 `PRAGMA foreign_keys=ON`；方法结束提交并关闭。**不共享连接、不做连接池、不跨方法保持事务**。
- **S-04** 单方法即单事务：中途抛异常时不提交（由连接关闭回滚）。因此「先删父表再删子表」这类顺序必须显式安排（见 S-42）。
- **S-05** 同一进程内可多线程调用同一 `Store` 实例；外层靠 SQLite 的写锁与 15 秒忙碌等待串行，模块内部不加锁。跨进程同样安全（WAL）。`init()` 不是并发安全的初始化，调用方负责「启动早期单次调用」。

### 1.3 契约条目：表结构与索引（**对外契约，用户库里已有这些**）

- **S-06** 表 `messages`：`msg_id` 文本主键；`source` 文本非空默认 `'qqbot'`；`event` 文本非空默认空串；`group_id` 文本非空；`group_name` 文本非空默认空串；`sender_id`、`sender_name` 同上；`ts` 文本非空默认空串；`received_at` 文本非空、无默认；`content` 文本非空默认空串；`source_text` 文本非空默认空串。
- **S-07** 表 `messages` 的索引三条，名字与列顺序不可改：`idx_messages_received(received_at)`、`idx_messages_group(group_id, received_at)`、`idx_messages_group_names(group_id, ts, group_name)`。第三条是覆盖索引（`group_names()` 的三列，缺了它 6 万行的群名查询要几秒）。
- **S-08** 表 `processed`：`msg_id` 文本主键且外键引用 `messages(msg_id)`；`digest_id` 整数、可空；`processed_at` 文本非空。
- **S-09** 表 `digests`：`id` 整数主键自增；`kind` 文本非空；`window_start`、`window_end` 文本非空默认空串；`item_count`、`message_count` 整数非空默认 0；`llm_used` 整数非空默认 0；`body` 文本非空默认空串；`payload` 文本非空默认 `'[]'`；`created_at` 文本非空。
- **S-10** 表 `deliveries`：`id` 整数主键自增；`digest_id` 整数非空；`channel`、`target`、`dedupe_key` 文本非空；`status` 文本非空默认 `'pending'`；`attempts` 整数非空默认 0；`last_error` 文本非空默认空串；`created_at`、`updated_at` 文本非空；**唯一约束** `(dedupe_key, channel, target)`。
- **S-11** 表 `deliveries` 索引 `idx_deliveries_status(status, updated_at)`。
- **S-12** 表 `meta`：`key` 文本主键，`value` 文本非空。
- **S-13** 表 `tasks`：`id` 整数主键自增；`task_key` 文本非空且唯一；`summary`、`action`、`deadline`、`groups`（默认 `'[]'`）、`sender`、`evidence`、`audience`、`condition_text`、`details`（默认 `'[]'`）、`done_at`、`snooze_until` 均为文本非空带默认；`category` 文本非空默认 `'info'`；`importance` 整数非空默认 3；`status` 文本非空默认 `'open'`；`digest_id` 整数非空默认 0；`created_at`、`updated_at` 文本非空；`duplicate_of` 整数非空默认 0；`urgent_override` 整数、**可空**（三态：空=跟随 AI，0=非紧急，1=紧急）；`pinned` 整数非空默认 0。
- **S-14** 表 `tasks` 索引两条：`idx_tasks_status(status, deadline, importance)`、`idx_tasks_created(created_at)`。
- **S-15** 表 `task_events`：`id` 整数主键自增；`task_id` 整数非空（**不设外键**，删除任务时由代码显式清理）；`event` 文本非空；`detail` 文本非空默认空串；`created_at` 文本非空。索引 `idx_task_events_task(task_id, created_at)`、`idx_task_events_created(created_at)`。
- **S-16** 表 `inbox_marks`：`msg_id` 文本主键（**不设外键**到 messages，清理靠代码）；`verdict` 文本非空；`reason`、`method` 文本非空默认空串；`classified_at` 文本非空；`task_id` 整数可空（非空表示已转成待办）。索引 `idx_inbox_verdict(verdict, classified_at)`。
- **S-17** 表 `group_names`：`group_id` 文本主键；`name` 文本非空；`updated_at` 文本非空。
- **S-18** 表 `dingtalk_sync`：`key` 文本主键；`value` 文本非空；`updated_at` 文本非空。**两张同步状态（钉钉 WAL 游标、学习通已见 id）共用这一张表**，键名分别是 `wal_state` 与 `chaoxing_state`。
- **S-19** 建表语句必须全部带 `IF NOT EXISTS`（表与索引都要），且能在一个批次里重复执行不报错。
- **S-20** 库结构版本写在 `PRAGMA user_version`，当前值 `1`。

### 1.4 契约条目：`init()` 的幂等、迁移与坏库隔离

- **S-21** `init()` 每次都执行完整建表脚本（靠 S-19 的 `IF NOT EXISTS` 幂等补建），再对 `messages` 与 `tasks` 逐列比对补列，最后若 `user_version` 不等于 1 则写成 1。**不写版本迁移链、不建迁移表**。
- **S-22** 补列规则：按 `PRAGMA table_info` 读现有列名，缺哪列就 `ALTER TABLE ... ADD COLUMN`。`messages` 只补 `source_text`（文本非空默认空串）。`tasks` 补这些列（文本类默认空串）：`audience`、`condition_text`、`details`（默认 `'[]'`）、`source`、`confirmed_at`、`dismissed_at`、`last_reminded_at`、`snooze_until`；数值类：`confidence`（实数非空默认 0）、`remind_count`（整数非空默认 0）、`duplicate_of`（整数非空默认 0）、`pinned`（整数非空默认 0）、`urgent_override`（整数、**无默认即可空**）。
- **S-23** 老库迁移后：原有数据一条不少、老任务能查到、新列取到默认值（`confidence` 为 0.0）、`task_events` 表被补建。重复打开多次（连续构造多份 `Store`）不得报错、不得重置已写入的列值。
- **S-24** 版本过高保护：只读读到的 `user_version` 大于 1 时，抛 `RuntimeError`，文案原样：`数据库结构版本 {库版本} 高于本程序支持的 1，请升级 notice-hub 后再启动：{库文件绝对路径}`。**此时不得写库**。
- **S-25** 坏库检测：只读打开做 `PRAGMA integrity_check`；库文件不存在或大小为 0 视为「没坏」。只有结果不是 `ok` **且**错误文本包含 `not a database` / `malformed` / `corrupt` / `encrypted` 之一才算真坏；`locked` / `busy` / `io` 这类临时故障**绝不能**触发隔离（双开实例或一次抖动就把好数据搬走是不可接受的）。
- **S-26** 隔离动作：把库文件连同 `-wal`、`-shm` 一起改名为 `{库文件}.corrupt-{YYYYMMDD-HHMMSS}`；同名已存在则加 `-2`、`-3` 退让，**绝不覆盖**上一份坏库样本。隔离后照常建新库，并写 `meta["recovered_from"] = 隔离文件名`，同时 ERROR 级日志原样：`数据库已损坏（{问题}），已隔离为 {文件名}，新库已建立，历史数据需从备份恢复。`
- **S-27** `init()` 返回 `None`，不返回连接、不返回状态；调用方只靠「不抛异常」判断成功。

### 1.5 契约条目：消息

- **S-28** `insert_message(*, msg_id, group_id, content, source_text="", ts=None, received_at=None, source="qqbot", event="", sender_id="", sender_name="", group_name="") -> bool`：`msg_id` 去空格后为空 → 抛 `ValueError("msg_id 不能为空")`；`received_at` 缺省用当前时间，`ts` 缺省跟随 `received_at`；两者都按秒精度 ISO 落库。
- **S-29** 重复 `msg_id` 不覆盖、抛错或合并，返回 `False`（用「插入或忽略」语义）；**首次插入成功返回 `True`**，并只在这种情况下追加 `messages.jsonl` 一行（内容含全部 11 个字段）。
- **S-30** `unprocessed_messages(*, limit=400)`：返回**没有** `processed` 记录的消息全字段字典，按 `received_at` 升序、`msg_id` 升序；`limit` 转整数后直接作 LIMIT。
- **S-31** `mark_processed(msg_ids, digest_id)`：过滤空串；空输入返回 0 且不写库；已存在的记录不覆盖（插入或忽略）；返回**传入的有效 id 个数**（不是实际新增数）。
- **S-32** `oldest_unprocessed() -> str`：返回未处理消息里最早 `received_at` 的字符串；没有则空串。

### 1.6 契约条目：摘要归档

- **S-33** `insert_digest(*, kind, window_start, window_end, body, payload, message_count, llm_used) -> int`：`item_count` 取 `payload` 长度；`llm_used` 落库为整数 0/1；`payload` 以 JSON 文本存（不转义中文）；`created_at` 用当前时间；返回自增 id（>0）。
- **S-34** 插入后向 `digests.jsonl` 追加一行（含 id、kind、窗口、计数、`llm_used` 布尔、正文、创建时间）。
- **S-35** `get_digest(digest_id) -> dict | None`：返回行字典并把 `payload` 解析成 `items` 键；`payload` 不是合法 JSON 时 `items` 为空列表（不抛）；id 不存在返回 `None`。
- **S-36** `recent_digests(*, limit=5)`：按 id 倒序取最近 N 条（`limit` 至少按 1 算），每条同样补 `items`；字段含 id、kind、窗口、计数、正文、payload、创建时间。
- **S-37** `recent_items(*, hours=6, limit=200) -> list[dict]`：`hours <= 0` 直接返回空列表；只取 `created_at` 在窗口内且 `item_count > 0` 的摘要，按 id 倒序，`limit` 至少按 1 算；把每条 payload 里的条目原样展开，并给每条补 `digest_id`（整数）、`kind`（摘要类型）、`created_at`（摘要创建时间）；payload 不是列表或条目不是字典时跳过该条/该条摘要。
- **S-38** `recent_item_texts(*, hours=6, limit=200)`：对 S-37 的结果投影成 `{"text": item.text 或 item.summary, "summary", "group", "ts": created_at}` 四个字符串字段。

### 1.7 契约条目：投递去重与重试

- **S-39** `claim_delivery(*, digest_id, channel, target, dedupe_key, max_attempts, retry_seconds) -> (bool, int)`：
  - 三元组 `(dedupe_key, channel, target)` 首次出现 → 插入 `status='pending'`、`attempts=1`，返回 `(True, 新 id)`；
  - 已存在且 `status='sent'` → `(False, 该 id)`，**永不重发**；
  - 已存在且 `attempts >= max_attempts` → `(False, id)`；
  - 已存在且距 `updated_at` 的秒数 `< retry_seconds`（冷却中）→ `(False, id)`；
  - 其余情况把 `attempts` 加 1、`status` 置回 `'pending'`、刷新 `updated_at`，返回 `(True, id)`。
- **S-40** `mark_delivery(delivery_id, *, ok, error="")`：`ok` 真写 `sent`，否则写 `failed`；`last_error` 截断到 500 字符；刷新 `updated_at`。
- **S-41** `increment_delivery_attempt(delivery_id) -> int`：记录不存在返回 0；存在则 `attempts` 加 1、`status` 置 `pending`、刷新 `updated_at`，返回新值。
- **S-42** `pending_deliveries(*, max_attempts, retry_seconds)`：取 `status != 'sent'` 且 `attempts < max_attempts` 的投递，按 `updated_at` 升序，最多 50 条，并在 Python 侧过滤掉「距 `updated_at` 不足 `retry_seconds` 秒」的（时间戳无法解析的不过滤）；每条 JOIN 摘要，附 `kind`、`window_start`、`window_end`、`body`、`item_count`。
- **S-43** `get_delivery(delivery_id)`：返回 `id/status/attempts/last_error/updated_at` 五个键的字典，或 `None`。
- **S-44** `delivery_stats()`：按状态分组计数，返回固定四键 `{"sent","pending","failed","total"}`，缺失状态补 0，`total` 为所有状态之和。

### 1.8 契约条目：两张同步状态

- **S-45** `dingtalk_sync_state()`：返回 `{"salt_1","salt_2","frame_index","last_created_at"}` 四键；无记录、JSON 非法、不是字典时一律返回这四个 0。
- **S-46** `save_dingtalk_sync_state(state)`：落库前归一成四个整数（缺省 0；`frame_index`、`last_created_at` 负值按 0 算），键名 `wal_state`，用插入或替换。
- **S-47** `chaoxing_sync_state()`：返回 `{"notice_ids": [], "work_ids": []}` 形状（字符串列表）；缺失/非法时返回两个空列表。
- **S-48** `save_chaoxing_sync_state(state)`：两个 id 列表都转字符串并**只保留最后 1000 个**，键名 `chaoxing_state`，JSON 落库不转义中文。

### 1.9 契约条目：收件箱

- **S-49** `save_mark(msg_id, verdict, reason="", method="", task_id=None)`：按 `msg_id` 插入或替换，`classified_at` 用当前时间；`task_id` 为 `None` 时存空。
- **S-50** `unmarked_messages(*, since=None, until=None, groups=None, limit=1000)`：返回**没有**判定记录的消息全字段；`since`/`until` 非空时按 ISO 比较 `received_at`（含等号）；`groups` 非空时按 `group_id IN (...)` 过滤；按 `received_at`、`msg_id` 升序；`limit` 直接作 LIMIT。
- **S-51** `inbox_items(*, verdict=None, group_id=None, q=None, limit=50, offset=0, promoted=None)`：只返回**有判定**的消息，字段固定为 `msg_id/source/event/received_at/group_id/group_name/sender_name/content/verdict/reason/method/task_id`；`q` 对 `content` 做 LIKE 包含匹配（前后加 `%`）；`promoted=True` 只取已转待办（`task_id` 非空），`False` 只取未转；按 `received_at` 倒序，`limit`/`offset` 直接生效。
- **S-52** `inbox_counts()`：返回固定三键 `{"notice","suspect","noise"}` 再加 `promoted`；前三键来自 `inbox_marks` 按 verdict 分组，缺失补 0，未知 verdict 也照实作为额外键累加；`promoted` 是 `task_id` 非空的条数。
- **S-53** `delete_inbox_items(msg_ids) -> int`：同一事务里删 `inbox_marks` 与 `messages` 两张表的对应行；返回**`messages` 实际删除数**（marks 不存在不虚报），最小 0；空输入、未知 id 都返回 0 且不抛异常；已转成的待办（tasks 表）**不跟着删**。

### 1.10 契约条目：待办

- **S-54** `upsert_task(*, task_key, summary, audience="", condition="", details=(), action="", category="info", importance=3, deadline="", groups=(), sender="", evidence="", digest_id=0, status="open", confidence=0.0, source="", classification_reason="") -> int`：`task_key` 去空格后为空 → 返回 **0**（不抛错）；`status` 不在 `TASK_STATUSES` 里时按 `'open'` 处理；`groups`/`details` 过滤空串、`groups` 去重后存 JSON；返回任务 id。
- **S-55** 同一 `task_key` 再写是**更新而不是新增**，并且必须**保留用户处理过的状态**：`status`、`done_at`、`snooze_until`、`pinned`、`urgent_override` 一律不被覆盖。可以覆盖的是：摘要、动作、适用对象、条件、细节、分类、组、发送者、digest_id、更新时间。
- **S-56** 更新策略细节：`importance` 取新旧**较大值**；`confidence` 取新旧较大值；`deadline` 只在传入非空时覆盖；`evidence`、`source` 只在传入非空时覆盖。
- **S-57** 并发安全：同 `task_key` 的并发写入不能抛唯一约束冲突（用一条「冲突即更新」的语句完成）。
- **S-58** 事件：每次**首次创建**写一条 `created` 事件（明细含 status、confidence、reason），若初始状态是 `candidate` 再补一条 `candidate_detected`（明细含 confidence、reason）；更新时不写新事件。判断「首次」的依据必须是**该任务有没有 `created` 事件**，不能靠时间戳。
- **S-59** `list_tasks(*, include_done=True, statuses=None, limit=800)`：`statuses` 非空时按集合过滤（优先于 `include_done`）；否则 `include_done=False` 只取 `open`；`limit` 缺省 **800**（有测试读函数签名默认值断言 800），`limit=None` 表示不限量（实现可用负数 LIMIT）。
- **S-60** `list_tasks` 排序：`deadline` 为空的排最后，然后按 `deadline` 升序、`importance` 降序、`id` 降序。
- **S-61** `list_tasks` 与 `get_task` 返回的行要补三个派生字段：`candidate_detail`（最近一条 `candidate_detected` 事件的 detail 文本）、`source_text`（该任务的 `task_key` 当作消息 id 去 `messages` 里取 `source_text`，为空则取 `content`，找不到为 NULL）、`duplicate_summary`（`duplicate_of` 指向的任务摘要）。
- **S-62** 行投影（`_task_row` 语义）：`groups`、`details` 一律解析成字符串列表（非法 JSON 或非列表 → 空列表）；`condition_text` 改名为 `condition`（原键被移除）；`done` 是「status == done」的布尔；`snooze_until` 是字符串（缺省空串）；`duplicate_of` 转整数；`pinned` 转布尔。
- **S-63** `effective_urgent(task) -> bool`：`urgent_override` 非 `None` 时**以它为准**（真假值直接转布尔）；为 `None` 时等于「category == 'urgent'」。
- **S-64** `set_task_urgent(task_id, value) -> dict`：`value` 只接受 `True`/`False`/`None`，其它类型抛 `ValueError("紧急覆盖值必须是 true、false 或 null")`；任务不存在返回 `{"ok": False, "error": "任务不存在"}`；成功写 1/0/NULL 并返回整条任务行 + `ok=True`（键 `urgent_override` 分别是 1/0/None）。
- **S-65** `set_task_pinned(task_id, value) -> dict`：`value` 必须是真正的布尔，否则抛 `ValueError("置顶值必须是 true 或 false")`（传 1 也要抛）；任务不存在 → `{"ok": False, "error": "任务不存在"}`；`value=True` 且任务已 `done` → `{"ok": False, "error": "已完成的任务不能置顶"}`（错误文案里必须出现「置顶」）；成功返回整条任务行 + `ok=True`。
- **S-66** `apply_task_action(task_id, action, *, detail="") -> bool`：任务不存在返回 `False`；`action` 去空格转小写；每条动作的合法前置状态与副作用如下表；不满足前置状态返回 `False`（不写事件、不改数据）；未知动作抛 `ValueError("不支持的任务动作: {action}")`。

| 动作 | 允许的当前状态 | 效果 | 事件名 |
|---|---|---|---|
| `confirm` | candidate / open | status→open，写 `confirmed_at` | `confirmed` |
| `dismiss` | candidate / open | status→dismissed，写 `dismissed_at` | `dismissed` |
| `done` | open / candidate | status→done，写 `done_at` | `done` |
| `snooze` | candidate / open | 写 `last_reminded_at` 与 `snooze_until`（明细不是字典或没有 `until` 时默认「当前时间 + 1 天」） | `snoozed`（明细含归一后的 `until`） |
| `reopen` | done / dismissed / expired | status→open，清空 `done_at`，写 `confirmed_at` | `reopened` |
| `delete` | done / dismissed / expired，**或** `task_key` 以 `manual:` 开头的 open 任务 | **真删**：先删该任务全部事件再删任务行，**不写**任何事件 | 无 |

- **S-67** 事件明细是 `dict`/`list` 时序列化成 JSON 文本（不转义中文、`default=str` 兜底），否则转字符串；空明细存空串。
- **S-68** `set_task_status(task_id, done)` 是兼容壳：`done=True` 等价 `apply_task_action(..., "done")`，否则等价 `reopen`。
- **S-69** `list_open_tasks(*, limit=800)` 等价 `list_tasks(statuses=("open",), limit=limit)`。
- **S-70** `list_task_events(task_id, *, limit=100)`：按事件 id **倒序**返回 `id/task_id/event/detail/created_at` 五个键，`limit` 至少按 1 算。
- **S-71** `task_stats()`：返回固定七键 `total/candidate/open/done/dismissed/expired/overdue`；`overdue` 的定义是「status=open 且 deadline 非空且 deadline 小于当前时间」。
- **S-72** `mark_task_reminded(task_id, *, detail="", when=None) -> bool`：只对 `open`/`candidate` 生效；成功时写 `last_reminded_at`、`remind_count` 加 1、刷新 `updated_at`，并追加 `reminded` 事件，返回 `True`；状态不符或任务不存在返回 `False`（不写事件）。
- **S-73** `task_metrics(*, start, end)`：统计 `[start, end)` 区间内的事件次数，返回 `candidates/confirmed/dismissed/completions/reminders/completions_after_reminder/overdue` 七个整数，外加三个一位小数的百分比：`confirmation_rate = confirmed*100/candidates`、`dismissal_rate = dismissed*100/candidates`（分母为 0 时取 `0.0`）、`completion_rate = completions*100/max(1, completions+overdue)`。`completions_after_reminder` 统计「完成事件之前存在过该任务的提醒事件」的去重任务数；`overdue` 统计当前 status=open 且 deadline 小于 `end` 的任务数。
- **S-74** `apply_task_correction(task_id, correction, *, value="") -> bool`：`correction` 只接受 `not_notice`、`not_task`、`not_urgent`、`duplicate`、`category`、`deadline`、`clear_deadline`，否则抛 `ValueError("不支持的纠错类型: {原文}")`；任务不存在返回 `False`。各类型效果：

| 类型 | 效果 |
|---|---|
| `not_notice` / `not_task` | status→dismissed |
| `duplicate` | status→dismissed，并把 `duplicate_of` 指向「重复目标」；目标的 `groups` 与新任务的 `groups` 取并集写回目标 |
| `not_urgent` | 分类为 urgent 时改成 `action`；`importance` 取 `min(旧值, 3)` |
| `category` | `value` 必须属于四个分类之一，否则抛 `ValueError("无效的任务分类")`；`importance` 取 `IMPORTANCE_BY_CATEGORY[新分类]` |
| `deadline` | `value` 转 ISO 时间写入；传入非空但解析不出 → 抛 `ValueError("无效的截止时间")` |
| `clear_deadline` | `deadline` 清空 |

- **S-75** 重复目标解析：`value` 能转成整数且指向另一条存在的任务 → 用它；否则按「摘要文本归一化后相等」在最近 200 条（排除 dismissed/expired、排除自己）里找最近的一条；都找不到 → `duplicate_of` 为 0。归一化=去掉所有空白与中英文标点（`，。、；：！？,.!?;:（）()[]【】"'“”‘’-_—`）并转小写。
- **S-76** 纠错必须留痕：写一条 `corrected` 事件，明细含 `type`、`previous`（status/category/importance/deadline 四项旧值）、`new`（status/category/importance/deadline/duplicate_of 五项新值）、`value`（截断 500）；若合并了群，再加 `merged_groups`。`dismissed_at` 只在「本次变 dismissed 且原本不是」时写。
- **S-77** `correction_stats(*, days=30)`：只统计最近 `days` 天（至少按 1 天算）的 `corrected` 事件，返回 `{"days", "total", "by_type", "by_group"}`；`by_type` 是「纠错类型 → 次数」，明细 JSON 非法时类型记 `unknown`；`by_group` 是「群名 → {类型: 次数, total: 次数}」，同一事件涉及多个群时每个群都计。
- **S-78** `correction_insights(*, days=30, min_group_hits=3)`：按群纠错总数降序，跳过总数小于 `max(2, min_group_hits)` 的群；对通过的群拼一句中文建议，句式与成分固定（`{群名}：` + 若干个「误判紧急 N 次」「误判为通知/待办 N 次」「重复 N 次」用「、」连接 + `，建议收紧该群规则或加入低优先级群。`），没有任何成分的群不出建议；另外若全部纠错里 `duplicate` 总数 ≥3，再追加一句 `重复标记偏多，建议复查跨群去重时间窗 QQ_DIGEST_DEDUPE_HOURS。`
- **S-79** `clear_tasks() -> int`：删空 `task_events` 与 `tasks` 两张表，返回删除的任务条数（供命令行重置用，**不得**接到网页端）。
- **S-80** `clear_finished_tasks() -> int`：只删 status 属于 done/dismissed/expired 的任务，并删除**它们**的事件；open/candidate 一律不动；返回删除条数，没有可删时返回 0。事件必须跟着任务一起消失。

### 1.11 契约条目：群名

- **S-81** `group_names() -> dict[str,str]`：群号 → 群名。来源两条：①`messages` 里「每个群最新那条**真正带名字**的消息」——判定「带名字」的条件是 `group_name` 非空且不等于 `group_id`；注意取的是「最新带名字消息的时间」，不是「最新消息」；②`group_names` 表（NapCat 报回来的名字）**优先级更高**，同群号时覆盖①的结果。
- **S-82** `remember_group_names(names) -> int`：清洗规则——键值都去空格，键或值为空丢弃，值等于键丢弃；空结果返回 0 不写库；否则按群号插入或更新，返回写入条数。
- **S-83** `group_label(group_id, names, aliases=None) -> str`：空群号返回空串；否则「用户别名 → 缓存群名 → 群号」依次回退，取第一个非空。

### 1.12 契约条目：元信息与维护

- **S-84** `meta_get(key, default="") -> str`：不存在返回 `default`。
- **S-85** `meta_set(key, value)`：插入或更新，值转字符串。
- **S-86** `prune(*, retention_days=None) -> int`：`days = retention_days 或 实例默认（30）`；截止时间=当前时间减 `days` 天；同一事务内按序：①删 `processed` 里挂在过期消息上的行（先子后父，满足外键）②删过期 `messages`，返回其行数 ③删 `processed` 里已经没有对应消息的孤儿行 ④删 `inbox_marks` 里没有对应消息的孤儿行。
- **S-87** `prune` 必须清孤儿判定：过期消息被删后，它的 `inbox_marks` 不能留下，否则 `inbox_counts()` 会一直虚高；仍在库里的消息的判定不能被误删。
- **S-88** `counts()`：返回固定五键 `messages`、`unprocessed`（无 processed 记录的消息数）、`digests`、`deliveries_sent`、`deliveries_failed`。

### 1.13 并发与时序补充

- **S-89** 所有方法对「表不存在」「列为 NULL」要按本文默认值语义处理，不能因缺列抛异常（升级过程中的老库必须能读）。
- **S-90** 任何公开方法都不得在失败时留下半条数据：单方法单事务（S-04）；需要「删两处」的操作（删收件箱条目、删任务、清已完成、prune）必须把两次删除放在同一连接同一事务里。
- **S-91** 不提供异步接口；调用方可能在多线程里并发调用（S-05），方法本身必须可重入（不持有跨调用状态）。

### 1.14 测试锁住的事实（`tests/test_store.py`，31 个用例）

1. 同一 `msg_id` 插两次：第一次 `True`、第二次 `False`；`counts()` 的 `messages` 与 `unprocessed` 各为 1。
2. `inbox_items()` 的返回必须带 `source` 与 `event` 原值（例：`onebot-attachment` / `GROUP_ATTACHMENT`）。
3. `delete_inbox_items(["m1"])` 返回 1；之后列表只剩 m2、`messages` 计数减 1、`inbox_counts()["notice"]` 归 0 且 suspect 保持 1。
4. `delete_inbox_items([])` 与「未知 id」都返回 0，且不误删已有数据。
5. `unprocessed_messages()` 按插入顺序返回（m1、m2）；`mark_processed` 后为空；`insert_digest` 返回 id>0；`get_digest(id)["items"]` 等于写入的 payload。
6. `claim_delivery` 首次 `(True, id)`；冷却期内（`retry_seconds=3600`）再次调用 `(False, 同一 id)`；`mark_delivery(ok=True)` 之后无论如何都 `(False, ...)`。
7. `pending_deliveries` 能取回失败待重试的记录且带摘要 `body`；`attempts` 达到 `max_attempts` 后不再出现在结果里。
8. `meta_set/meta_get` 往返一致；`prune(retention_days=30)` 删掉 40 天前的消息并返回 1，`messages` 归 0。
9. 过期消息的 `inbox_marks` 必须被 prune 清掉（notice 归 0），未过期消息的判定保留（suspect 仍 1），列表只剩 fresh。
10. `insert_message` 成功后写 `messages.jsonl` 且内容含该 msg_id。
11. 用「老结构」建库（只有基础列、含一条数据）后 `Store(path)`：老任务还能查到、`confidence` 为 0.0、`tasks` 表被补出 `confirmed_at`/`last_reminded_at` 等列、`task_events` 表存在。
12. `upsert_task` 写入的 `audience`、`condition`（返回键名是 `condition`）、`details` 列表、以及 `source_text`（来自关联消息的完整原文，不能被截断）都要能原样读回。
13. `list_tasks` 的 `limit` 默认值必须是 **800**；`limit=2` 取 2 条；`limit=None` 返回全部 3 条；不传时也返回 3 条（数据量小于默认上限）。
14. 任务动作链 `confirm → done → reopen → dismiss` 四步全 `True`；事件里必须能查到 `dismissed`、`reopened`、`done`、`confirmed`、`candidate_detected`；`task_metrics` 在跨年区间内 `candidates/confirmed/dismissed` 各为 1；`task_stats()["dismissed"]` 为 1。
15. 已完成任务可以 `delete`：任务查不到、事件为空列表。
16. `dismiss` 只接受 candidate/open：对 done 返回 `False` 且状态不变；对 open、candidate 返回 `True`；对已 dismissed 的再 dismiss 返回 `False`；expired 返回 `False`。
17. `delete` 只接受「已结束」任务：open、candidate 都返回 `False` 且任务仍在；dismissed、expired 返回 `True`；不存在的 id 返回 `False` 不抛异常。
18. `clear_finished_tasks()` 删掉 done/dismissed/expired 共 3 条并返回 3，只剩 open 与 candidate；被删任务的事件为空；再调一次返回 0。
19. `apply_task_correction(not_urgent)`：urgent 任务变 `action`，`importance` 变 3；`duplicate` 让任务变 dismissed；事件明细里含 `not_urgent` 与 `previous`。
20. `set_task_urgent(True)` 返回 `ok=True`、`urgent_override=1`，列表里能读到 1，`effective_urgent` 为真；`set_task_urgent(False)` 返回 0；`set_task_urgent(None)` 返回 `None` 且 `effective_urgent` 跟随 category（urgent → 真）。
21. `set_task_urgent` 对不存在的任务返回 `ok=False` 且带 `error`。
22. `urgent_override` 迁移幂等：同一路径连续构造三份 `Store`，值不丢、不报错。
23. `pinned`：新建默认假；置顶后 `ok=True`、`pinned=True`、列表首条为真；重开两次仍在；取消置顶后为假。
24. 已完成任务不能置顶（`ok=False` 且 `error` 含「置顶」，`pinned` 仍假）；传 `1`（非布尔）必须抛 `ValueError`；不存在的 id 返回 `ok=False`。
25. `snooze` 带 `until` 时 `snooze_until` 原样存；不带 `until` 时存非空值且最近事件是 `snoozed`。
26. `duplicate` 纠错后：状态 dismissed、`duplicate_of` 指向原任务 id、`duplicate_summary` 等于原任务摘要、原任务的 `groups` 变成两个群的并集（顺序为先原有后并入）。
27. 「分类 → 重要度」必须是同一份定义：`summarizer.IMPORTANCE_BY_CATEGORY is store.IMPORTANCE_BY_CATEGORY`；四类分别 5/4/3/3；手动再确认一次分类后重要度不变；info 必须是 3（历史回归点是 1）。
28. `correction_stats(days=30)` 三次 `not_urgent` 后：`total=3`、`by_type["not_urgent"]=3`、`by_group["新生交流群"]["not_urgent"]=3`；`correction_insights(days=30, min_group_hits=3)` 里要出现该群名。
29. `delivery_stats()`：一条成功后 `sent=1`、`pending=0`。
30. `group_names()` 只认「最新那条带名字的消息」：三条同群记录里最新一条群名等于群号时不算名字，仍返回较早那条的真实群名；全程只有群号时该群不出现；写入 `group_names` 表后缓存覆盖消息里的名字，且表里独有的群（g9）也要出现。

---

## 2. `noticehub/summarizer.py`

### 2.1 公开面

数据类 `Digest`（字段见 2.2）与这些函数：

- `record_to_message(record: dict) -> Message`（`Message` 是对外消息值对象：时间、发送者、正文、来源群）
- `payload_to_item(entry: dict) -> dict`
- `analyse_records(records) -> list[dict]`
- `classify_task_item(item, settings) -> dict`
- `is_focus(analysis, settings) -> bool`
- `filter_recent_duplicates(analyses, history, settings) -> tuple[list, list]`
- `urgent_analyses(analyses, settings, *, now=None) -> list[dict]`
- `evidence_sentence(text, item=None, limit=300) -> str`
- `build_digest(settings, records, *, kind="window", now=None, history=None) -> Digest`
- `format_push_text(digest, settings) -> str`
- `push_summary(digest, limit=26) -> str`
- `html_push_text(digest, settings) -> str`
- `finalize_digest(digest, settings) -> Digest`
- `preview_text(settings, records) -> str`

### 2.2 契约条目：`Digest` 数据形状

- **M-01** 字段：`kind`（字符串）、`window_start`/`window_end`（可空时间）、`items`（列表，默认空）、`message_ids`（字符串列表）、`body`/`summary`/`html`（字符串，默认空）、`llm_used`（布尔，默认假）、`llm_error`（字符串）、`llm_retryable`（布尔）、`message_count`（整数）、`groups`（字符串列表）、`id`（整数，默认 0）、`omitted_count`（整数，默认 0）。
- **M-02** `has_focus` 属性=「items 非空」。
- **M-03** `title` 属性按 kind 映射：`urgent`→`紧急通知`、`window`→`群通知汇总`、`silent`→`静默窗口`、`morning`→`今日截止`、`evening`→`截止提醒`，其它→`群通知`。
- **M-04** `window_start_text` / `window_end_text`：有时间时格式化为 `YYYY-MM-DD HH:MM`，无时间返回空串。
- **M-05** `payload()` 把 items 转成可 JSON 序列化的列表，每项固定 16 键：`category`（缺省 `info`）、`importance`（整数）、`score`（整数）、`summary`、`audience`、`condition`、`details`（过滤空串后的字符串列表）、`action`、`evidence`、`deadline`（格式化后的字符串）、`group`、`group_id`、`sender`、`text`、`msg_id`。`summary`/`action` 缺失时用本地规则补（不允许出现空串与 `None`）。

### 2.3 契约条目：入参转换

- **M-06** `record_to_message`：时间取 `ts` → `received_at` → 当前时间；发送者取 `sender_name` → `sender_id` → `群成员`；正文取 `content`（缺省空串）；来源取 `group_name` → `group_id` → 空串。
- **M-07** `payload_to_item`：把归档条目还原成可再次渲染的条目。`importance` 缺省时按 `IMPORTANCE_BY_CATEGORY[category]`（未知分类按 3）；`deadline_dt` 由条目的 `deadline` 文本解析；`evidence` 为空时用 `evidence_sentence` 兜底；`details` 过滤空值。
- **M-08** `analyse_records`：对每条记录产出「分析字典」，并额外写入 `group_name`、`group_id`、`msg_id`（都是字符串，缺省空）、`record`（原始记录）。分析字典至少要含：`category`（四类之一）、`score`（整数）、`tags`（集合，可含 `urgent`/`action`/`academic`/`notice`/`authority`/`broadcast`）、`deadline`（可空时间）、`action_words`（列表）、`colloquial_question`（布尔）。

### 2.4 契约条目：筛选、去重与紧急判定

- **M-09** `is_focus(analysis, settings)`：`colloquial_question` 为真 → 假；低优先级群（`settings.quiet_groups` 命中）且不是「通知类」→ 假；分类为 `urgent` 时要求 `score >= 1`；其余分类要求 `score >= settings.min_score`（默认 3）。
- **M-10** `classify_task_item(item, settings)`：把行动项分成可直接执行的 `open` 与待确认的 `candidate`，返回 `{"status", "confidence", "reason", "source"}`。置信度从 0.28 起累加：有截止时间 +0.25、有强指令词（`请/务必/必须/要求/按时/尽早/截止/前完成/前提交`）+0.20、有直接动作词（`提交/填写/报名/缴费/领取/参加/完成/上传/确认/回复/核对/下载/安装/办理/注册/认证/上交` 或分析给出的 action_words）+0.14、分类 urgent +0.10、分类 action +0.08、有证据 +0.05、`score >= 4` +0.05；扣分：弱措辞（`记得/别忘了/不要忘记/可能要/可能/也许/大概/尽量/有空/考虑/可以在`）−0.24、模糊时间（`近期/近日/稍后/左右/前后/之前要/尽快`）−0.14、低优先级群 −0.08、没有直接动作 −0.08；结果夹到 `[0.05, 0.98]` 并保留两位小数。
- **M-11** 上条的状态判定优先序：默认 `candidate`（理由「只有行动线索，证据还不够直接，先放到待确认。」）；有弱措辞 → `candidate`（理由里带出那个词）；有截止时间且（有直接动作或强指令）→ `open`（理由「有明确行动要求和截止时间，直接进入待办。」）；urgent 且有直接动作或强指令或 tag 含 urgent → `open`（理由「规则判断为紧急事项，直接进入待办。」）；强指令且有直接动作 → `open`（理由「有明确的行动要求，没有写截止时间。」）；有截止但既无动作也无指令 → candidate（理由「提到了日期，但更像活动日期而不是截止时间。」）；有模糊时间 → candidate（理由里带出那个词）；无截止但有直接动作 → candidate（理由「识别到行动，但没有明确截止时间，先确认是否真的要办。」）。`source` 缺省 `qq_message`。
- **M-12** `filter_recent_duplicates`：`settings.dedupe_hours <= 0` 或历史为空 → 原样返回 `(analyses, [])`；否则把历史条目文本与候选文本都做「匹配归一化」后比较相似；命中的候选被移出保留列表，并在该条目上写 `suppressed_duplicate = {"group": 历史群名, "summary": 历史摘要, "ts": 历史时间}`；返回 `(保留, 被抑制)`。
- **M-13** 有候选被抑制时写一条 INFO 日志，文案原样：`跨群去重：{n} 条与最近 {hours} 小时内已推内容重复，不再重复推送（{群名、连接}）。`（群名去重排序；没有群名时写「来源未知」）。
- **M-14** `urgent_analyses(analyses, settings, *, now=None)`：`settings.urgent_immediate` 为假 → 空列表；跳过低优先级群；只保留 `category == "urgent"` 且 `score >= 4` 且 tags 与 `{action, notice, academic, broadcast, authority}` 有交集的；再要求下列任一成立才进入结果：正文命中强紧急词（`紧急/务必/尽快/马上/立即/最后通知/逾期/过期/截止`）、分析给出 `action_words`、截止时间落在 `[now, now+48h]`（含两端）。`now` 缺省取当前时间。

### 2.5 契约条目：LLM 精炼（必须写细）

- **M-15** 触发条件：`settings.llm_enabled` 为真**且**候选非空才尝试；否则完全跳过（`llm_used` 假、`llm_error` 空）。
- **M-16** 缺密钥短路：`dashscope_api_key` 为空且 `llm_backend` 不在 `{auto, codebuddy}` 时，不发起任何请求，`llm_used=False`、`llm_error="未配置 DASHSCOPE_API_KEY"`、`llm_retryable=False`（配置缺失**不是**可重试故障，否则消息会被无限推迟）。
- **M-17** 送 LLM 的条目上限 **50 条**；超出的部分原样保留、不送模型。送出的每条包含：`id`（从 1 起的序号）、`time`（`YYYY-MM-DD HH:MM:SS` 或空）、`sender`、`text`（**截断到 1200 字符**）、`local_category`、`local_deadline`（格式化后的本地截止时间字符串）。
- **M-18** 请求形状（OpenAI 兼容的 chat completions）：`POST {endpoint}`，请求头 `Authorization: Bearer {api_key}` 与 `Content-Type: application/json`，正文是 JSON：`model`（`settings.dashscope_model`，默认 `qwen-plus`）、`temperature` 0.1、`max_tokens` 6000、`messages` 两条——system 说明「只依据用户消息、不补造、必须保留适用对象/条件/例外/不同人群差异、只输出严格 JSON」；user 给出 JSON 输出协议（`items[]` 里每项含 `id/category/importance/summary/audience/condition/action/details/deadline/keep`）、当前时间、四类分类定义（urgent=今明两天必须处理；action=要报名/提交/缴费；academic=考试/课程/教务；info=其他通知）、`importance` 1–5、`keep=false` 表示可丢弃，并附带 M-17 的消息列表。
- **M-19** 模型/端点特化：模型名以 `qwen3` 开头时请求体加 `enable_thinking=false`；端点指向本机（`http://127.0.0.1` 或 `http://localhost` 开头）时再加 `think=false`、`reasoning_effort="none"`。
- **M-20** 后端选择：`llm_backend` 只允许 `auto/ollama/openai/codebuddy`，其它值抛 `RuntimeError`（文案 `不支持的 LLM backend: {值}`）。`auto` 与 `codebuddy` 先走本地 CLI（命令行路径取环境键 `QQ_DIGEST_CODEBUDDY_CLI`，超时同 `llm_timeout`）；CLI 返回的 JSON 必须含 `items` 列表，否则视为失败：`codebuddy` 模式直接抛错，`auto` 模式回退到 HTTP。`ollama`/`openai` 直接走 HTTP。**`ollama` 后端绝不能调用 CLI**（有测试断言不被调用）。
- **M-21** 超时与重试：单次请求超时 = `settings.llm_timeout`（默认 60 秒）；重试次数 = `settings.llm_max_retries`（默认 2，即最多 3 次）；退避 = `settings.llm_retry_backoff`（默认 1.5 秒，指数翻倍）；是否重试用批次 A 的重试判定（408/409/425/429/500/502/503/504、无状态码的网络/超时、响应无法解析都可重试；401/403/400 不重试）；日志标签固定为 `文本模型精炼`。
- **M-22** 失败分类与对外可见文案（原样）：HTTP 层失败 → `LLM HTTP {状态码}: {响应体前 500 字符}`，且异常带 `status`；网络/超时 → `LLM 请求失败: {原始错误}`；响应不是可解析 JSON 对象 → `LLM JSON 解析失败: {原因}`。这三种都以「可重试异常」形式抛出，再由 §M-23 决定是否可重试。
- **M-23** 失败回退：任何 `RuntimeError` 都**不得**让摘要流程失败——`llm_used=False`、`llm_error=str(错误)`、`llm_retryable=llm_should_retry(错误)`，写一条 WARNING `LLM 精炼失败，回退本地规则：{错误}`，然后用本地规则继续产出摘要。
- **M-24** 响应解析：从 `choices[0].message` 取正文，依次尝试 `content` → `reasoning_content` → `reasoning`；正文可能是纯 JSON，也可能是「外面包了文字或 Markdown 代码块」的 JSON，实现必须能从文本里抽出第一个完整 JSON 对象；抽不出或不是对象 → 按 M-22 的解析失败处理。
- **M-25** 结果应用（字段裁剪规则，全部为硬上限）：响应里的 `id` 是**从 1 起的序号**，对应送出的第几条；`id` 不是纯数字的条目丢弃；模型没返回的条目原样保留。
  - `keep` 显式为 `False` → **丢弃该条**（不推送）；
  - `category` 必须是四类之一，否则保留模型前的分类；
  - `details`：列表时逐条压到 300 字符、最多留 8 条；字符串时压成一条（300 字符）；其它情况清空；
  - `summary` ≤140、`audience` ≤100、`condition` ≤140、`action` ≤80 字符；
  - `importance` 取整数，缺省 3；
  - 截止时间：**本地规则解析出的优先**，本地没有才用模型返回的（模型值按「带时分/带日期的中文与 ISO 时间」解析，解析不出则不设）；
  - 处理完的条目按「分类序（urgent→action→academic→info→其它）→ importance 降序 → score 降序」排序，`score` 缺失按 0。
- **M-26** 送出的前 50 条之外的原条目追加回结果（不参与精炼排序的新增字段）。

### 2.6 契约条目：组摘要与渲染

- **M-27** `build_digest(settings, records, *, kind="window", now=None, history=None)`：
  - `kind == "urgent"` 时参与筛选的是 `urgent_analyses` 的结果，其余 kind 用全部分析；
  - 候选 = `is_focus` 通过者 → 若有历史先做跨群去重 → 再做「本地相同内容去重」→ 按 `score` 降序 → 记下 `omitted = max(0, 候选数 - settings.max_items)`（超出部分只报数量；>0 时写 WARNING `本次 {omitted} 条重点超出单次上限 {max_items}，未展开。`）→ 截断到 `max_items`；
  - 再按 M-15…M-26 尝试 LLM；最后按「分类序 → importance → score → deadline → 消息时间」稳定排序并补齐字段（摘要、动作、适用对象、条件、details、截止、证据、重要度；重要度缺省用 `IMPORTANCE_BY_CATEGORY`，未知分类按 3）；
  - `window_start` = 首条记录的 `received_at`（解析失败或没有记录时用 `now`），`window_end` = `now`（缺省当前时间）；`message_ids` = 全部记录的 msg_id（缺省空串）；`message_count` = 记录条数；`groups` = 按出现顺序去重的群名（`group_name` 缺省用 `group_id`，都空的跳过）；
  - 返回前调用 `finalize_digest`。
- **M-28** `finalize_digest(digest, settings)`：`items` 为空时**原样返回**（`body`/`summary`/`html` 保持空）；否则依次生成 `body`（纯文本）、`summary`（通知栏一行）、`html`（仅当 `settings.html_push` 为真，否则保持空）。
- **M-29** `format_push_text(digest, settings)` 版式（换行符 `\n`，末尾去空白）：
  - 第 1 行：`【QQ{title}】{窗口结束时间}`（时间取 `window_end_text`，为空则当前时间 `YYYY-MM-DD HH:MM`）；
  - 第 2 行：`{群范围} · {message_count} 条消息 · {条目数} 条重点`，群范围是 `groups` 用「、」连接，无群写 `未知群`，并压到 40 字符；有被截断条目时在同一行追加 `（另有 {n} 条超出单次上限未展开）`；
  - 空行；
  - 每个条目：`{序号}. [{分类标签}] {摘要≤60}`（标签映射 紧急/待办/学业/通知）；下一行缩进三空格，用「 · 」连接存在的成分：`截止：{格式化截止}`、`需做：{动作≤30}`（动作与摘要相同时不写）、来源行 `{群名或跨群列表} · {发送者}`（压到 45 字符；群多于一个时群名后加 `（跨群重复）`）；有链接再加一行 `   链接：{首个链接}`；`settings.include_raw` 为真且原文不在摘要里时再加一行 `   原文：{原文≤80}`；每个条目后跟一个空行；
  - 结尾：`llm_error` 非空且有条目时追加一行 `（AI 摘要暂不可用，已用本地规则筛选）`。
- **M-30** `format_push_text` 默认**不输出原文**（`include_raw` 默认假）：推送正文里不得出现原文片段。
- **M-31** `push_summary(digest, limit=26)`：无条目时返回 `title`；否则取第一条：短截止标签（今天 HH:MM / 明天 HH:MM / MM-DD [HH:MM]，是否带时分取决于格式化后的截止串里有没有冒号）+ 摘要压到 14 字符，用空格连接；还有别的条目时追加 ` +{n}条`；最后整体压到 `limit` 字符。
- **M-32** `html_push_text(digest, settings)`：
  - 头部一行灰色小字：`{时间} · {群范围≤30} · {message_count} 条消息`，有截断时追加 ` · 另有 {n} 条超出单次上限未展开`；
  - 分类为 urgent/action 的条目最多渲染 **5 个色块**：背景 `#fff8ec`、左边框 3px（urgent `#d93025`、action `#f29900`）、标签「紧急」/「待办」、标题行「{截止} 截止 · {摘要}」（压到 46 字符）、可选「需做：{≤30}」、可选元信息行「{群名} · {发送者}」（压到 40）、可选「适用：… · 条件：…」（合并压到 90）、可选「原文：{证据≤40}」（证据已在摘要里时不写）、有任务链接时一个「查看完整原文」链接、有链接时一个「相关链接」链接；
  - 其余条目合并成一行：`其余 {n} 条：{前 5 条摘要各≤22，用 · 连接}`，条目多于 5 时再追加 ` 等 {n} 条`；
  - 结尾 `llm_error` 非空且有条目时追加灰色一行 `（AI 摘要暂不可用，已用本地规则筛选）`；
  - 所有插值文本必须 HTML 转义（`<script>` 之类必须变成实体）。
- **M-33** `preview_text(settings, records)`：空记录 → `没有待处理消息。`；有记录但没筛出重点 → `待处理 {n} 条消息，未筛出重点内容（不会推送）。`；否则头部 `[仅预览] 待处理 {n} 条，命中 {m} 条` + 两个换行 + 正文。
- **M-34** `evidence_sentence(text, item=None, limit=300)`：先把文本空白折叠成单空格；为空返回空串；按中英文句末标点（`。！？!?；;` 与换行）切句；关键词集合 =（条目截止时间的 `MM-DD`、`MM月DD日`、`M月D日`、`D日`、`HH:MM`）+ 强紧急词 + 证据词（`提交/报名/填写/截止/材料/考试/缴费/领取/上课/会议/讲座/面试/答辩/选课`）；返回**第一个命中关键词的句子**（超长截断到 limit）；没有命中则返回第一句。
- **M-35** 所有函数都是纯计算（除 LLM 调用与读配置外）：同一输入重复调用结果一致；不写库、不写文件、不发其它网络请求。

### 2.7 配置键（摘要相关，默认值固定）

| 键 | 默认 | 语义 |
|---|---|---|
| `min_score` | 3 | 非紧急条目的入选分数线 |
| `max_items` | 30 | 单次摘要最多展开的条目数（超出计数不静默丢） |
| `include_raw` | 假 | 推送正文是否附原文 |
| `html_push` | 真 | 是否生成 HTML 卡片 |
| `dedupe_hours` | 6 | 跨群去重时间窗（0 表示关闭） |
| `urgent_immediate` | 真 | 是否允许立即推送 |
| `quiet_groups` | 空 | 低优先级群（只进合并摘要，不触发立即推送） |
| `llm_enabled` | 真 | 是否尝试 LLM |
| `llm_backend` | `auto` | `auto`/`ollama`/`openai`/`codebuddy` |
| `dashscope_api_key` | 空 | 密钥；空且后端不是 auto/codebuddy 时不发请求 |
| `dashscope_model` | `qwen-plus` | 请求体 model |
| `dashscope_endpoint` | 供应商默认地址 | chat completions 完整地址 |
| `llm_timeout` | 60 | 单次超时（秒） |
| `llm_max_retries` | 2 | 重试次数（不含首次） |
| `llm_retry_backoff` | 1.5 | 首次退避秒数，指数翻倍 |

### 2.8 测试锁住的事实（`tests/test_summarizer.py`，32 个用例）

1. 三条消息（真通知 + 两句闲聊）→ 只保留第一条；正文含「截止」与群名；条目的 `action` 非空。
2. 「四六级报名截止时间是9月30日」→ payload 里 `category` 是 `urgent`、`deadline` 含 `2026-09-30`，且 payload 可被 JSON 序列化。
3. 没有截止时间的纯通知（「关于2026年国庆节放假安排的通知」）保留 1 条。
4. 「今天天气不错」被分析成 urgent 分类，但 `urgent_analyses` 返回空（紧急必须有实质内容）；「请各班班长今天18:00前提交材料」返回 1 条。
5. `urgent_immediate=False` 时 `urgent_analyses` 恒为空列表。
6. 只有「收到」时 items 为空、`body` 为空串、`preview_text` 含「不会推送」。
7. `preview_text` 正常输入含 `[仅预览]` 且含 `18:00`。
8. 缺 key 且后端 `ollama`：`llm_used` 假、`llm_error` 含「未配置」、`llm_retryable` 假、正文含「本地规则筛选」。
9. 缺 key 但后端 `codebuddy`：仍调用精炼函数一次，`llm_used` 为真。
10. 缺 key 且后端 `ollama`：精炼函数**一次都不调用**，`llm_error` 恰好是 `未配置 DASHSCOPE_API_KEY`。
11. 端点不可达（`llm_max_retries=0`、`llm_retry_backoff=0`）：条目仍保留 1 条、`llm_used` 假、`llm_error` 非空、`llm_retryable` **真**、正文含「规则筛选」。
12. 后端 `ollama` 时不得调用 CLI 精炼入口。
13. `Settings(llm_backend="codebuddy", key="")` 的 `llm_active` 为真；`ollama` + 空 key 为假。
14. 两条只差一个句号的相同消息 → 摘要只剩 1 条，`message_count` 仍是 2。
15. 真实群消息里的相对时间必须保留时分：「明天9月29日下午五点以前」→ `2026-09-29 17:00`；「在18:30完成入场」→ `2026-09-29 18:30`；「明天下午三点半」→ `2026-09-29 15:30`。
16. 推送正文用短摘要、隐藏原文：默认 `body` 含摘要、不含「原文：」、不含原文片段；`include_raw=True` 时含「原文：」。
17. LLM 返回 `{"items":[{id:1,category:action,importance:5,summary,action,deadline,keep:true}]}` 时：`llm_used` 真、正文含模型摘要、**不含**原文片段。
18. 「今晚8点」解析成 `2026-09-28 20:00`。
19. `digest.summary` 等于 `push_summary(digest)`，且含 `今天18:00`。
20. HTML 推送：含 `#fff8ec`、`<script>` 被转义成实体、`digest.html` 与直接调用结果一致。
21. `evidence_sentence` 从「大家好。请各班班长今天18:00前把材料交到学工办。谢谢。」里挑出含 `18:00` 的那句，且不含「谢谢」。
22. `payload_to_item` 往返：摘要与 `deadline_dt` 与原条目一致，`evidence` 非空。
23. payload 保留结构化的 `audience`、`condition`、`details` 三项原值。
24. 「明天几点正式表演？」这类口语疑问不产生截止时间；`colloquial_question` 为真、分类不是 urgent。
25. 带指令的疑问句仍保留截止时间：「请大家明天下午5点前交材料，好吗？」→ `2026-09-29 17:00`。
26. 低优先级群：三句闲聊全部被丢弃（items 为空、`urgent_analyses` 为空）；同群的真通知保留 1 条，但**不进** `urgent_analyses`。
27. 超过 `max_items`（测试设为 2）时：`items` 为 2 条，`omitted_count` 等于「分析出的重点总数减 2」，正文与 `format_push_text` 都出现 `另有 {n} 条超出单次上限未展开`。
28. 未触发截断时 `omitted_count` 为 0，正文里不出现「未展开」。
29. 日期区间「2026年9月29日—2026年10月31日」取**结束日期**：`2026-10-31 23:59`。
30. `24:00` 归一到次日零点：`2026-10-09 24:00`、`2026-10-09T24:00:00`、`10月9日 24:00` 都得到次日 `00:00`；`24:30`、`2026-13-40 10:00` 仍判非法返回空。

---

## 3. 契约条目计数

| 模块 | 条目数 | 编号区间 |
|---|---|---|
| `store` | 91 | S-01 … S-91 |
| `summarizer` | 35 | M-01 … M-35 |
| **合计** | **126** | |

测试锁住的事实：store **30 条**（`tests/test_store.py` 31 个用例，归并同义项）、summarizer **30 条**（`tests/test_summarizer.py` 32 个用例，其中两条归并到同一条）。

---

## 4. 需要拍板（测试没说清、实现前必须定）

- **D-01 摘要模块的下层能力由谁提供。** 现在的抽象层下面还压着一个「消息分析内核」：分类/打分/标签、中文截止时间解析（含「今晚8点」「明天下午三点半」「24:00 次日零点」「日期区间取结束日」）、本地摘要与动作抽取、相似度与归一化去重、链接抽取、截止时间格式化。这些是摘要行为的地基，本批只规定**可观察结果**（见 2.8 的 15、18、24、25、29、30 条）。要定：它们是否单独出一份规格（建议出），还是并入本批由实现者自行设计。
- **D-02 `Message` 值对象的归属。** 摘要公开面接收/产出的是「消息值对象」（时间、发送者、正文、来源），现在来自另一个模块。重写时它属于哪个模块、字段名怎么定，需要和批次 A 的领域模型对齐。
- **D-03 精确文本算不算契约。** 推送正文的版式（`【QQ群通知汇总】`、缩进三空格、`截止：`/`需做：`/`链接：`/`原文：`/`（AI 摘要暂不可用，已用本地规则筛选）`）与 HTML 的色值（`#fff8ec`、`#d93025`、`#f29900`）都有测试断言。若产品允许改版式，请明确；否则实现者必须逐字符对齐。
- **D-04 `score` 与 `tags` 的算法。** `is_focus`、`urgent_analyses`、排序都用 `score` 与 `tags`，但它们的构造规则在分析内核里、本批未完全规定。重写时若算法不同，第 4 条测试（「今天天气不错」被分析成 urgent 但 urgent_analyses 为空）会因 score/tags 取值不同而红。需要把「打分与打标签规则」也纳入规格，或明确允许重算全部阈值。
- **D-05 LLM 提示词是否可以重写。** M-18 规定了输出协议与关键约束，但没逐字固定 system/user 文案。是否要求逐字一致？（测试只校验「能解析出 items、能裁剪字段」，不校验文案。）
- **D-06 `codebuddy` CLI 的接口。** 现在靠一个本地命令行（路径由环境键提供）返回 JSON。重写是否保留这条后端？保留则要定 CLI 的调用协议（参数、stdin/stdout、超时、失败退出码）。
- **D-07 摘要与待办的「分类→重要度」共享对象。** 测试用 `is` 断言两侧是**同一个对象**。重写若拆成两个模块，必须提供同一份常量实例（或明确改成值相等也可以，但要改测试——本批不建议动测试）。
- **D-08 老库的列默认值。** `urgent_override` 可空是刻意的三态语义；若重写把它改成非空默认 0，「跟随 AI 分类」这一态就没了（用户取消紧急后无法回到「跟随」）。建议保持可空。
- **D-09 任务删除的边界。** 手工新建（`manual:` 前缀）且仍 open 的任务允许真删，这是为了让用户删掉误建的任务；没有测试直接锁这条（测试只覆盖 open/candidate 被拒、finished 可删）。要确认这条例外继续保留。
- **D-10 `list_tasks` 的派生字段成本。** `source_text`、`candidate_detail`、`duplicate_summary` 是逐行子查询。数据量大时是性能隐患（历史上群名查询就踩过）。要不要改成按需拉取（界面只在详情里用）？
- **D-11 跨群去重的相似度阈值。** 现行为依赖「归一化 + 相似」两个函数，本批没规定阈值；阈值变了会让「只差一个句号的两条消息」不再合并（第 14 条测试会红）。需要把阈值写进规格。
- **D-12 `payload` 的键集合是否算对外契约。** 归档 payload 会被重新读出来渲染（`payload_to_item`）。若重写增删键，老库里的历史摘要渲染会退化。建议把 M-05 的 16 键当契约。
- **D-13 `counts()` 与 `delivery_stats()` 的口径。** 两处都只给固定键，未知状态被并入 `total` 但不单列。是否需要「unknown」桶？
- **D-14 坏库隔离的保留策略。** 每次损坏都留一份 `*.corrupt-*`，长期会堆积（用户磁盘）。要不要加保留上限（只留最近 N 份）？
