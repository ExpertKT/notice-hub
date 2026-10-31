# G1 · 迁移方案

两阶段替换，包名改动放在最后一次提交。G1 结束后主线可以按 `04-application.md` 改 Web 调用点，不必等 G2 写完。

## 阶段 0 · 现在

主线已签「够接 Web」。行为基线已吸收到当前 `origin/main`。G2 第 1 到第 8 步已完成：CLI 动作在 `noticehub.bootstrap.cli`，`main.py` 仍提供原来的 9 个子命令。单实例锁留在 `main.py`。包名改动是最后一次提交，现在还不动。

`main` 继续按策略 B 发版。clean-room 在 G4 之前不合回 `main`。`main` 上新的提交，重写方在改清单内文件之前先快进吸收。若两边改了同一个清单内文件，停下来把差异记进发现清单，不在重写分支里重做主线已经发布的行为。

## 阶段 1 · 并存（G2）

新包 `noticehub` 与 `qq_live_digest` 同时存在。

顺序：

1. `Clock`、时间纯函数、`Settings.from_legacy`。旧 `from_env` 转调它，跑 `test_config`。
2. SQLite 仓储实现，旧 `Store` 方法转调仓储。表和迁移不动。跑 `test_store`。
3. 领域类型：`MessageEvent`、待办资格、事件合并、规则 `match`。不写库。
4. `accept_message`、`tick`、规则接线、`import_existing_tasks`。旧 `DigestService` 的这些方法转调用例。跑 `test_service`、`test_rules`。
5. 通知器与投递仓储。跑 `test_push`。
6. QQ 适配器只产出 `MessageEvent`。跑 `test_receiver`。
7. 历史补采与附件编排。跑 `test_catchup`、`test_attachments`。
8. CLI 子命令转调 bootstrap。`main.py` 的对外命令保持。

每步旧模块变成薄适配，全量 `python -m unittest discover -s tests -t .` 保持绿。已有断言不放宽。

单次提交不超过 400 行。超过就拆开。

G2 验收环境按 `docs/handover/11-g2-two-env-gate.md`：无软依赖与全依赖各跑一遍，skip 不算通过。样本和旧输出基线尚未提供；在它们到达之前，G2 以现有单测为行为对照，不宣称与生产样本逐条一致。

## 阶段 2 · 切换调用点（G3，主线）

主线把 `webapp` 里对 `self.store`、`group_tasks`、`overview` 的核心路径改成 `04-application.md` 的函数。路由路径、状态码、JSON 键不变。

`service.py` 不再 import `webapp`。HTTP 服务由 bootstrap 启动，Web 需要的回调（扫描、重载各来源、settings）经构造参数注入。

这一阶段删旧实现只发生在调用点已经全部切走、全量测试仍绿之后。

## 阶段 3 · 重命名（最后一个提交）

最终形态以 [08-remaining-and-g4.md](08-remaining-and-g4.md) 为准：方案 B，`qq_live_digest` 不删除。已经迁走的实现留在 `noticehub`，旧文件里的再导出继续留着。`main.py`、`packaging/*.spec`、`launcher.py`、CI 不因为这一步改 import。`APP_VERSION` 仍只在 `qq_live_digest/version.py`。

不把包名改动拆进前面的功能提交，避免 bisect 停在 import 断裂的中间态。

## 主线现在就能用的部分

G3 可以先对着契约写适配，用并存期的旧 `Store` 做端口实现，等 G2 的 SQLite 实现替换。Web 不需要知道 SQLite。

主线不要把 `webapp` 逻辑搬进 `noticehub.application`。用例签名以本目录为准。若契约不够覆盖某条核心路由，在发现清单补一行，改契约，再改调用点。
