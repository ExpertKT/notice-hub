# G1 · 端口契约

实现放 `noticehub.infrastructure`。本文件是 Web 与用例允许依赖的全部存储和外部能力。方法名是契约，G2 可以让旧 `Store` 做适配层来满足它们。

类型用下面的字段集描述。落地时用 `dataclass`，字段名与 SQLite 列和现有 JSON 键一致，方便主线把 `dict` 调用点换过来。

## Clock

```text
now() -> datetime          # naive 本地时间，与今天的 now_local() 相同，不带 tzinfo
```

领域代码接收 `datetime`，不自己取时钟。测试注入固定时钟。

## 数据库

文件仍是 `<data_dir>/digest.sqlite3`，WAL。`SCHEMA` 里 10 张表：`messages`、`processed`、`digests`、`deliveries`、`meta`、`tasks`、`task_events`、`inbox_marks`、`group_names`、`dingtalk_sync`。

学习通同步状态存在 `dingtalk_sync.key = chaoxing_state`，不另建表。

迁移仍只能加列：`PRAGMA table_info` 差集后 `ALTER TABLE ADD COLUMN`。禁止改列类型、删列、重命名列。新列只能追加，并带默认值。

库结构版本记在 `PRAGMA user_version`。代码常量 `SCHEMA_VERSION = 1`。

- 读不到库、库还不存在：版本按 0。
- 版本为 0：加列之后写成 1。
- 版本等于 1：不动版本号。
- 版本高于 1：`RuntimeError`，拒绝启动。文案为「数据库结构版本 %d 高于本程序支持的 %d，请升级 notice-hub 后再启动：%s」。不要打开后继续写。

启动时先做只读 `PRAGMA integrity_check`。只有库确实损坏才隔离重建。`locked`、`busy`、I/O 这类临时错误不隔离。损坏判定：`integrity_check` 不是 `ok`，或打开错误信息里含 `not a database`、`malformed`、`corrupt`、`encrypted`。空文件和文件不存在不算损坏。

隔离时把库文件以及旁边的 `-wal`、`-shm` 改名为 `<原名>.corrupt-<YYYYMMDD-HHMMSS>`。同名已存在则退成 `-2`、`-3`，不覆盖上一份。然后建新库，并 `meta_set("recovered_from", 隔离文件名)`。失败只记错误日志。Web 的 `GET /api/backup` 仍归主线。

`data/tasks.sqlite3` 无代码引用，端口不读它。

### TaskRepository

任务状态只允许 `candidate | open | done | dismissed | expired`。

```text
list_tasks(include_done: bool = True, statuses: Sequence[str] | None = None, limit: int | None = 800) -> list[Task]
get_task(task_id: int) -> Task | None
upsert_task(draft: TaskDraft) -> int          # 空 task_key 返回 0。一条 INSERT … ON CONFLICT 完成。已存在的 task_key 不改 status；importance 与 confidence 取较大值；deadline、evidence、source 仅在新值非空时覆盖
apply_action(task_id: int, action: TaskAction, detail: Any = "") -> bool
apply_correction(task_id: int, correction: TaskCorrection, value: Any = "") -> bool
set_urgent(task_id: int, value: bool | None) -> TaskResult
set_pinned(task_id: int, value: bool) -> TaskResult
stats() -> TaskStats
correction_stats(days: int = 30) -> CorrectionStats
correction_insights(days: int = 30, min_group_hits: int = 3) -> list[str]
mark_reminded(task_id: int, detail: Any = "", when: datetime | None = None) -> bool
```

`TaskAction`：`confirm | dismiss | done | snooze | reopen`。未知动作抛 `ValueError`。不存在的任务返回 `False`。

`snooze` 的 `detail["until"]` 可解析则用它，否则 `Clock.now() + 1 day`。

`TaskCorrection`：`not_notice | not_task | not_urgent | duplicate | category | deadline | clear_deadline`。未知或非法分类抛 `ValueError`。

`set_urgent` 的 `value` 只接受 `bool | None`，否则 `ValueError`。任务不存在时返回 `ok=false` 且 `error="任务不存在"`，这是现有函数的返回，HTTP 层不要改成 404，除非主线同时改前端。

`set_pinned`：已完成任务拒绝置顶，返回现有错误文案。

`TaskStats` 键：`total, candidate, open, done, dismissed, expired, overdue`。`overdue` 是 `status=open` 且 `deadline` 非空且早于 `Clock.now()`。

`Task` 至少包含今天 `tasks` 行的列，外加 `effective_urgent`（`urgent_override` 非空则用覆盖值，否则 `category == urgent`）、`candidate_detail`、`source_text`、`duplicate_summary`。后三个是 `list_tasks` / `get_task` 现有的相关子查询，Web 分组依赖它们。

`Store` 没有 `list_events`。`task_events` 只在现有写路径里追加。端口不新增读接口。

`CorrectionStats` 键：`days, total, by_type, by_group`。`by_type` 是纠错类型到次数。`by_group` 是群名到 `{纠错类型: 次数, total}`。只统计 `task_events.event = corrected` 且 `created_at` 不早于现在减去 `days` 的行。`correction_insights` 只生成建议文本，不改配置。群的 `total` 达到 `min_group_hits`（至少按 2 封底）才出一条；全文「重复」达到 3 次时另加一句去重时间窗建议。文案保持现有两句模板。

### MessageRepository

```text
insert_message(event: MessageEvent) -> bool    # 新插入 true；同一 msg_id 再来 false
unprocessed(limit: int = 400) -> list[StoredMessage]
mark_processed(msg_ids: Sequence[str], digest_id: int | None) -> int
oldest_unprocessed() -> str
```

`messages.jsonl` 仍在插入成功时追加。这是审计旁路，不算第二数据库。

### DigestRepository

```text
insert_digest(...) -> int
get_digest(digest_id: int) -> DigestRecord | None
recent_digests(limit: int = 5) -> list[DigestRecord]
```

`digests.jsonl` 随插入追加。

### DeliveryRepository

```text
claim_delivery(...) -> int | None
pending_deliveries(max_attempts: int, retry_seconds: int) -> list[Delivery]
mark_delivery(delivery_id: int, ok: bool, error: str = "") -> None
increment_attempt(delivery_id: int) -> int
```

去重仍是 `UNIQUE(dedupe_key, channel, target)`。

### InboxRepository

```text
save_mark(msg_id, verdict, reason="", method="", task_id=None) -> None
unmarked_messages(since, until, groups, limit=1000) -> list[StoredMessage]
inbox_items(...) -> list[InboxItem]
inbox_counts() -> dict[str, int]
```

分类与提升是核心用例。标记写 `inbox_marks`。

### GroupNameRepository

```text
remember(names: Mapping[str, str]) -> int
names() -> dict[str, str]
label(group_id, names, aliases) -> str
```

`GET /api/napcat/groups` 与 `GET /api/groups/suggest` 会在读路径上写群名缓存。这个副作用留在对 `remember` 的显式调用里，仓储方法本身不在查询时写。

### MetaRepository

```text
get(key: str, default: str = "") -> str
set(key: str, value: str) -> None
```

推送预算、LLM 降级计数、向导接受时间继续用 `meta` 表。键名不变。

## Notifier

```text
send_digest(digest, dedupe_key: str) -> list[DeliveryOutcome]
test_channels() -> list[ChannelTest]
channels() -> list[str]     # qq-bot, wxpusher, serverchan, pushplus, webhook
```

无通道时 `send_digest` 不抛，返回空成功集；用例记「仅归档」。这和今天「没有可用推送通道」的日志一致。

`test_channels` 对应 `POST /api/settings/test-push`。返回每条 `{channel, target, ok, error}`。

## GroupHistory

QQ 历史补采。实现仍调用 NapCat。

```text
probe_floor(group_id: str) -> FloorResult
fetch(groups: Sequence[str], since: date | None, until: date | None) -> HistoryBatch
```

`FloorResult`：`ok, floor_ts, total_seen, error`。

## RuleSet

```text
load() -> list[Rule]
save(rules) -> None          # 先整体解析，任一条非法则一个字节都不写
```

文件仍是 `<data_dir>/rules.json`。`match` 是纯函数，放 `domain`，不放 port。

## 不进本次端口的能力

微信内存读取、钉钉本地库、学习通 HTTP、NapCat 进程、防火墙、Tailscale、更新下载，继续由现有模块提供。application 的核心用例不把它们收成新端口。主线 Web 仍直接调用那些模块，直到另一次改动。

## 旁路文件（端口实现必须继续读写）

| 路径 | 谁写 |
|---|---|
| `messages.jsonl`、`digests.jsonl` | 对应仓储 |
| `rules.json` | RuleSet |
| `chaoxing.cookie` | 现有学习通模块（保留） |
| `qrcode.png`、`hosting.json`、`firewall.request` | 现有 NapCat / 托管 / Web（保留） |
| `updates/state.json`、`updates/pending.json` | 现有更新模块（保留） |
| `service.lock` | CLI 单实例锁（保留在 `main.py` 的并存期） |
