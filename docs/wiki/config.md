# 配置

所有配置都在安装目录的 `.env` 里。**新名字统一带 `NOTICEHUB_` 前缀；旧名 `QQ_DIGEST_*` 仍然能读，新名优先。**

大部分项不用手改：网页「设置」里改完会写回同一个 `.env`。下面这份是给手工配置、或者想知道「这个数到底控制什么」的人看的。模板以安装目录的 `.env.example` 为准。

## 群与来源

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NOTICEHUB_GROUPS` | 空 | 要处理的群号，逗号分隔。**留空 = 一个群都不处理**（不会默认全收） |
| `NOTICEHUB_GROUP_ALIASES` | 空 | 群号到中文名的映射，例如 `123456=学院通知群` |
| `NOTICEHUB_QUIET_GROUPS` | 空 | 低优先级群：不触发紧急推送，普通讨论不进摘要 |
| `NOTICEHUB_IMMEDIATE_GROUPS` | 空 | 即时处理群：不等合并窗口，下一轮直接处理，适合测试群 |
| `NOTICEHUB_PUSH_OPENIDS` | 空 | 官方 QQ 机器人的推送目标 openid |
| `NOTICEHUB_OFFICIAL_BOT_ENABLED` | `0` | 是否启用官方 QQ 机器人。主链路走 NapCat，保持 0 |
| `QQ_BOT_APPID` / `QQ_BOT_SECRET` / `QQ_BOT_SANDBOX` | 空 / `1` | 官方机器人凭据，沙箱填 1 |

## AI 摘要

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NOTICEHUB_LLM` | `1` | 总开关 |
| `NOTICEHUB_LLM_API_KEY` | 空 | 任何 OpenAI 兼容厂商的 key。旧名 `DASHSCOPE_API_KEY` 仍可用 |
| `NOTICEHUB_LLM_ENDPOINT` | DeepSeek 地址 | 兼容端点 |
| `NOTICEHUB_LLM_MODEL` | `deepseek-flash` | 文本模型 |
| `NOTICEHUB_VL_MODEL` | `qwen3-vl-plus` | 读图用的视觉模型。换成非 DashScope 端点时要填该端点自己的视觉模型名，否则只记图片名、不报错 |
| `NOTICEHUB_LLM_BACKEND` | `auto` | `auto` 先试本机 CodeBuddy/WorkBuddy CLI，失败回退兼容端点；也可写 `ollama` / `openai` / `codebuddy` |
| `NOTICEHUB_CODEBUDDY_CLI` | 空 | CodeBuddy CLI 路径（用 auto 时才会找） |
| `NOTICEHUB_LLM_TIMEOUT` | `60` | 单次调用超时秒数 |
| `NOTICEHUB_LLM_MAX_RETRIES` / `_RETRY_BACKOFF` | `2` / `1.5` | 失败原地重试次数与退避倍数 |
| `NOTICEHUB_LLM_DEFER_MAX_ATTEMPTS` / `_DEFER_WINDOW_MINUTES` | `3` / `15` | 仍失败就推迟这批，超过次数才退回本地规则 |
| `NOTICEHUB_INCLUDE_RAW` | `0` | 推送里是否附原文；0 只发精简摘要 |
| `NOTICEHUB_DOCUMENT_MAX_CHARS` | `30000` | 文档类附件读多少字 |
| `NOTICEHUB_PDF_OCR_MAX_PAGES` | `20` | PDF 走 OCR 最多几页 |

## 推送

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `WXPUSHER_APP_TOKEN` / `WXPUSHER_UIDS` / `WXPUSHER_TOPIC_IDS` | 空 | WxPusher（微信接收） |
| `SERVERCHAN_KEYS` | 空 | Server 酱 |
| `PUSHPLUS_TOKENS` | 空 | PushPlus |
| `NOTICEHUB_WEBHOOKS` | 空 | 通用 Webhook，POST JSON `title/content/source`，可以接钉钉、企微、飞书机器人 |
| `NOTICEHUB_PUSH_HTML` | `1` | 推送卡片用 HTML 色块突出待办 |
| `NOTICEHUB_PUSH_DAILY_BUDGET` | `12` | 非紧急推送每天上限，0 = 不限 |
| `NOTICEHUB_QUIET_HOURS` | `23:00-07:00` | 夜间静默，留空 = 关闭。紧急事项始终放行 |
| `NOTICEHUB_DEADLINE_REMINDERS` | `1` | 截止提醒开关 |
| `NOTICEHUB_DEADLINE_MORNING` / `_EVENING` | `07:30` / `21:00` | 早上看今天到期、晚上提前看明天 |
| `NOTICEHUB_CANDIDATE_PUSH` | `1` | 模糊行动项先发确认卡 |
| `NOTICEHUB_CANDIDATE_MIN_CONFIDENCE` | `0.55` | 低于这个置信度就当候选 |
| `NOTICEHUB_CANDIDATE_REMINDER_HOURS` | `24` | 候选多久没处理再提醒 |
| `NOTICEHUB_CANDIDATE_MAX_PER_TICK` / `_PER_DAY` | `2` / `4` | 每轮、每天最多几张确认卡 |
| `NOTICEHUB_WEEKLY_REVIEW` | `1` | 每周复盘 |
| `NOTICEHUB_WEEKLY_REVIEW_WEEKDAY` / `_TIME` | `6` / `20:30` | 0 = 周一，6 = 周日 |

## 调度

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NOTICEHUB_WINDOW_MINUTES` | `30` | 普通通知的合并窗口 |
| `NOTICEHUB_POLL_SECONDS` | `20` | 轮询间隔 |
| `NOTICEHUB_MAX_BATCH` | `400` | 单批最多处理多少条消息 |
| `NOTICEHUB_URGENT_IMMEDIATE` | `1` | 紧急通知是否立刻推 |
| `NOTICEHUB_MIN_SCORE` | `3` | 分数阈值，越高越只推重要通知 |
| `NOTICEHUB_MAX_ITEMS` | `30` | 单条推送最多几项 |
| `NOTICEHUB_MAX_ATTEMPTS` / `RETRY_SECONDS` | `4` / `90` | 投递失败重试 |
| `NOTICEHUB_RETENTION_DAYS` | `30` | 数据保留天数 |
| `NOTICEHUB_DEDUPE_HOURS` | `6` | 多少小时内推过的同一条不再重复推，0 = 关闭 |

## QQ / NapCat 接入

建议让 agent 来装：把这一段发给它，或者让它直接接手这台电脑。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NOTICEHUB_ONEBOT_ENABLED` | `1` | 是否接收 OneBot v11 上报 |
| `NOTICEHUB_ONEBOT_HOST` / `PORT` | `127.0.0.1` / `8765` | 上报监听地址 |
| `NOTICEHUB_ONEBOT_TOKEN` | 空 | 要和 NapCat 的 OneBot 配置一致 |
| `NOTICEHUB_NAPCAT_API_URL` | `http://127.0.0.1:3001` | NapCat HTTP API，补采历史消息用 |
| `NOTICEHUB_NAPCAT_API_TOKEN` | 空 | 留空自动复用 OneBot token |
| `NOTICEHUB_NAPCAT_QR_PATH` | 空 | 二维码文件路径，留空用 `<数据目录>/qrcode.png` |
| `NOTICEHUB_HTTP_TIMEOUT` | `15` | 调 NapCat 的超时秒数 |
| `NOTICEHUB_CATCHUP_ENABLED` | `1` | 断点补采 |
| `NOTICEHUB_CATCHUP_HOURS` / `_COUNT` / `_INTERVAL_MINUTES` | `24` / `50` / `30` | 补多久、每次拉多少、隔多久拉一次 |

### 下载失败与更新来源

`NOTICEHUB_NAPCAT_MIRROR` 默认值为空，不设默认镜像。NapCat 默认从 GitHub 官方发布地址下载，连接与单次读取超时为 30 秒。2026-11-17 本机探测官方源与候选 ghfast.top 均连接超时（curl 退出码 28 / HTTP 000 / 0 字节），尚未确认本机可用的镜像。可在配置中设置 `NOTICEHUB_NAPCAT_MIRROR=https://你的加速服务`：程序把官方 URL 接到此前缀后面。只接受 HTTPS 前缀；没有预设第三方镜像，也不保证第三方服务可用。更改后重启服务。

下载进度显示已下字节和服务器报告的总大小。失败保留已下文件并给出路径，再点「下载安装并启动」可重试；目前未验证下载源返回 206，**不承诺断点续传**，重试使用新的临时目录重新下载。超过一小时的待装临时目录会在后续安装时清理。镜像下载仍须通过原有 ZIP 与安装包指纹检查。

「开始托管」先确认引擎、加载器和配置，再退出电脑版 QQ；任一步失败立即停止，状态接口保留失败原因。没装引擎时会引导到「设置 → QQ 接入」下载。首启向导的「去配置」沿用设置页入口，关闭向导会刷新托管状态，刷新期间暂时抑制 journey；刷新成功或失败都会解除暂停，后续状态刷新仍可显示引导。

更新检查用 `sys.frozen` 区分发布包和源码运行。源码仍可检查发布信息，但不提示安装发布包或同版本修复包；发布包沿用 stable/beta 通道。更新缓存携带构建来源，来源不同会失效，旧版无标记缓存按发布包处理，源码不会复用。

## 数据、附件、网页

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NOTICEHUB_DATA_DIR` / `NOTICEHUB_LOG_DIR` | 空 | 留空用安装目录下的 `data/` 和 `logs/` |
| `NOTICEHUB_ATTACHMENTS` | `1` | 是否读群文件、群图片 |
| `NOTICEHUB_ATTACHMENT_MAX_MB` | `10` | 单个附件上限 |
| `NOTICEHUB_ATTACHMENT_MAX_PER_HOUR` | `30` | 每小时最多处理几个 |
| `NOTICEHUB_ATTACHMENT_RETENTION_DAYS` | `7` | 附件本地保留几天 |
| `NOTICEHUB_VISION` | `1` | 读图开关 |
| `NOTICEHUB_WEB` | `1` | 是否开网页待办台 |
| `NOTICEHUB_WEB_HOST` | `0.0.0.0` | 监听地址 |
| `NOTICEHUB_WEB_PORT` | `8766` | 网页端口 |
| `NOTICEHUB_WEB_TOKEN` | 空 | 访问令牌，留空自动生成 |
| `NOTICEHUB_AGENT_SHELL` | 空（等于打开） | 待办页的智能体一整套（今况那句、3D 地图与行程、对话入口）默认就是开的。只有写成 `0` 才关；空串和不写都算开 |
| `NOTICEHUB_WEB_BASE_URL` | 空 | 固定外部访问地址，例如你自己绑的域名或 Tailscale 的 HTTPS 地址；留空就用当前公网通道给出的地址 |
| `NOTICEHUB_TUNNEL_PROVIDER` | `cloudflare` | 公网入口走哪条通道：`cloudflare` 不用注册、不用自己的域名，地址每次重启会变；`tailscale` 地址固定，但要登录自己的 Tailscale 账号 |
| `NOTICEHUB_AMAP_JS_KEY` / `NOTICEHUB_AMAP_JS_SECURITY_CODE` | 空 | 高德 JS API 的 key 和安全码；不设也能在地图模式条里现填（存 `<data_dir>/map_keys.json`），环境变量优先于文件 |
| `NOTICEHUB_BAIDU_JS_AK` | 空 | 百度 JS API 的 AK；同上，可在地图模式条里现填 |
| `NOTICEHUB_OSM_TILE` | `https://tile.openstreetmap.de/{z}/{x}/{y}.png` | 自绘以外的 OSM 底图瓦片源，必须是个含 `{z}` `{x}` `{y}` 的 https 模板；连不上时可在地图模式条里换 |

两条通道的差别：

- **`cloudflare`（默认）**：临时地址，形如 `https://随机名.trycloudflare.com`。不用注册、不用域名、手机不用装东西，适合「偶尔用流量看一眼」。第一次开通会从 Gitee 镜像下约 19 MB 的压缩包（十秒上下，解开是 55 MB 的 `cloudflared.exe`）；Gitee 不通才退 GitHub 官方包（55 MB，可能要二十多分钟）。两道大小与 sha256 校验都过才算数，不过就换下一个源。下好放在 `data\bin\`，之后直接用。服务重启后地址会变，手机主屏幕上存的旧地址就失效了。
- **`tailscale`**：固定地址，形如 `https://机器名.xxx.ts.net`，适合长期加进主屏幕或 iPhone 日历订阅。需要你在自己的 Tailscale 账号里登录一次并开通 Funnel，这一步程序替不了。

设置页里能直接切换，切完会写回 `.env` 的这一项；切换后点「开通公网」按新通道开通。

## 托管与开机启动

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NOTICEHUB_HOSTING_QUIT_QQ` | `1` | 开始托管时关掉电脑版 QQ |
| `NOTICEHUB_HOSTING_RESTORE_QQ` | `1` | 结束托管时把 QQ 开回来 |
| `NOTICEHUB_HOSTING_AUTO_ON_START` | `0` | 一启动就进托管 |
| `NOTICEHUB_AUTOSTART` | `0` | 开机自启（只写当前用户启动项） |
| `NOTICEHUB_FLOATBALL_ENABLED` | `0` | 桌面浮球开关（设置页「桌面浮球」勾选即写这一项） |
| `NOTICEHUB_PATCH_AUTO` | `1` | 自动接收同版本修复包：后台下好、托盘提示，用户点「重启并更新」才装。设 `0` 只是不自动下载，仍可手动检查 |

> 修复包是同一版本号的重发包，补丁号记在程序里的 `APP_PATCH`，不进 `.env`。设置页版本号后面的「修复 N」就是它。装之前会把上一版备份到 `data\updates\rollback\`，出问题在设置页点「回到上一版」。

## 校园地点的楼层与位置校准

在 `NOTICEHUB_DATA_DIR` 指定的数据目录下编辑 `campus_poi.json`，文件内容为数组。同一 `id` 可以只写要改的字段，例如：

```json
[
  {"id": "library", "floors": 7, "dx": 10, "dy": 20},
  {"id": "gym", "floors": 3}
]
```

这会把图书馆改为 7 层，并向东移 10 米、向北移 20 米。校准方向以校区西南角为原点，x 向东、y 向北，单位是米：`dx` 正数向东，`dy` 正数向北。**既有绝对坐标 `x/y` 和 API 的方向保持 x 向东、y 向南**，因此内部使用 `x + dx`、`y - dy`；不要把北向绝对坐标直接填进旧 `y` 字段。

- 内置点没写的字段保留内置值；`floors` 必须是正整数，字符串、小数、零、负数和布尔值当没写。
- `dx/dy` 缺省为 0，非数字、布尔值和非有限数当没写。每次读取从基准坐标计算，不会越刷新越偏。
- 新增点须带 `id`、`name` 和有效 `x/y`；未填楼层时为未知，接口返回 `floors: null`。
- 保存后重新读取行程即可生效，不需要重启；`/api/agent/plan` 的 `plan.pois` 返回校准坐标和 `floors`，日程、对话规划也使用这些坐标。

内置 27 处地点据校方校园平面图量取，营业时间未知。运动场没有看台与看台坐标，因此没有凭空追加位置；需要时用真实坐标新增点并填写 `floors`。地点编号与来源见[架构](architecture.md)。

## 在设置页改地点：改名、挪位、隐藏

设置页「校园地图」里的地点清单能直接改，不必手写文件：

- **内置 27 处不能删**，只能隐藏——删掉会一起丢掉它们的 OSM 坐标来源；改点「隐藏」就不出现在地图和行程里，卡片底部留一个折叠的「已隐藏」列表，随时点回来。
- 自己加的点（`custom_*`）可以改名、挪位、删除。
- 改动写进数据目录的 `campus_poi_override.json`，只存被改过的字段（`name` / `x` / `y` / `hidden`），出厂表和 `campus_poi.json` 一个字节都不动；删掉这个文件就整体回到出厂。
- 隐藏的地点同时不参与认地点：待办里写「去图书馆」不会再认到被藏起来的图书馆。它只是不出现在图上，文件里还留着。
- 位置可以填坐标，也可以在地图上按住「我在哪」的标记拖到实地位置再松手：40 米内有楼就当在这栋楼里，没有就记成「自由位置」，两种都会把落点存下来。落点同时成为当天行程的起点。

读取顺序是 **seed → 内置 → override → `campus_poi.json`**，后面的盖前面的：换成另一片校区时，把那份校区的基线表放成数据目录的 `campus_poi_seed.json`（格式同 `campus_poi.json`，每条自带 `x`/`y`），有它就不再用内置那 27 处；没有它一切照旧。

## 改完不生效？

- `.env` 在**安装目录**，不是数据目录。改错位置等于没改。
- 网页设置页改的项会立刻写回并生效；手改文件要重启服务（托盘 →「重启服务」）。
- 设置页有张「配置体检」表，写的是「实际生效的值 + 它从哪来（环境变量 / .env / 默认值）」，比翻文件快。
- 想知道某次请求为什么失败，看 [常见问题](faq.md)，或者 `logs\qq-live-digest.log`。
