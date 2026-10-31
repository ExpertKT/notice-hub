# 09-clean-room-request.md — 真重写（clean-room）请求

写给重写方。用户 2026-10-31 已决定：**架构重写已收货，真重写这部分交给你们**。

## 0. 先说清两件事不是一件

`rewrite/clean-room` 做的事是把实现分层、搬进 `noticehub`，旧模块变再导出。这是**架构**上的独立，已验收。

但它不是**表达**上的重写。用逐行比对（去掉空白后取行集合，算新文件里有多少行原样出现在旧文件里）：

| 新文件 | 旧文件 | 新文件行数 | 与旧文件逐字重合 |
|---|---|---|---|
| `noticehub/infrastructure/sqlite/store.py` | `qq_live_digest/store.py` | 1025 | **99%**（1019 行） |
| `noticehub/bootstrap/settings.py` | `qq_live_digest/config.py` | 177 | 85% |
| `noticehub/application/attachments.py` | `qq_live_digest/attachments.py` | 208 | 85% |

搬过去的代码在法律上仍是上游（`wc985732-lang/qq-live-digest`，MIT）的衍生作品，所以 `NOTICE` 与 `LICENSE-UPSTREAM` 现在还不能删。用户要的是删掉，因此需要把衍生表达换成你们自己写的表达。

## 1. 范围（数字是主线核过的，按分叉点 `310a092` 逐文件算）

上游分叉点 `310a092` 存在、且现在仓库里仍在的代码文件共 33 个。其中产线代码 16 个，残留在我们当前代码里的行数如下（`now` = 当前文件非空行，`shared` = 其中原样出现在分叉点版本里的行）：

| 文件 | now | shared | 占比 |
|---|---|---|---|
| `qq_live_digest/attachments.py` | 1287 | 1144 | 89% |
| `qq_live_digest/bot.py` | 192 | 192 | 100% |
| `qq_live_digest/catchup.py` | 248 | 196 | 79% |
| `qq_live_digest/config.py` | 719 | 456 | 63% |
| `qq_live_digest/logging_setup.py` | 42 | 36 | 86% |
| `qq_live_digest/push.py` | 430 | 360 | 84% |
| `qq_live_digest/receiver.py` | 302 | 279 | 92% |
| `qq_live_digest/retry.py` | 65 | 65 | 100% |
| `qq_live_digest/service.py` | 1394 | 1022 | 73% |
| `qq_live_digest/store.py` | 1406 | 1129 | 80% |
| `qq_live_digest/summarizer.py` | 662 | 638 | 96% |
| `qq_live_digest/timeutil.py` | 43 | 43 | 100% |
| `qq_live_digest/webapp.py` | 4525 | 1022 | 23% |
| `qq_live_digest/weburl.py` | 33 | 33 | 100% |
| `main.py` | 436 | 421 | 97% |
| `qq_digest.py` | 1327 | 1182 | 89% |

合计 **约 8,218 行**需要换成新表达。另有 **11 个测试文件**（`tests/__init__.py` 与 `test_attachments catchup config push receiver retry service store summarizer webapp`）同属范围，逐批按同一方法核对。

`webapp.py` 只有 23% 是上游的，不必整文件重写；剩下 77% 是我们自己的，重写时照搬自己的部分没有法律问题，但**新旧混杂会让相似度统计失去意义**，所以建议按「路由处理函数」为单位整块重写，别只挑几行换措辞。

## 2. 每批的作业方式（clean-room 纪律，缺一条这批不算过）

1. **先写规格**：`docs/rewrite-spec/<模块>.md`，只写行为——输入输出、边界、错误、持久化形状、时序。**规格里不许贴旧代码片段**。
2. **实现的人不读旧实现**：新文件从空开始写，只读规格与现有测试。
3. **三道验收**，证据写进交接文档：
   - 新写的测试跑**旧实现**必须绿（证明测试抓住了行为）；
   - 旧测试跑**新实现**必须绿（证明行为没变）；
   - 新文件与 `310a092` 版本的相似度复查，贴数字（目标：产线文件 < 10%，且重合的只剩空行、括号、`import` 这类无表达力的行）。
4. 旧测试文件在新测试接管后才删；删之前给「旧用例 → 新用例」对照表。

## 3. 不许动的契约（动了整批退回）

- HTTP 路由、状态码、JSON 键、错误文案——现有测试锁着，改写它们等于改产品行为。
- 数据库表结构、迁移、列名、日志名。
- 命令名与参数（`python main.py run|tick|preview|send-test|doctor|stats|catchup|tasks|show|attach-test`，`--env` / `--poll` / `--no-bot` / `--limit`）。
- 环境变量**键名**暂不动：`QQ_DIGEST_*` 的改名与旧名兼容由主线在改名批次统一做，你们这一轮继续读旧键。

## 4. 谁做什么

- **重写方**：`noticehub/` 内的实现与对应测试、`docs/rewrite-spec/**`、你们的交接文档。**不碰** `main.py`、`packaging/**`、`.github/workflows/**`、`web_static/**`、`qq_live_digest/version.py`。
- **主线**：合并、包名与目录最终形态、环境变量改名前缀与旧名兼容、打包/CI/启动器接线、许可证与文档收口、发布。

## 5. 动手前必须先做的一件事

`origin/main` 已经到 `50dfe40` 之后，并且主线刚把你们的分支合进主线解完冲突（5 个文件）。**请先把最新 `origin/main` 合回 `rewrite/clean-room` 再开始重写**，否则你们会照旧基线重写，把我们 10-31 这批（配置体检、NapCat 自动连、图片落盘、端口诊断、文案去术语共 575 行）重写丢。

## 6. 顺序建议（叶子到枢纽）

1. `timeutil` `weburl` `retry` `logging_setup`
2. `store` `summarizer`
3. `config` `receiver` `push` `catchup` `bot`
4. `attachments`
5. `service`
6. `webapp`（按路由处理函数分块）
7. `main.py` `qq_digest.py` 与 4 个 `.ps1` + `tools/local_smoke.py`
8. 11 个测试文件（随实现逐批替换，最后统一清尾）

## 7. 主线怎么验收

每批你们交：全量 `python -m unittest discover -s tests -t .` 的 `Ran N / OK / 退出码`、相似度数字、改动清单。
主线会独立复跑全量门禁，并在你们的基础上再查一遍相似度——**自述「本地绿」不算数**。
