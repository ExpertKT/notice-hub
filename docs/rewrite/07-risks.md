# G1 · 风险与发现清单

发现清单只记录，不在 G1 改代码。

## 替换时最容易把行为做丢的地方

1. **规则接线在入库成功之后，且失败必须吞掉。** 今天 `_apply_rules` 和 `on_message` 的入库各自有兜底 `except`。重写若让规则异常冒泡，一条坏规则会让 QQ 接收线程丢掉消息。若让重复消息也跑规则，`task_key` 虽然能挡住重复行，却会多读一次规则文件。两条都要按 `02-module-map.md` 保住。
2. **建待办失败不能取消推送。** `_publish` 在 `mark_processed` 之后才 `_sync_tasks`。任务同步失败时消息已经不会再进下一批。用例必须继续投递，并保留「用 import_existing_tasks 补建」这条路。
3. **事件合并要先于资格判断。** 否则没有 deadline 的正文会在合并前被丢掉，beta.13 已经按这个顺序测过。
4. **已有待办再次写入时不改状态。** `upsert_task` 用一条 `INSERT … ON CONFLICT`。已有 `task_key` 时不写 `status`。完成、忽略、候选都会留下。`importance` 和 `confidence` 取较大值。`deadline`、`evidence`、`source` 的新值为空时不覆盖旧值。并发同键不再抛 `IntegrityError`。
5. **迁移只能加列，并且认 `user_version`。** `SCHEMA_VERSION = 1`。库版本高于 1 时 `RuntimeError` 拒绝启动，文案含「数据库结构版本 %d 高于本程序支持的 %d」。版本 0 的旧库加列后写成 1。改列类型或删列后，旧程序会因这条守卫起不来，这是有意的。
6. **200 与 `ok: false`。** 托管不可用、部分设置保存失败今天返回 HTTP 200。那是 Web 的契约，本次不改。用例层用 `ValueError` / `ok` 字段表达，由主线映射成现有状态码。
7. **任务槽是进程内全局。** 历史回填和分类共用一把锁。拆成两个独立后台任务会让取消和轮询对不上现有前端。
8. **群名缓存写在读路由里。** `list_napcat_groups` 与 `suggest_groups` 会 `remember_group_names`。端口把读和写分开之后，主线调用点仍要显式写，否则界面群名回退成群号。
9. **静默失效。** 项目里多次出现兜底 `except` 把 `NameError` 或断链变成绿灯。新代码里，失败路径要有日志，并且返回值能区分「没做」和「做成了」。规则与入库这两处的吞异常是刻意的，日志不能省。
10. **`test_webapp.py` 锁的是页面源码。** 重写方不改这个文件，也不要为了新结构去放宽里面的断言。页面分组若从 `webapp.group_tasks` 挪到用例，主线要让旧断言继续看到同一份 JSON。
11. **损坏库才隔离。** 只读 `integrity_check` 不是 `ok`，或打开错误含 `not a database` / `malformed` / `corrupt` / `encrypted`，才把库和 `-wal`、`-shm` 改名为 `.corrupt-<时间戳>`，撞名退 `-2`、`-3`，再建新库，并 `meta_set("recovered_from", ...)`。`locked`、`busy`、普通 I/O 错误不隔离。空文件不隔离。
12. **分类重要度只有一份。** `store.IMPORTANCE_BY_CATEGORY` 是 `{urgent: 5, action: 4, academic: 3, info: 3}`。手动改分类和摘要补重要度都用它。再抄一份旧表（`action` 3、`info` 1）会让重要度跳。
13. **每日库快照。** `tick` 里 `_maybe_snapshot`：同一天只做一次，文件在 `<data_dir>/snapshots/digest-YYYYMMDD.sqlite3`，用 SQLite `backup`。`SNAPSHOT_KEEP = 7`，更旧的删掉。失败只打 warning，不让调度失败。完成标记是 `meta` 键 `last_snapshot`。
14. **OneBot 空令牌按地址区分。** `is_loopback_host` 把空串、`localhost`、环回地址当成环回，解析不出的地址不当环回。非环回且 `QQ_DIGEST_ONEBOT_TOKEN` 为空：拒绝启动，不监听。环回且令牌为空：打 warning 后照常启动，请求放行。不要在启动时自动生成令牌，也不要把环回空令牌改成静默丢消息。

## 基线本身的缺口

| 项 | 状态 |
|---|---|
| 真实通知样本和旧实现输出基线 | 交接说明写明稍后提供。G2 在样本到达前以单测为对照 |
| 上游 `310a092` 之后是否还有新提交 | 边界报告取数时 `git fetch upstream` 被取消。申报或 G4 相似度自查前要重新 fetch |
| G2 双环境 | 本机尚未建 E1/E2。门禁定义在 `docs/handover/11-g2-two-env-gate.md` |
| `PAGE_SOURCE` 断言口径 | 主线材料问题。`1efa41e` 的 `tests/test_webapp.py` 里子串 `assertIn` 出现 829 次。主线回复写他们签过的分支上是 828。重写不清理这些断言 |
| `updates_api` 再导出约 30 个名字到 `webapp` | 为保住 `patch("qq_live_digest.webapp....")`。消掉与否由主线在 G3 定。重写方不改 |
| `service` 与 `webapp` 的运行时双向依赖 | architecture-map 4.4 未做。G2 的 application 不再 import Web；HTTP 怎么启动留到 G3 |
| 数据库损坏 | 真损坏会隔离重建并记 `recovered_from`。临时锁和忙不隔离。另有每日快照，只留 7 份。不在本次另做一套修复 |
| 浮球 | `FLOATBALL_ENABLED` 为关，路由还在。不重做 |

## 打包与更新

重写不改 `packaging/` 和 `updater.py`。最后一次重命名若碰到 spec 或 launcher 的 import，沿用现有打包约定：zip 顶层就是安装根、条目用 `/`、打包前清只读位、缺 `LICENSE` / `LICENSE-UPSTREAM` / `NOTICE` 则构建失败、无 sha256 的更新拒装。这些不在 G2 重做，避免把已经踩过的 Windows 打包问题再实现一遍。

## 合并

策略 B 仍有效：clean-room 不合回 `main`，直到 G4。本分支的代码吸收点是 `cb29a35`。`origin/main` 在这之后还有提交，动清单内文件之前再吸收。
