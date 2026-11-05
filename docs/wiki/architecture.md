# 架构

维护者看的一页。只写事实：东西在哪、怎么连起来、数据放在哪。

## 目录分层

| 位置 | 里面是什么 |
| --- | --- |
| `main.py` | CLI 入口：`run`、`tick`、`preview`、`stats`、`show`、`tasks`、`catchup`、`doctor`、`send-test`、`attach-test` |
| `noticehub/` | 全部业务代码 |
| `noticehub/web_static/` | 网页前端：`page.html`、`js/*.js`，没有构建步骤，改完刷新就是新的 |
| `packaging/` | 出货包：启动器、托盘、安装脚本、各平台打包 |
| `tests/` | 单测与浏览器级用例 |
| `docs/wiki/` | 这一套文档 |

## `noticehub/` 里的分层

| 模块 | 职责 |
| --- | --- |
| `receiver.py` | 接收 NapCat 的 OneBot v11 HTTP 上报，落库 |
| `store.py` | SQLite：消息、待办、去重、完整性检查与恢复 |
| `summarizer.py` | 把一批消息归纳成待办、截止时间、推送文本；模型失败回退本地规则 |
| `service.py` | 调度、合并窗口、补采、重试、生命周期 |
| `push.py` | 推送通道与失败回退 |
| `webapp.py` | 网页与 HTTP 接口：待办台、月历、收件箱、设置、更新 |
| `napcat_admin.py` | NapCat 探测、安装、启停、写配置、登录二维码 |
| `hosting.py` | 托管切换（关 QQ / 起 NapCat / 还原）与状态持久化 |
| `config.py` / `bootstrap/settings.py` | 读 `.env` 与环境变量，新旧前缀兼容 |
| `config_audit.py` | 把「实际生效的值 + 它从哪来」汇成表，设置页的「配置体检」用它 |

网页相关的接口按功能拆过一轮：路由表、设置接口、更新接口各自独立，`webapp.py` 从 4403 行降到 3697 行，行为不变。

## 进程与端口

| 进程 | 作用 |
| --- | --- |
| `QQ-Notice-Hub.exe` | 启动器：单实例互斥、托盘、看门狗（每 2 秒查一次服务，按 5/15/45 秒退避重启） |
| `qq-live-digest.exe` | 真正的服务进程 |
| NapCat | 独立的 QQ 协议客户端，由群务台启动时用**独立资料目录** |

| 端口 | 用途 |
| --- | --- |
| `8766` | 网页、手机端、日历订阅 |
| `8765` | NapCat 往群务台推 OneBot 上报 |
| `3001` | 群务台反查 NapCat 的 HTTP API（补采、状态、二维码） |

防火墙规则只对「专用 / 域网络」和本机网段放行。公用网络下手机连不上是刻意的。

## 数据落在哪

| 路径 | 内容 |
| --- | --- |
| `安装目录\.env` | 配置 |
| `安装目录\data\` | SQLite 数据库、附件缓存、学习通 Cookie、日历链接 |
| `安装目录\logs\` | 运行日志、启动器日志 |
| `data\*.corrupt-<时间戳>` | 启动时完整性检查不通过而被隔离的旧库 |

- 备份就备份 `data\`。
- 数据库每天自动快照，保留 7 份。
- 设置页的「一键备份」导出的 ZIP 里只有 SQLite，不含配置、令牌、日志。

## 一次消息怎么变成待办

1. NapCat 把群消息 POST 到 `8765`。
2. `receiver` 落库；不在 `NOTICEHUB_GROUPS` 白名单的群直接丢。
3. `service` 按合并窗口把消息攒批，交给 `summarizer`。
4. `summarizer` 产出待办（标题、截止时间、置信度），拿不准的先进「候选」。
5. `push` 按通道发出去，失败走回退。
6. `webapp` 的 `/api/tasks` 和 `/calendar.ics` 把结果给网页和手机。

## 更新链路

1. 检查更新：按通道（stable / beta）查 GitHub Releases。
2. 清单里每个包的 `sha256` 必须存在；下载域名只认 GitHub 和 Gitee。
3. 下载：两个源版本与 SHA-256 一致时优先走 Gitee，否则保持 GitHub；有超时和「放弃下载」。
4. 覆装前先看目标文件有没有被占用，被占就整包放弃，不留半装状态。
5. 装完由托盘「重启并更新」触发重启；服务没起来会重试 3 次，仍失败会提示手动启动。

Linux 只能检查和下载，安装要手动。macOS 暂未适配（安装脚本 `packaging/macos/` 保留但未验证）。

## 安全边界

- 访问令牌换 HttpOnly 会话 Cookie，地址栏不留明文 token，也不写 localStorage。
- 连续的失败鉴权会被限速；HTTP 请求 30 秒超时、并发连接上限 64。
- NapCat 安装包解压前校验指纹，首次要人工确认一次实算值。
- 日志与「配置体检」都不回显 token、key、Cookie 原文。

改代码的规矩见仓库根的 `AGENTS.md`；改动要落进 [更新日志](changelog.md)。
