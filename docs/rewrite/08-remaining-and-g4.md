# 剩余迁移与 G4

回答主线 `noticehub-剩余迁移与G4.md` 的四件阻塞。基线是 `rewrite/clean-room` 的 `824c032`。本文不改运行代码，不合 `main`，不改包名。

不迁分三种，不全是「以后再迁」：

1. 外部集成或桌面/更新链。`docs/rewrite/01-architecture.md` 写明这一轮不拆。
2. 兼容层。正本已在 `noticehub`，旧文件只为旧 import 和测试 patch 留着。
3. 正本仍在旧包。Web 入口、调度宿主、推送 HTTP、NapCat 客户端、日历渲染属于这一轮的终点，不进 `noticehub`。

## 1. 51 个模块

旧路径都在 `qq_live_digest/`。新路径空着表示正本就是旧文件。

| 旧路径 | 处置 | 新路径 | 理由 | 依据 |
|---|---|---|---|---|
| `__init__.py` | 不迁 |  | 方案 B 下旧包还在，包说明留在这里。不是兼容层，也不进新分层。 | `qq_live_digest/__init__.py` |
| `announce.py` | 不迁 |  | 外部集成：公告拉取。失败不抛异常。归主线，重写不收。 | `qq_live_digest/announce.py`；`docs/rewrite/02-module-map.md` |
| `attachments.py` | 拆分 | 编排：`noticehub/application/attachments.py`。解析、下载、视觉识别、`cleanup_files`：仍是本文件 | 两处同名。队列类 `AttachmentWorker` 的正本在 `noticehub`，本文件第 970 行再导出这个名字。解析函数和 `Attachment` 的正本仍是本文件，因为测试 patch `qq_live_digest.attachments` 的 `download`、`chat_completion`、`image_bytes_text`、`urllib.request.urlopen`。两边都不删。 | `qq_live_digest/attachments.py`；`noticehub/application/attachments.py` |
| `bot.py` | 拆分 | 群事件归一：`noticehub/adapters/qq/onebot.py`。WebSocket 客户端：仍是本文件 | 官方机器人进程留在旧模块。只把群消息收成 `MessageEvent` 的函数迁走。 | `qq_live_digest/bot.py`；`noticehub/adapters/qq/onebot.py` |
| `browser_cookie.py` | 不迁 |  | 外部集成：从本机浏览器读学习通 Cookie。Windows CNG，这一轮不收。 | `qq_live_digest/browser_cookie.py`；`docs/rewrite/02-module-map.md` |
| `catchup.py` | 拆分 | 补采编排：`noticehub/application/history.py`。`NapCatClient`：仍是本文件 | HTTP 客户端是正本。`available_floor`、`backfill`、`backfill_range`、`history_to_record` 从用例再导出，测试仍 patch `qq_live_digest.service.backfill`。 | `qq_live_digest/catchup.py`；`noticehub/application/history.py` |
| `chaoxing_receiver.py` | 不迁 |  | 外部集成：学习通只读采集。 | `qq_live_digest/chaoxing_receiver.py`；`docs/rewrite/01-architecture.md` |
| `community.py` | 不迁 |  | 外部集成：在线人数计数。失败不当错误。 | `qq_live_digest/community.py`；`docs/rewrite/02-module-map.md` |
| `config.py` | 拆分 | 字段装配：`noticehub/bootstrap/settings.py`。`.env` 读写、`RUNTIME_ROOT`、`from_env`：仍是本文件 | `from_legacy` 转调 `build_settings`。原子写和 `remove_env_keys` 留在这里。测试 patch `config.os.name`、`config._default_web_host`、`config.RUNTIME_ROOT`，这些查找必须留在本文件。启动器也从这里取 `Settings`。 | `qq_live_digest/config.py`；`noticehub/bootstrap/settings.py`；`packaging/launcher.py` |
| `dingtalk_receiver.py` | 不迁 |  | 外部集成：钉钉本地库只读。 | `qq_live_digest/dingtalk_receiver.py`；`docs/rewrite/01-architecture.md` |
| `doctor.py` | 不迁 |  | 正本：诊断 zip。只读数据目录，脱敏后打包。CLI 只调用它，不把打包逻辑搬进 bootstrap。 | `qq_live_digest/doctor.py` |
| `event_extract.py` | 不迁 |  | 正本：实验室的纯规则抽事件。默认不进摘要主路径。算法不改、不搬。 | `qq_live_digest/event_extract.py`；`docs/rewrite/02-module-map.md` |
| `floatball_host.py` | 不迁 |  | 外部集成：Edge/Win32 浮球宿主。 | `qq_live_digest/floatball_host.py`；`docs/rewrite/02-module-map.md` |
| `floatball_page.py` | 不迁 |  | 外部集成：浮球页的 HTML 字符串。 | `qq_live_digest/floatball_page.py` |
| `floatball_window.py` | 不迁 |  | 外部集成：tkinter 浮球窗口。 | `qq_live_digest/floatball_window.py` |
| `group_suggest.py` | 不迁 |  | 正本：用模型给订阅群提建议，带启发式退路。不进这一轮分层。 | `qq_live_digest/group_suggest.py` |
| `host_actions.py` | 不迁 |  | 外部集成：提权、防火墙、宿主审计。启动器直接 import。 | `qq_live_digest/host_actions.py`；`packaging/launcher.py` |
| `hosting.py` | 不迁 |  | 外部集成：托管时关/开用户自己的 QQ。启动器与网页共用这一份。 | `qq_live_digest/hosting.py`；`packaging/launcher.spec` |
| `ics.py` | 不迁 |  | 正本：唯一的 iCalendar 渲染。用例只取待办再调用 `render_calendar`。`noticehub` 没有第二份。测试 import 本模块并 patch `_local_tzinfo`。 | `qq_live_digest/ics.py`；`noticehub/application/calendar.py` |
| `inbox.py` | 不迁 |  | 正本：收件箱分类和 `promote`。用例只做校验并调用注入的 `promote`。`task_key` 仍是裸 `msg_id`。 | `qq_live_digest/inbox.py`；`noticehub/application/inbox_item.py` |
| `lab.py` | 不迁 |  | 正本：实验室开关门面。惰性 import，失败收成 `ok: false`。 | `qq_live_digest/lab.py`；`docs/rewrite/02-module-map.md` |
| `llm_json.py` | 不迁 |  | 正本：从模型输出里抠 JSON。无 I/O。收件箱在用，这一轮不搬。 | `qq_live_digest/llm_json.py` |
| `logging_setup.py` | 不迁 |  | 正本：服务进程的轮转日志。启动器不写这份文件。 | `qq_live_digest/logging_setup.py` |
| `messages.py` | 不迁 |  | 正本：安装引导等用户可见句子。页面 HTML 里还有一份，本轮不动。 | `qq_live_digest/messages.py` |
| `napcat_admin.py` | 不迁 |  | 外部集成：NapCat 发现、安装、登录。 | `qq_live_digest/napcat_admin.py`；`docs/rewrite/01-architecture.md` |
| `pairing.py` | 不迁 |  | 外部集成：手机配对短码。点允许才发令牌。 | `qq_live_digest/pairing.py`；`docs/rewrite/02-module-map.md` |
| `push.py` | 拆分 | 投递去重：`noticehub/infrastructure/notify.py` 的 `PushManager`。通道类和 `post_json`：仍是本文件 | 测试 patch `qq_live_digest.push.post_json`。HTTP 通道不搬。 | `qq_live_digest/push.py`；`noticehub/infrastructure/notify.py` |
| `qr.py` | 不迁 |  | 外部集成：无第三方库的二维码编码，给配对页用。 | `qq_live_digest/qr.py` |
| `receiver.py` | 拆分 | 事件归一：`noticehub/adapters/qq/onebot.py`。HTTP 接收和 `_Handler`：仍是本文件 | 测试查看 `_Handler._authorized` 的源码，要求 `hmac.compare_digest` 至少两处，且 logger 名是 `qq_live_digest.receiver`。`_Handler` 不能离开本文件。 | `qq_live_digest/receiver.py`；`noticehub/adapters/qq/onebot.py` |
| `replies.py` | 不迁 |  | 正本：实验室「待回」清单。默认不进主流程。 | `qq_live_digest/replies.py` |
| `retry.py` | 不迁 |  | 正本：只重试可恢复的 HTTP 错误。映射文档允许以后变成策略对象，这一轮没搬，G4 也不搬。 | `qq_live_digest/retry.py`；`docs/rewrite/02-module-map.md` |
| `routes.py` | 不迁 |  | 正本：路由表。只放路径、处理函数名、免鉴权标记。不 import `webapp`。G4 不改 61/39/1/1。 | `qq_live_digest/routes.py` |
| `rules.py` | 拆分 | 纯 `parse` / `match`：`noticehub/domain/rules.py`。`rules.json` 读写：仍是本文件 | `import re` 必须留在本文件。测试 patch `qq_live_digest.rules.re.compile`。 | `qq_live_digest/rules.py`；`noticehub/domain/rules.py` |
| `search.py` | 不迁 |  | 正本：实验室全文检索。不注册路由，默认关闭。 | `qq_live_digest/search.py` |
| `selfcheck.py` | 不迁 |  | 正本：自检行的状态判断。设置页和自检共用。 | `qq_live_digest/selfcheck.py`；`docs/rewrite/02-module-map.md` |
| `service.py` | 拆分 | `tick`、`import_existing_tasks`、`accept_message`、`run_catchup`：`noticehub/application/schedule.py`、`ingest.py`、`history.py`。`DigestService` 宿主：仍是本文件 | 宿主还管线程、机器人、补采状态和健康输出，并在调用时把 `now_local`、`backfill` 注进用例。不把宿主再搬进 application，否则测试 patch `qq_live_digest.service.now_local` 会落空。 | `qq_live_digest/service.py`；`noticehub/application/schedule.py`；`noticehub/application/history.py`；`noticehub/application/ingest.py` |
| `settings_api.py` | 不迁 |  | 正本：设置九域写入。试推那一个方法改叫 `test_push_channels`，其余没动。 | `qq_live_digest/settings_api.py`；`noticehub/application/push_test.py` |
| `store.py` | 不迁 | 正本：`noticehub/infrastructure/sqlite/store.py` | 兼容层，23 行再导出。删除条件：仓库里不再 `from qq_live_digest.store import`，也不再 patch 这条路径。由主线在那之后单独提交删除。这一轮不删。 | `qq_live_digest/store.py`；`noticehub/infrastructure/sqlite/store.py` |
| `summarizer.py` | 拆分 | 资格判断：`noticehub/domain/eligibility.py`。摘要拼装：仍是本文件 | 拼装仍被 `noticehub/application/schedule.py` 调用。正本不搬。 | `qq_live_digest/summarizer.py`；`noticehub/domain/eligibility.py`；`noticehub/application/schedule.py` |
| `timeutil.py` | 不迁 | 纯函数：`noticehub/domain/time.py`。`now_local`：本文件转调 `SystemClock` | 兼容层。测试和用例仍从本模块取 `iso` / `now_local`。删除条件与 `store.py` 相同，由主线单独提交。这一轮不删。 | `qq_live_digest/timeutil.py`；`noticehub/domain/time.py`；`noticehub/infrastructure/clock.py` |
| `ttl_cache.py` | 不迁 |  | 正本：进程内 TTL 缓存。只读探针共用。 | `qq_live_digest/ttl_cache.py` |
| `tunnel.py` | 不迁 |  | 外部集成：Tailscale Funnel。 | `qq_live_digest/tunnel.py`；`docs/rewrite/02-module-map.md` |
| `update_signing.py` | 不迁 |  | 外部集成：更新清单 RS256 验签。只验不签。 | `qq_live_digest/update_signing.py` |
| `update_sources.py` | 不迁 |  | 外部集成：GitHub / Gitee 更新地址常量。 | `qq_live_digest/update_sources.py` |
| `updater.py` | 不迁 |  | 外部集成：查版本、下载、校验、覆盖安装。网页和启动器共用。公开函数不抛异常。 | `qq_live_digest/updater.py`；`packaging/launcher.py` |
| `updates_api.py` | 不迁 |  | 正本：更新和公告的 HTTP 域。为保住 `patch("qq_live_digest.webapp....")` 才从 `webapp` 拆出。G4 不合并回去。 | `qq_live_digest/updates_api.py` |
| `version.py` | 不迁 |  | 正本：`APP_VERSION` 与 `APP_CHANNEL` 的唯一出处，现为 `1.1.0-beta.15` / `beta`。G4 不挪到 `noticehub.version`。 | `qq_live_digest/version.py`；`docs/rewrite/01-architecture.md` |
| `webapp.py` | 不迁 |  | Web 入口正本。见第 3 节。不建 `noticehub/web`。`web_static/` 随它留下，不在这 51 个里。 | `qq_live_digest/webapp.py`；`docs/rewrite/01-architecture.md` |
| `weburl.py` | 不迁 |  | 正本：局域网地址探测。网页在用。映射文档允许进 domain，这一轮没搬。 | `qq_live_digest/weburl.py`；`docs/rewrite/02-module-map.md` |
| `wechat_receiver.py` | 不迁 |  | 外部集成：微信本地库只读。 | `qq_live_digest/wechat_receiver.py`；`docs/rewrite/01-architecture.md` |
| `win_text.py` | 不迁 |  | 外部集成：把 Windows 控制台的 GBK 字节解成文本。NapCat 安装失败路径依赖它。 | `qq_live_digest/win_text.py` |

51 行到此为止。`web_static/`、`main.py`、`packaging/`、`android/` 不是这 51 个模块，不进表。

## 2. G4 的最终形态

选方案 B。

方案 A 要求 `qq_live_digest` 整目录消失，表里不能留「这一轮不迁」的正本。上面有 Web、微信、钉钉、学习通、NapCat、托管、更新、浮球这些正本。`docs/rewrite/01-architecture.md` 写的是这些保持原模块，并且不建 `noticehub/web`。选 A 等于把已签的范围外收进新包。不选。

方案 B 的意思：

- G4 不删除 `qq_live_digest`。
- 已经迁走的实现留在 `noticehub`。旧文件里已经是再导出的，继续当兼容层。
- 表里写「正本仍是旧文件」的，import 保持 `qq_live_digest.*`。
- 不存在「先把 51 个都搬走，再改名」的提交。两套目录并存是这一轮的终点，不是未做完。

G4 那一次提交由主线做。重写方不改 `main.py`、`packaging/**`、`launcher.py`、CI，也不为此申请授权。

这一次提交实际要改的运行代码是空的。核对结果：

- `main.py` 已经 `from noticehub.bootstrap.cli import ...`。命令名和 `acquire_lock` 不动。
- `packaging/launcher.py` import 的是 `hosting`、`config`、`host_actions`、`floatball_host`、`floatball_window`、`updater`。这些都是不迁的正本。文件里没有 `APP_VERSION`。
- `packaging/launcher.spec` 的 hiddenimports 是 `pystray` 和 `tkinter`。注释写启动器 import `qq_live_digest.hosting` / `qq_live_digest.config`。方案 B 下路径不变。
- `packaging/qq_live_digest.spec` 的 datas 指向 `qq_live_digest/web_static`。hiddenimports 是 `pypdfium2` 与 PIL 收集结果。页面不搬，datas 不改。
- `.github/workflows/tests.yml` 没有包名，只跑 `python -m unittest discover -s tests -t .`。
- `APP_VERSION` 留在 `qq_live_digest/version.py`。不新增 `noticehub/version.py`。`QQ-Notice-Hub.exe` 和 `QQ_DIGEST_*` 不改。

所以 G4 没有一份必须再改的 import 清单。主线按第 8 节验收本文后，合并 `rewrite/clean-room` 进 `main` 就是收口。合并仍由主线做。重写方不合。

兼容层（`store.py`、`timeutil.py`，以及 `attachments.py` 里再导出的 `AttachmentWorker`）什么时候删：不在 G4，也不在这次合并。条件是该文件只剩再导出，并且测试不再 import 或 patch 旧路径。由主线之后单独提交，一次一个模块，全量门禁仍要绿。这一轮不排这个删除。

依据：`docs/rewrite/06-migration.md`、`docs/rewrite/01-architecture.md`、`main.py`、`packaging/launcher.py`、`packaging/launcher.spec`、`packaging/qq_live_digest.spec`、`.github/workflows/tests.yml`、`qq_live_digest/version.py`。

## 3. `webapp.py`

保持现状。仍是单个 `qq_live_digest/webapp.py`。不拆成 `handlers/`，不迁进 `noticehub`，这一轮也不给收缩行数，因为不改这个文件。

18 条用例的调用已经在里面。剩下的是鉴权、网卡和隧道探测、防火墙、向导、NapCat、托管页面。那些是表里的不迁正本，不是可以删的重复逻辑。路由表已经在 `routes.py`，设置域在 `settings_api.py`，更新域在 `updates_api.py`。再拆一次会改 `tests/test_webapp.py` 的源码断言和 `patch("qq_live_digest.webapp....")`，单次也放不进 400 行。

依据：`qq_live_digest/webapp.py`、`qq_live_digest/routes.py`、`qq_live_digest/settings_api.py`、`qq_live_digest/updates_api.py`、`docs/rewrite/01-architecture.md`。

## 4. 五处契约偏差

五条都接受现状。契约改向现有测试，不改 HTTP。`docs/rewrite/04-application.md` 里原来写「钳」和没写 404 的句子已按下面改正。

1. 接受。任务不存在时用例返回 `ok: false` 和「任务不存在」。`POST /api/tasks/urgent`、`POST /api/tasks/pin` 的 HTTP 是 404。已完成不能置顶也是 404。`tests/test_webapp.py` 锁的是 404。不改成 200。
2. 接受。`limit` 超出 1–20000 仍是 400「提取条数必须在 1 到 20000 之间」。不钳制。
3. 接受。`POST /api/inbox/classify/cancel` 留在网页里。没有任务在跑时回 200。历史取消才是 `cancel_job`，那时没有在跑的历史任务回 409「当前没有正在运行的回溯任务」。
4. 接受，并更正前提。没有 `noticehub/domain/calendar.py`，也没有第二套渲染。正本只有 `qq_live_digest/ics.py` 的 `render_calendar`。用例 `render_calendar_ics` 取 `list_tasks(limit=None)` 后调用它。不迁。若以后迁，旧模块必须改成再导出，让 `qq_live_digest.ics._local_tzinfo` 的 patch 仍打在同一个函数上。那不是合并的前置。
5. 接受。任务槽就是进程内的 `qq_live_digest.webapp._JOB_STATE`。`napcat_install` 也用这个 dict，测试会直接改它。用例只接收快照、启动和探测函数。不搬进 `application`，不进数据库。

依据：`docs/rewrite/04-application.md`、`noticehub/application/calendar.py`、`noticehub/application/tasks.py`、`noticehub/application/jobs.py`、`qq_live_digest/ics.py`、`qq_live_digest/webapp.py`。
