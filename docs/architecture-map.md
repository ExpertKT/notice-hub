# 架构地图（beta.12 基线）

> 最后核对：2026-10-28，对应 HEAD 1eadd35。

盘点方式：只读读代码 + 路由分派语句逐条抽取，未运行、未改任何代码。
基线：`qq_live_digest/webapp.py` **3606 行**（2026-10-28 复核；路由与 settings/updates 域已拆到 `routes.py` / `settings_api.py` / `updates_api.py`），包内 Python 合计 **1,048,161 字节 / 40 个模块**，前端 `page.html` 2611 行 + 4 个拆出脚本 1512 行。

## 0. 一句话结论

**没有做过整轮架构优化。** 已发生的是五处分散治理：`page.html` 拆四块 js（beta.12）、`host_actions.py` 收敛提权、`community.py` 独立、字符串断言清理、`lab.py` 做成统一门面。

结构债集中在两处：

1. `webapp.py` 单文件同时承担 **99 条路由（GET 61 / POST 38）+ 业务逻辑 + 页面模板注入**（`_Handler` 之外还有约 200 个模块级私有函数在处理更新检查、局域网探测、防火墙、首启引导、任务分组、推送/LLM 设置）。
2. **平台接入没有统一接口**：QQ / 微信 / 钉钉 / 学习通 各自一套 `status` / `groups` / `scan` 路由与前端卡片，重复度高，没有 `SourceAdapter` 协议。

另有一个已经**实测确认**的「接口/数据流脱节」实例（见 §1.7）：更新清单的生产路径（`release/update-manifest.json`）与消费路径（仓库根 `update-manifest.json`）对不上，`beta.11` 及更早版本因此从未拉到过 Gitee 清单。双路径兜底已在工作区修好，缺的是发布前的自动校验。

---

## 1. 接口全清单

`do_GET` / `do_POST` 在 `webapp.py` 里只做查表分派：免鉴权表 → `/js/*` 前缀 → `_authorized()` → 鉴权表 → 404。路由数据在 `qq_live_digest/routes.py`（`GET_ROUTES` / `POST_ROUTES` 字面量表 + `GET_PATTERN_ROUTES` / `POST_PATTERN_ROUTES` 前缀表 + `match_pattern()`），改动不再需要动 `if path == ...` 链。

**鉴权三态**

| 标记 | 含义 |
|---|---|
| 免 | 无 token。GET 在 `webapp.py:2907` 的 `_authorized()` 之前分派，POST 在 `webapp.py:3577` 之前；或本身只认配对码 |
| T | 需要 `?token=` 或 `X-Token`（`_authorized`，constant-time 比较，`webapp.py:2144`） |
| T+confirm | 额外要求 body `confirm:true`（`_confirmed_host_action`，`webapp.py:2237`）：会改本机网络/要提权 |

**免鉴权清单（共 14 个分支）**
GET：`/health`、`/api/health`（同一分支）、`/manifest.webmanifest`、`/icon.svg`、`/sw.js`、`/js/*`、`/api/donate/qr.png`、`/api/onboarding`、`/api/onboarding/next`、`/api/onboarding/status`、`/api/pair/status`（只认 code+secret）、`/api/community/counts`
POST：`/api/onboarding/accept`、`/api/pair/start`

### 1.1 待办 / 日程

| 方法 | 路径 | 鉴权 | 用途 | 核心模块 |
|---|---|---|---|---|
| GET | `/api/tasks` | T | 待办列表（含分组、期限标签） | `store.list_tasks` + `group_tasks` |
| GET | `/api/overview` | T | 概览计数（今日/逾期/完成） | `overview()` |
| GET | `/api/insights` | T | 近 N 天（默认 30）统计洞察 | `store` |
| GET | `/api/meta` | T | 元信息（版本、通道、meta provider） | `version` / `settings` |
| GET | `/calendar.ics` | T | iCalendar 订阅（任务截止） | `ics.render_calendar` |
| GET | `/calendar` | T | 302 回首页 | — |
| POST | `/api/tasks/create` | T | 新建待办 | `store` |
| POST | `/api/tasks/pin` | T | 置顶/取消置顶 | `store.meta` |
| POST | `/api/tasks/urgent` | T | 标急/取消急 | `store` |
| POST | `/api/tasks/<id>` | T | 单条动作：`done`/`reopen`/`correct` | `store` |

### 1.2 消息 / 推送 / 收件箱

| 方法 | 路径 | 鉴权 | 用途 | 核心模块 |
|---|---|---|---|---|
| GET | `/api/notices` | T | 最近 8 条摘要记录 | `store.recent_digests` |
| GET | `/api/inbox` | T | 分类后的消息列表（`verdict` 筛选、分页） | `store` + `inbox` |
| GET | `/api/inbox/image` | T | 取某条消息的群图片（`msg_id=onebot:...`） | `attachments` |
| GET | `/api/inbox/classify/status` | T | 「提取通知」任务进度 | `_job_snapshot` |
| GET | `/api/history/fetch/status` | T | 历史回溯任务进度 | `_job_snapshot` |
| GET | `/api/history/floor` | T | 探测某群本地缓存最早时间 | `catchup` |
| POST | `/api/inbox/classify` | T | 启动「提取通知」分类 | `inbox` + `_start_job` |
| POST | `/api/inbox/promote` | T | 把消息转成待办 | `store` |
| POST | `/api/history/fetch` | T | 启动历史回溯 | `catchup` |
| POST | `/api/history/fetch/cancel` | T | 取消回溯 | `_JOB_STATE` |
| POST | `/api/subscriptions` | T | 保存订阅群列表 | `store.meta` |

### 1.3 接入（QQ / 微信 / 钉钉 / 学习通 / 配对 / 公网 / 托管）

| 方法 | 路径 | 鉴权 | 用途 | 核心模块 |
|---|---|---|---|---|
| GET | `/api/setup/status` | T | 首启是否已完成 | `settings` / `store` |
| GET | `/api/onboarding` | T | 今日打开计数 + 首启引导 | `store.meta` |
| GET | `/api/onboarding/next` | 免 | 下一步引导（跳过状态放 URL，不写服务端） | `_onboarding_*` |
| GET | `/api/onboarding/status` | 免 | 引导进度快照 | `_onboarding_status_payload` |
| POST | `/api/onboarding/run` | T | 执行首启动作（含装引擎），写审计 | `napcat_admin` + `host_actions` |
| POST | `/api/onboarding/accept` | 免 | 记录「我知道了」时间 | `store.meta` |
| GET | `/api/napcat/status` | T | 引擎/登录/二维码状态 | `napcat_admin` |
| GET | `/api/napcat/doctor` | T | NapCat 诊断结论 | `doctor` |
| GET | `/api/napcat/groups` | T | 读 QQ 群列表 | `_napcat("get_group_list")` |
| GET | `/api/napcat/qrcode` | T | 登录二维码 PNG | `napcat_admin` |
| POST | `/api/napcat/install` | T | 下载安装 NapCat | `napcat_admin` |
| POST | `/api/napcat/launch` | T | 启动引擎 | `napcat_admin` |
| POST | `/api/napcat/restart` | T | 重启引擎（停旧起新） | `napcat_admin` |
| POST | `/api/napcat/autosetup` | T | 一键接入（装 + 启 + 等登录） | `napcat_admin` |
| GET | `/api/qq/groups/status` | T | 群名异步刷新任务状态 | `qq_group_refresh_owner` |
| GET | `/api/groups/suggest` | T | 群分类建议（LLM，失败退启发式） | `group_suggest` |
| GET | `/api/dingtalk/status` | T | 钉钉接入状态 | `dingtalk_receiver` |
| GET | `/api/dingtalk/groups` | T | 钉钉群列表 | `dingtalk_receiver` |
| POST | `/api/dingtalk/scan` | T | 启动钉钉扫描 | `server.start_scan` |
| GET | `/api/wechat/status` | T | 微信接入状态 | `wechat_receiver` |
| GET | `/api/wechat/groups` | T | 微信群列表 | `wechat_receiver` |
| POST | `/api/wechat/scan` | T | 启动微信扫描 | `server.start_scan` |
| GET | `/api/chaoxing/status` | T | 学习通接入状态 | `chaoxing_receiver` |
| GET | `/api/chaoxing/courses` | T | 课程列表 | `chaoxing_receiver` |
| GET | `/api/chaoxing/cookie/status` | T | Cookie 校验状态 | `_chaoxing_cookie_validation_payload` |
| POST | `/api/chaoxing/cookie/validate` | T | 校验粘贴的 Cookie | `chaoxing_receiver` |
| POST | `/api/chaoxing/auto_cookie` | T | 从本机浏览器读 Cookie | `browser_cookie` |
| GET | `/api/sync/info` | T | 手机同步地址信息 | `weburl` |
| GET | `/api/sync/test` | T | 自测同步 URL 可达（含 webcal） | `weburl` |
| GET | `/api/sync/qr.png` | T | 同步二维码 | `qr` |
| GET | `/api/app/qr.png` | T | App 配对二维码（内含本机令牌，需先登录） | `qr` |
| GET | `/api/pair/info` | T | 配对信息（局域网/公网地址） | `pairing` + `tunnel` |
| GET | `/api/pair/pending` | T | 待批准配对请求 | `pairing.pending` |
| GET | `/api/pair/status` | 免(code+secret) | 手机轮询配对结果；通过后下发 token | `pairing.status` |
| POST | `/api/pair/start` | 免 | 手机发起配对（发 6 位码 + secret） | `pairing.start` |
| POST | `/api/pair/approve` | T | 批准配对 | `pairing.decide` |
| POST | `/api/pair/deny` | T | 拒绝配对 | `pairing.decide` |
| POST | `/api/pair/public` | T | 取公网配对地址（与 tunnel 同分支，无 token 时直接拒） | `tunnel` |
| GET | `/api/tunnel/status` | T | Tailscale Funnel 状态 | `tunnel` |
| POST | `/api/tunnel/start` | T+confirm | 开公网入口（改本机网络） | `tunnel` |
| GET | `/api/firewall/status` | T | 防火墙放行状态 | `host_actions` |
| POST | `/api/firewall/allow` | T+confirm | 加防火墙规则（弹 UAC 提权） | `host_actions` |
| GET | `/api/hosting/status` | T | 托管（让出 QQ）状态 | `hosting` |
| POST | `/api/hosting/start` | T | 开始托管 | `hosting` |
| POST | `/api/hosting/stop` | T | 结束托管 | `hosting` |

### 1.4 设置

| 方法 | 路径 | 鉴权 | 用途 | 核心模块 |
|---|---|---|---|---|
| GET | `/api/settings` | T | 设置页全量状态（推送、LLM、平台、统计） | `_push_setting_state` / `_llm_setting_state` |
| POST | `/api/settings` | T | 保存设置（295 行，全文件最长的单个分支） | `settings` + `store` |
| POST | `/api/settings/test-llm` | T | 试连 LLM | `llm_json` / provider |
| POST | `/api/settings/test-push` | T | 发一条测试推送 | `push` |
| GET | `/api/backup` | T | 下载 SQLite 数据库 | `store` |

### 1.5 实验室（默认全关，`config.py:473-476` 四个 `LAB_` 默认 0）

| 方法 | 路径 | 鉴权 | 用途 | 核心模块 |
|---|---|---|---|---|
| GET | `/api/lab/status` | T | 各实验功能「开关开 + 模块就绪」 | `lab.available` |
| GET | `/api/lab/search` | T | 群消息全文检索 | `lab.search` → `search.py` |
| GET | `/api/lab/replies` | T | 待回清单 | `lab.list_replies` → `replies.py` |
| POST | `/api/lab/replies/ack` | T | 标记某条已回 | `lab.ack_reply` |
| POST | `/api/lab/events` | T | 文本抽事件 | `lab.extract_events` → `event_extract.py` |

> `lab.py` 是唯一门面：路由只依赖它，它用 `importlib` 惰性 import 真实模块，任何异常收成 `{"ok": false}`。`rules` 已定稿并随 beta.13 发布（`lab.list_rules` / `lab.save_rules` → `rules.load_rules` / `rules.save_rules`，规则存 `data/rules.json`）。

### 1.6 系统 / 静态 / 页面

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET | `/health`、`/api/health` | 免 | 存活探针（返回推送通道名） |
| GET | `/api/selfcheck` | T | 服务自检汇总（doctor + napcat status） |
| GET | `/api/update` | T | 查更新（`?check=1` 强制）；走双源：GitHub Releases API 优先，Gitee 镜像清单兜底 |
| POST | `/api/update/download` | T | 下载更新包（多重兜底：主源失败换 `fallbacks` 列表里的备用 URL） |
| POST | `/api/update/apply` | T | 覆盖安装并重启 |
| GET | `/api/announcements` | T | 公告（缓存 + 后台刷新） |
| GET | `/api/community/counts` | 免 | 「当前在线 / 今日打开」计数 |
| GET | `/`、`/index.html` | T | 主页面（注入通道徽章/版本/logo 变量） |
| GET | `/floatball`、`/floatball.html` | T | 浮球极简页（同源，只读任务与收件箱） |
| GET | `/setup` | T | 302 回首页 |
| GET | `/manifest.webmanifest`、`/icon.svg`、`/sw.js` | 免 | PWA 静态资源 |
| GET | `/js/*` | 免 | 前端拆出的脚本（只允许 `web_static/js/*.js`，防目录穿越） |
| GET | `/api/donate/qr.png` | 免 | 赞赏码图片 |

**合计 99 条路由（含 1 条 `/js/*` 前缀与 1 条 `/api/tasks/<id>` 前缀）：GET 61 / POST 38，对应 92 个处理方法。**（2026-10-28 以 `routes.py` 实际表项数出）

### 1.7 更新链路：一条数据流的两端对不上（已实测）

分工：**GitHub 走 Releases API**（`updater.py:238-245`，不读清单）；**只有 Gitee 镜像读清单**（`updater.py:246` → `update_sources.manifest_url()`）。

```python
# update_sources.py:56-57
def manifest_url(source: dict[str, Any]) -> str:
    return str(source["raw_base"]).rstrip("/") + "/update-manifest.json"   # → 仓库根
```

但清单文件实际发布在 `release/` 下（本仓库 `release/update-manifest.json`）。两端不一致，实测：

| 地址 | 期望 | 实测 |
|---|---|---|
| `https://raw.githubusercontent.com/ExpertKT/notice-hub/main/update-manifest.json` | 清单 | **404** |
| `https://gitee.com/ExpertKT/notice-hub-mirror/raw/master/update-manifest.json` | 清单 | **404** |
| `https://raw.giteeusercontent.com/ExpertKT/notice-hub-mirror/raw/master/release/update-manifest.json` | — | **200**，内容为 beta.12 清单 |

结论：**Gitee 兜底从未生效**。GitHub 可达时用户无感（主线走 Releases API）；GitHub 不可达时（国内网络最常见的情况）镜像路径直接抛错，兜底等于不存在。清单文件本身是对的，错的是消费端拼的路径。

**修复已在工作区落地**（`update_sources.py:52-69` + `updater.py:237-257`）：清单路径提成常量，**`release/` 优先、根目录作兼容兜底**，404 才退下一个，两个地址共用同一份超时预算（不会把 30 秒放大成 60 秒）。

```python
# update_sources.py:54
MANIFEST_PATHS = ("release/update-manifest.json", "update-manifest.json")
```

`beta.11` 及更早的版本从来没拉到过 Gitee 清单，只是 GitHub 能兜住才没暴露 —— 这条也写进了源码注释。

对比 `announcements_url()`（`update_sources.py:57-58`）拼的是仓库根，而 `announcements.json` 确实在仓库根 —— 它是**对的**。问题恰恰出在这里：两个同源函数，都指向根，但 `announcements.json` 在根、`update-manifest.json` 在 `release/`。**剩下的缺口**：发布流程还没有一步去校验「文件放哪」和「URL 怎么拼」是否一致，下次挪文件仍会静默复发。

---

## 2. 模块地图

依赖方向（→ 表示「依赖」）：

```
main.py ──> service.py ──> { receiver, catchup, bot,
                             summarizer, push, store,
                             webapp.TaskWebServer }   ← 调度器反向持有 HTTP 层
webapp.py ──> { store, config, messages, timeutil, push, attachments,
                ics, weburl, win_text,
                chaoxing_receiver, dingtalk_receiver, wechat_receiver,
                floatball_page, announce, community, qr, lab,
                pairing, selfcheck, tunnel, host_actions,
                [可选] catchup, inbox, group_suggest, hosting,
                       napcat_admin, browser_cookie, updater }
launcher.py ──> { floatball_host, floatball_window }
```

### 2.1 采集 / 接入

| 模块 | 职责 | 被谁依赖 |
|---|---|---|
| `receiver.py` | OneBot v11 事件接收（NapCat 等兜底方案） | `service`, `catchup` |
| `catchup.py` | NapCat/OneBot 历史消息补采 | `service`（可选） |
| `bot.py` | 官方 QQ 机器人常驻客户端（qq-botpy + GROUP_MESSAGE_CREATE 补丁） | `service` |
| `attachments.py` | 群文件/群图下载、解析、AI 摘要，再当普通消息进摘要流水线（46 KB） | `receiver`, `catchup`, `service`, `webapp` |
| `wechat_receiver.py` | 微信本地库只读读取器（WeChat 4.1.x / SQLCipher v4，58 KB） | `service`, `webapp` |
| `dingtalk_receiver.py` | 钉钉 V3 本地库只读读取器 | `wechat_receiver`, `service`, `webapp` |
| `chaoxing_receiver.py` | 学习通只读客户端与响应探测 | `service`, `webapp`, `messages` |
| `browser_cookie.py` | 从本机浏览器（Firefox/Edge/Chrome）提取学习通 Cookie | `webapp`（可选） |
| `napcat_admin.py` | NapCat 发现与一键 OneBot 接线（50 KB） | `webapp`（可选） |

### 2.2 处理

| 模块 | 职责 | 被谁依赖 |
|---|---|---|
| `service.py` | 滚动窗口调度：紧成立即推、普通合并推、失败重投（56 KB） | `main.py` |
| `summarizer.py` | 消息记录转摘要条目 + 生成推送纯文本 | `service`, `push`, `inbox` |
| `push.py` | 推送通道：QQ 机器人私聊优先、HTTP 兜底，全部写 `deliveries` 去重 | `service`, `webapp` |
| `inbox.py` | 消息三分类（通知/疑似/噪声）与转待办 | `webapp`（可选） |
| `group_suggest.py` | 用 LLM 建议订阅哪些群，失败退启发式 | `inbox`, `webapp`（可选） |
| `event_extract.py` | 事件结构化提取（实验室功能，默认关闭） | 仅 `lab` 动态加载 |
| `rules.py` | 规则引擎纯函数内核（实验室功能，设置页可视化编辑） | 仅 `lab` 动态加载 |
| `replies.py` | 待回清单（实验室功能，默认关闭） | 仅 `lab` 动态加载 |
| `search.py` | 群消息全文检索（实验室功能，默认关闭） | 仅 `lab` 动态加载 |
| `announce.py` | 公告通道 fetch/parse/active，绝不抛异常 | `updater`, `webapp` |
| `community.py` | 在线数/今日打开计数 | `webapp` |
| `doctor.py` | 一键诊断包（`python main.py doctor --bundle`） | `main.py`, `webapp` |
| `selfcheck.py` | 自检面板的纯函数判定 | `webapp` |
| `llm_json.py` | LLM 返回 JSON 的容错解析（0.9 KB） | `inbox`, `group_suggest` |
| `retry.py` | 外部调用重试：只重试可恢复错误 | `attachments`, `summarizer` |
| `ttl_cache.py` | 进程内 TTL 缓存 | `host_actions`, `wechat_receiver` |

### 2.3 存储 / 基础

| 模块 | 职责 | 被谁依赖 |
|---|---|---|
| `store.py` | SQLite 持久化：消息去重、摘要归档、投递去重与重启恢复（61 KB） | 几乎全部 |
| `config.py` | 环境变量 / `.env` 读配置，密钥只存本机不写日志（31 KB） | 几乎全部 |
| `messages.py` | 用户可见文案的单一来源 | `attachments`, `webapp`, `napcat_admin`, `tunnel`, `host_actions` |
| `timeutil.py` | 统一「本地时间 naive datetime」 | 广泛 |
| `logging_setup.py` | 控制台 + 按大小轮转文件 | `main.py` |
| `version.py` / `update_sources.py` | 版本常量 / 更新源常量与 URL | `doctor`, `announce`, `updater` |
| `weburl.py` | 局域网与 Tailscale 访问 URL 拼装 | `service`, `webapp` |
| `qr.py` | 无依赖 QR 字节编码器 | `webapp` |
| `ics.py` | RFC 5545 iCalendar 渲染 | `webapp` |
| `win_text.py` | 解 Windows 控制台工具输出编码（netstat/tasklist…） | `host_actions`, `hosting`, `napcat_admin`, `tunnel`, `webapp` |

### 2.4 宿主 / 外壳

| 模块 | 职责 | 被谁依赖 |
|---|---|---|
| `webapp.py` | 手机端待办台：HTTP 服务 + 路由分派 + 页面注入（3606 行；路由数据在 `routes.py`） | `service` |
| `hosting.py` | 「托管」：让 NapCat 独占 QQ 号时临时退出/恢复本机 QQ | `webapp`（可选） |
| `host_actions.py` | 宿主机操作的唯一入口：提权执行、防火墙、审计 | `webapp` |
| `tunnel.py` | Tailscale Funnel 一键公网入口状态机 | `webapp` |
| `pairing.py` | 手机 App 配对：6 位码 + secret + 电脑端批准 | `webapp` |
| `updater.py` | 查 GitHub release、下载校验、写脚本覆盖安装（40 KB） | `webapp`（可选） |
| `floatball_page.py` | 浮球用的同源极简 HTML | `webapp` |
| `floatball_host.py` | Edge App 宿主 + Win32 窗口样式 | `floatball_window`, `launcher` |
| `floatball_window.py` | tkinter 原生浮球（色键透明） | `launcher` |

### 2.5 职责不清 / 重复（按严重度）

1. **`webapp.py` 是「路由 + 服务 + 模板」三合一**。`_Handler` 只占一小半，其余约 200 个模块级函数在做本属 service 层的活（更新检查、局域网探测、防火墙状态机、首启状态机、推送/LLM 设置解析）。`POST /api/settings` 单个分支 295 行。
2. **`service.py → webapp.TaskWebServer` 的反向依赖**：调度器为了起 HTTP 线程而依赖 HTTP 模块（`service.py:26`），页面层与调度层没有分界。
3. **四个平台各写一套 `status`/`groups`/`scan`**：`webapp.py:3310-3405` 是钉钉/微信/学习通三段近乎同构的 handler，前端也是三张独立卡片（`settings.js:297/316/332`）。加第五个平台要动 4 处（route、payload、js 卡片、文案常量）。
4. **浮球三份实现并存**：`floatball_page`（网页）、`floatball_host`（Edge 宿主）、`floatball_window`（tkinter）。`launcher.py:40` 写着 `FLOATBALL_ENABLED = False  # 浮球暂停`，但 `/floatball` 路由仍在生产里对外提供页面 —— 未发布功能留在了发布路径上。
5. **`wechat_receiver → dingtalk_receiver`**：语义上是「复用本地库读取实现」，但耦合写成平台依赖；两个接收器各自 32 KB / 58 KB，共享部分没有独立模块。
6. **更新清单的「生产端」与「消费端」路径脱节（已实测 404，见 §1.7）**：`update_sources.manifest_url()` 一直拼仓库根，发布实际写到 `release/`，`beta.11` 及更早因此从未拉到过 Gitee 清单。双路径兜底已在工作区落地（`MANIFEST_PATHS`）；缺的是发布流程里的自动校验——否则下次挪文件同样静默复发。这是典型的「接口边界没人守」。

---

## 3. 前端结构与用户动线

### 3.1 结构

`page.html` 2611 行是骨架 + 内联样式，四个脚本按 `<script src>` 顺序加载（`page.html:774-777`，**顺序有依赖**：announce → pair → settings → guide）：

| 文件 | 行数 | 内容 |
|---|---|---|
| `js/announce.js` | 287 | 公告铃铛、面板、未读态、首次弹窗 |
| `js/pair.js` | 360 | 手机配对卡、公网隧道面板、防火墙面板、轮询 |
| `js/settings.js` | 651 | 更新卡、学习通/钉钉/微信/实验室四张平台卡、设置页全量渲染 |
| `js/guide.js` | 214 | 首启引导（`#journey`）、NapCat 安装/二维码/群列表、本地状态机 |

页面骨架（自上而下）：

```
#platform-setup-wizard   首启平台选择弹窗（hidden，modal）
masthead                 页头（通道徽章注入点）
.summary / #upcoming-carousel   今日概览 + 最近到期轮播
.tabs                    待办 / 最近推送 / 收件箱 / 设置
#journey                 「还差几步」引导条（hidden）
.workspace
  .primary-view          tab-tasks / tab-notices / tab-inbox / tab-settings
  #calendar-panel        月历（在主列，不在 tab 里）
.side-rail               右栏 5 张卡
  #pinned-panel          02 置顶日程
  #connect               01 QQ 接入（内含 #groupbox 订阅群）
  #rail-summary          03
  #rail-about            04
  #rail-actions
  #rail-health           05 服务自检
```

### 3.2 动线现状

**首启（应有）：** wizard 选平台 → `#journey` 说「还差几步」→ `#connect` 装/启引擎 → 二维码 → 登录成功 → `#groupbox` 选群 → 待办出现。
**实际断点：** 登录成功后 `#groupbox` 才 `hidden=false`，而引导条 `#journey` 不覆盖「去选群」这一步；用户登录完看到的是右栏多出一块内容，`#journey-next` 已经在说「完成」。选群没有明确的下一动作。

**日常：** 打开就看 tab-tasks（默认态）→ 逾期/今日靠 `.summary` → 需要翻旧消息切 tab（收件箱里还要再点「② 提取通知」才看到分类结果）。
**断点：** 「待办/日历/置顶日程/收件箱」四块都是任务相关，分散在 主列 tab + 主列底部 + 右栏顶部 + 第三个 tab。

### 3.3 视觉引导改进建议（按性价比排序）

1. **接入状态收口到一张卡**（成本中 / 收益最大；建议实施版本：beta.13）。现在同一个「接好了没」散在 6 处：`#status`（登录）、`#napcat-engine`（文件在不在）、`#qrbox`（二维码）、`#journey-steps`（还差几步）、`#rail-health-list`（自检行）、wizard 的平台勾选。右栏 `#connect` 应变成「一台设备一行」的总状态卡：QQ / 微信 / 钉钉 / 学习通 各一行 + 一个状态点 + 一个「去处理」；`#rail-health` 退回成诊断细节（折叠）。
2. **按钮从 7 个收到 1 主 1 次**（成本小 / 收益大；建议实施版本：beta.13）。`#connect` 现在同屏有：自动下载并安装、保存并重载、一键接入、启动并登录、刷新二维码、重启引擎、启动 NapCat 并获取二维码。建议按状态只露一个主按钮（未装→安装；装了没启→启动并登录；等扫码→刷新二维码；在线→无），其余进 `<details>`。现在「一键接入」和「启动并登录」对用户不可区分，`title` 里才解释。
3. **文案去重**（成本极小 / 收益立竿见影；建议实施版本：beta.13）。`page.html:702` 的 `login-note` 与 `703` 的「账号与设备说明」几乎逐句重复（独立资料目录 / 不改动日常 QQ / 同一 QQ 号不能两处登录 / 建议小号）。留一处。
4. **移动端把「接入/自检」移出首屏**（成本中 / 收益大）。390px 下 `.side-rail` 折到主列之后，用户要先滚过待办+日历才看到接入卡；而接入是首启必做的事。建议窄屏下 `#connect` 提到 tab 之后、日历之前，`#rail-health` 移进设置页。
5. **平台条（pill）直达**（成本中 / 收益中）。设置页四张平台卡埋在 tab-settings 深处，用户不知道微信/钉钉/学习通在哪配。顶部加一条平台状态条（灰=未配 / 绿=已连），点击直达对应卡。
6. **「选群」补成显式下一步**（成本小 / 收益中）。登录成功后把 `#journey` 的 next 改成「去选要订阅的群」，并把 `#groupbox` 的高亮加上，替代现在的静默展开。
7. **二维码默认折叠**（成本小 / 收益中）。`#qq-qr-details` 是 `open`，未登录时二维码块占掉首屏大半；改成状态行「未登录 · 展开扫码」。
8. **三套「下一步」合一**（成本中 / 收益中）。`#journey-next`、`#rail-health-message`、wizard 的 intro 都在说「还差什么」，措辞不一致时会互相矛盾。统一成一个 `nextAction` 数据源，三处只做展示。
9. **右栏 01–05 编号去掉或语义化**（成本极小 / 收益中）。编号暗示「01→02→03」顺序，但 02 是置顶日程、05 是自检，实际顺序是 01 接入→订阅群→自检。建议改三步行进器「接入 → 订阅 → 自检」。
10. **通知入口收口**（成本中 / 收益低）。更新横幅、公告铃铛、浮球（暂停中）三处都在抢「有条新东西」的注意力，长期应收成一个入口。

---

## 4. 架构优化建议（3 条结构性的 + 1 条立即要修的）

### 4.1 更新链路路径脱节：主体已修，缺一道发布前校验

不是风格问题，是曾经一直在坏的功能（§1.7 实测 404）。**双路径兜底已落地**：`MANIFEST_PATHS` 常量 + `_fetch_manifest` 按序退、共用超时预算。

**还剩一步**：把「文件放哪」和「URL 怎么拼」钉死。建议发布流程加一条只读校验——对 `release/update-manifest.json` 和根 `announcements.json` 各 GET 一次，非 200 就中止发布。该发布前校验**并入 release skill / `tools/gate.ps1` 作为固定步骤**，而不是靠人记得跑。不加这道校验，下次谁再挪文件，同样的静默 404 会再来一遍（这次能兜住是因为常量写了两条路，不是因为流程有防线）。

### 4.2 切 `webapp.py`，按「路由 / 载荷 / 模板」三分（收益最高）

现状：3606 行、99 条路由（路由数据在 `routes.py`）、单个 POST 分支仍有数十行。目标不是重写框架，而是最小切口：

- 新建 `webapp_payload.py`：搬走 `_update_*`、`_push_setting_*`、`_llm_setting_*`、`_firewall_*`、`_onboarding_*`、`group_tasks`/`overview` 这些**纯数据装配**函数（`webapp.py:125-2060` 里的大部分）。
- 路由层只留「解析参数 → 校验 → 调 payload → 回 JSON」。
- 页面模板常量单独一个文件（现在 `PAGE_HTML` 在 `webapp.py:1501`，还把三处 `replace` 注入混在路由里）。

判据：`webapp.py` 降到 2000 行以内，且任何 `_update_*` 相关 bug 的定位不再需要跨 4000 行读。

### 4.3 给四个平台抽 `SourceAdapter` 协议（这才是「把接口整理顺」的正题）

现状：钉钉/微信/学习通三套 handler 结构相同（`webapp.py:3310-3405`），QQ 走 `napcat_admin` 自成一套。加平台要改 4 处。

建议协议只要三个方法：`status() -> dict`、`list_groups() -> list`、`scan() -> job`，加一个 `key` 字段。路由改成 `GET /api/sources/<key>/status` / `POST /api/sources/<key>/scan`（旧路径保留一段时间做别名）。前端对应只渲染一张「平台行」组件，现在四张卡手写的部分能删掉大半。

### 4.4 把 `service → webapp` 的依赖反过来（中等成本，但决定后面能不能拆 4.2）

`service.py:26` 为了起 HTTP 线程直接 `from .webapp import TaskWebServer`。建议 `TaskWebServer` 只负责 socket 生命周期，业务回调由 `main.py` 注入（现在 `webapp` 靠 `self.server.settings` / `server.start_scan` 等属性反向取服务实例，全是 `# type: ignore[attr-defined]`）。做完这一步，4.2 的拆分才能不牵动调度器。

---

## 附：盘点时顺带发现（不在本单范围，仅记录）

- **免鉴权信息面**：`/api/onboarding/status` 与 `/api/community/counts` 在局域网内无需 token 即可读（首启向导需要，属有意设计；但 `onboarding/status` 会暴露「这台机器装到哪一步」，可考虑只在本机回环放行）。
- **未发布功能留在发布路径**：`launcher.py:40` 浮球已暂停，`/floatball` 路由仍对外服务。
- **实验室默认全关但 README 与 `config.py:473-476` 一致**（四个 `LAB_` 默认 0），文档无误差，差异在「是否该默认开」这一产品口径上。
