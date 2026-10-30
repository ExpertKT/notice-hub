# risks.md — 重写会打断什么，按依赖顺序

口径：以 HEAD `ad67e3e` + 工作区 19 条未提交改动为基准。
「必须保持兼容」= 对外可见契约（HTTP 接口、`.env` 键名、数据文件格式、更新清单格式），
改了会让用户/现有安装/已发布版本出问题。

---

## 0. 先说三条结构性事实（决定风险排序）

1. **`service.py:27` 顶层 `from .webapp import TaskWebServer`，webapp 又反向从
   `self.server.settings` / `start_scan` / `napcat_stage_state` 取服务实例。**
   这是模块级双向依赖。`docs/architecture-map.md:366` 把它列为「决定能不能拆 4.2」的前置。
   **只重写 webapp 内部 = 把同样的耦合复制一遍。**
2. **`tests/test_webapp.py` 6187 行，`PAGE_SOURCE`/`PAGE_HTML` 类断言 255 条**（全 tests
   617 处引用），锁的是页面模板源码片段。动模板就成片红，而它们测不出行为。
3. **工作区三个新模块（`routes.py`/`settings_api.py`/`updates_api.py`）还没 `git add`。**
   从 HEAD 切 clean-room 分支**拿不到它们**。切分支前必须先提交或先 `git add`。

---

## 1. 依赖顺序（重写时按这个顺序动，反向动会双倍返工）

```
① store.py（数据模型 + 迁移）      ← 一切的地基
② 领域纯函数（event_extract / ics / timeutil / search）
③ service.py 的编排（去掉 webapp 反向依赖）
④ ports 定义
⑤ application 用例（按 usecases.md 的 18 条核心）
⑥ 适配器（receiver / dingtalk / wechat / chaoxing / push）
⑦ webapp / routes（HTTP 层最后）
⑧ 前端（page.html + js）
⑨ 打包与更新（build.ps1 / updater / launcher）
```

**为什么 store 必须最先**：`config.py` 只被 15 个模块依赖，但牵动 19 个测试文件；
`timeutil.py` 被 13 个模块依赖。这两个「小文件」是改动的最大刹车。
`store.py` 更甚——它是 11 张表的唯一入口。

**为什么 webapp 必须靠后**：`service.py:27` 的双向依赖不先解掉，重写 webapp 就得同时
维护两套耦合。

---

## 2. 按域列风险

### A. 数据模型与迁移（**必须保持兼容**）

| 会打断 | 具体 |
|---|---|
| 已发布版本的数据库 | `store.py:184-202` 只有「加列」迁移（`PRAGMA table_info` 差集 + `ALTER TABLE ADD COLUMN`）。**没有 schema 版本号**（无 `user_version`、无 `schema_version` 表）。重写若改列类型/删列/重命名，老库升不上来 |
| `GET /api/backup` | `webapp.py:2259-2295` 直接 `self.store.path` + `source.backup()` + 打成 zip。重写 store 接口时这个 handler 会摸空 |
| `data/` 下的旁路文件 | `messages.jsonl`/`digests.jsonl`（`store.py:167-168,220-225`）、`chaoxing.cookie`、`qrcode.png`、`firewall.request`、`updates/state.json`、`updates/pending.json`、`hosting.json`。这些**不在数据库里**，重写数据层时最容易漏 |
| `data/tasks.sqlite3` | 遗留产物，代码零引用。清理它零风险，但别顺手「修」 |

### B. HTTP 契约（**必须保持兼容**，见 api-contract.md）

| 会打断 | 具体 |
|---|---|
| 前端全部调用 | 94 条登记项。**`/api/settings` GET+POST 同名不同方法**、**`/js/*` 与 `/api/tasks/<id>` 两条前缀路由**、**`/api/hosting/stop` 与 `/start` 共用一个 handler 名靠 URL 后缀分支**——这三处纯遍历 dict 会漏 |
| 手机 App / 安卓端 | 同一批接口的消费者。改字段名 = 老 App 白屏 |
| 测试契约锁 | `tests/test_routes.py` 用路由表做等价断言。重写后这张表本身会变，**要么同步改测试，要么先冻结契约** |
| 200 + `ok:false` 的坑 | `/api/hosting/start|stop`（`webapp.py:3415-3416`）、设置保存（`settings_api.py` 的兜底）都在错误时返回 **200**。客户端必须看 body 的 `ok`。重写若「修正」成 4xx/5xx，**会打破现有前端的判断逻辑** |

### C. 配置（**必须保持兼容**）

- `.env` 键名（`QQ_DIGEST_*`）是用户已填好的事实。`save_settings` 会就地改写 `.env`
  （`webapp.py:3443-3444` 用共用的 `update_env_file`）。
- `version.py:12-13` 的 `APP_VERSION`/`APP_CHANNEL` 是**唯一**版本来源，
  且 `updater.check()` 拿它和 GitHub tag 比。重写时不许在别处硬编码版本字符串。
- 新装 `.env` 由 `packaging/launcher.py` 的 `ENV_TEMPLATE` 生成，
  历史教训：**全新安装的 `.env` 没有 `QQ_DIGEST_GROUPS`/`QQ_DIGEST_GROUP_ALIASES` 两行**，
  于是「第一次保存订阅」对任何测试者都必定失败（`c81686e` 修的）。

### D. 前端（**必须保持兼容，但测试断言可重写**）

| 会打断 | 具体 |
|---|---|
| 页面模板断言 | `PAGE_SOURCE`/`PAGE_HTML` 类 255 条。backlog 自称「约 325 条，两周量」。**排期必须先定：先清断言还是旧断言整批删** |
| 静态资源收集 | `3d03d06` 的教训：`web_static/js` 漏进 PyInstaller datas，装出来脚本 404 而服务正常。重写若改前端目录结构，`qq_live_digest.spec` 的 datas 必须同步 |
| `#platform-setup-wizard` | 纯 localStorage 控制（`page.html:2658`），后端不参与。重写时别把它当成需要后端状态的组件 |

### E. 打包与自动更新（**必须保持兼容**）

| 会打断 | 具体 |
|---|---|
| 已安装实例的更新 | `updater.py` 的 `pending.json` 格式、`apply_async` 生成的 PowerShell 脚本、`/health` 探活。改任何一环，**老版本就再也升不上来** |
| 更新清单格式 | `release/update-manifest.json` 的字段（`tag`/`version`/`name`/`notes`/`published_at`/`asset_name`/`sha256`/`download_url`，`updater.py:191` 校验）。改了老版本读不出 |
| zip 结构 | 顶层即安装根、条目用 `/`、无只读位。`install-task.ps1` 注册 `<install root>\QQ-Notice-Hub.exe` |
| 版本比较 | `updater.py` 的版本元组比较 + 降级保护。重写版本号解析要先对齐 |
| Gitee 镜像 | `MANIFEST_PATHS = ("release/update-manifest.json", "update-manifest.json")`，双路径兜底。beta.11 及更早**从来没拉到过 Gitee 清单** |

### F. 宿主动作（平台绑定，重写成本最高）

| 会打断 | 具体 |
|---|---|
| NapCat 托管 | `napcat_admin.py` 的下载/解压/写 OneBot 配置/起进程/杀 QQ。**历史教训 `d73b46e`：启动前不写 OneBot 配置 → 扫码成功但链路断** |
| 防火墙 | `host_actions.py` 的 netsh + UAC + `data/firewall.request` 原子写。**历史教训 `597ffd8`：netsh 打桩要打在真正跑子进程的那个模块** |
| Tailscale Funnel | `tunnel.py` 的子进程 + `serve_config` 5 秒缓存。重写若丢缓存，自检从秒级退回十几秒 |
| 微信内存读取 | `wechat_receiver.py` 扫进程内存取密钥，首次数分钟 |
| 托盘 / 浮球 | `launcher.py` + `floatball_*.py`（`FLOATBALL_ENABLED=False`）。CI 的 tray-smoke 只跑 `-NoUi`，因为 GitHub runner 无可读任务栏 |

### G. 附件原图（老记录取不到，明确不做）

- **现象**：`onebot-attachment` 记录的老图取不到——旧实现写盘按 `image_key(消息行 id)`
  落 `files/<sha1>_<n>.<ext>`，读侧按 `attach:<key>_*` 找，两边不同源。
- **主因 1（缓存已过期）**：`QQ_DIGEST_ATTACHMENT_RETENTION_DAYS` 默认 7 天
  （`config.py:284`），`service.py:1341` 的 `_maybe_prune` 每 6 小时按 mtime 清一次
  `data/files`；现场那几条是十几天前的记录，对应文件大概率早已被清掉。
- **主因 2（记录缺消息行 id）**：记录里只有 `attach:<key>`，没有 `onebot:<群>:<消息id>`，
  回填得先反查 `messages` 才能算出前缀，投入高且可能一条都捞不到。
- **已修（只对新写入生效）**：`attachments.py:1202` 的 `_keep_image` 用
  `prefix=attachment.key` 落盘，读写同源；
  `tests/test_webapp.py::test_attachment_event_image_round_trip` 端到端覆盖
  （真过 `AttachmentWorker.process()` 再取图）。

---

## 3. 可以放手改的（低风险）

- `routes.py` 的**组织形式**（保持对外路径不变，内部怎么查表随意）。
- `settings_api.py` 的九个 `_save_*` 方法拆分方式（保持 `.env` 写入结果不变）。
- 前端**样式与动效**（不动 DOM id/class 契约和接口字段）。
- `docs/`、`README.md`、`backlog`。
- `data/tasks.sqlite3` 遗留文件（清理）。

---

## 4. 重写期间的两阶段合并（沿用既定策略）

1. **并存阶段**：新模块与旧模块同时存在，旧模块转适配层，**跑通全量测试**。
2. **切换阶段**：切调用点、删旧实现。**包名与模块重命名放最后一个提交，一次性完成**。

理由：`webapp.py` 的测试断言与 `service.py` 的双向依赖决定了「渐进替换」比「大爆炸」便宜；
但模块重命名若分散在多个提交里，`git bisect` 和回滚都会变难。

---

## 必须由人补的项

1. **`docs/architecture-map.md:366` 记的「4.4 依赖反转」到底做了什么、做到哪一步**——
   这决定 clean-room 能不能直接接上，只能人工确认。
2. **老用户实际装在哪些版本**（决定迁移代码要覆盖多远）。本地只有 `F:\notice-hub`
   一份 beta.12，线上分布未知。
3. **手机 App 的最低支持版本**（决定接口能改到什么程度）。
4. **`PAGE_SOURCE` 断言是「先清」还是「整批删」的产品/排期决策**。
