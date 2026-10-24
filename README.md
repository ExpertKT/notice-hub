<div align="center">

# 群务台 · notice-hub

**把 QQ 群里的通知，自动变成待办和日历。**

从指定 QQ 群读取消息，过滤闲聊，提取通知、行动项和截止时间，集中到网页待办台，也可订阅手机日历。

[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm%20Noncommercial-12695b.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-12695b.svg)](https://www.python.org/)
![Platform](https://img.shields.io/badge/Platform-Windows%20%2F%20Linux%20%2F%20macOS-12695b.svg)
[![Tests](https://github.com/ExpertKT/notice-hub/actions/workflows/tests.yml/badge.svg)](https://github.com/ExpertKT/notice-hub/actions/workflows/tests.yml)
[![Release](https://img.shields.io/github/v/release/ExpertKT/notice-hub?color=12695b&label=release&include_prereleases)](https://github.com/ExpertKT/notice-hub/releases)

[下载正式版 v1.0.0](https://github.com/ExpertKT/notice-hub/releases/tag/v1.0.0) · [下载测试版 v1.1.0-beta.10](https://github.com/ExpertKT/notice-hub/releases/tag/v1.1.0-beta.10) · [使用说明](docs/使用说明.md) · [API 接入](docs/接API.md) · [安卓 App](docs/安卓App.md) · [NapCat 安装](docs/napcat-setup.md)

**🧪 v1.1.0 测试版（beta.10）已发布 — [查看说明并下载 →](https://github.com/ExpertKT/notice-hub/releases/tag/v1.1.0-beta.10)**

beta.10 做四件事：**托盘菜单不再锁死**（托管操作卡住导致「重启并更新」永久变灰，现在 90 秒没返回就自动恢复）；**公告栏**（顶部「本地运行」左边多了铃铛和未读数字，可「全部标为已读」，Esc 关闭，手机窄屏占满宽度）；**公告到得更快**（检查间隔 6 小时 → 15 分钟，点「检查更新」或打开公告时顺手刷新）；**手机连不上点一下就修**（防火墙没放行直接弹管理员权限自动加规则，24 小时内最多弹一次；公网访问点「开通公网入口」后自动装好并拉起 Tailscale、登录完自动开 Funnel 并显示公网地址——唯一要自己做的是登录 Tailscale 账号，公网通道必须绑账号）。beta.9 重点解决「第一次用找不到二维码」：向导关闭后自动定位到二维码，NapCat 未启动时给「启动并获取二维码」按钮，没有任何数据时顶部出现「首次使用？」引导条。同时新增四个**默认关闭**的实验功能（群里搜历史消息、待回清单、时间地点提取、规则引擎，在 `.env` 里设 `QQ_DIGEST_LAB_SEARCH / REPLIES / EVENTS / RULES = 1` 后重启，设置页出现「实验室」），以及一键诊断包 `python main.py doctor --bundle`（脱敏，出问题直接发给我们）。还修了一批影响日常使用的问题：学习通 Cookie 不再被误判过期（泛雅子站也能正确识别）；同一条群消息不再重复生成任务；摘要超长不再静默丢弃，会提示「另有 K 条未展开」；任务统计与日历导出不再被 800 条截断；装完更新服务没起来会自动重试并明确提示；配对页首开更快。beta.8 修手机连不上：自动检测 Windows 防火墙是否放行 8766 并给一键放行命令；局域网地址不再误选 Tailscale / VMware / WSL 等虚拟网卡；公网开通失败会明确说卡在哪一步。beta.7 更新包下载加了超时与「放弃下载」出口；覆装前检查文件占用。更新仍校验 SHA-256，Linux / macOS 下载后需手动安装。<br>
这是**预发布版**，稳定版仍是 [v1.0.0](https://github.com/ExpertKT/notice-hub/releases/tag/v1.0.0)；稳定版通道仍按版本筛选，不会自动升级到测试版。

<a href="https://github.com/ExpertKT/notice-hub/releases/tag/v1.1.0-beta.10">
  <img src="docs/promo/poster-16x9.png" alt="群务台 v1.1.0 测试版：多来源接入、跨平台源码运行与界面体验修复" width="760" />
</a>

</div>

<p align="center">
  <img src="docs/screenshots/desktop-tasks.png" alt="桌面端待办总览：今天、本周、已过期分组，右侧是置顶日程与 QQ 接入" />
</p>

## 适合谁

如果作业、实验、报名和考试时间散在多个 QQ 群里，群务台会把需要你行动的内容集中到一个页面。消息、摘要和待办默认只存在你这台电脑上；只有你自己配的 AI 或推送通道会看到内容。

## 核心能力

- 只收你勾选的群；闲聊自动降噪，拿不准的消息先让你确认，再进待办。
- 首次启动显示全屏接入向导，可稍后配置；主界面保留未完成提醒，设置页可重新运行向导。
- QQ 群列表在后台常驻刷新，默认每 5 分钟拉取；失败按 1、2、5 分钟退避，页面显示最近状态和下次计划时间。
- 待办按今天、本周、以后、已完成分组，支持完成、忽略、置顶和稍后提醒；网页含月历，也可订阅手机日历。
- 截止提醒、紧急事项可推送至微信、钉钉、企业微信、飞书或 QQ 私聊；PDF、Office 文档、文本、压缩包和截图可解析并回溯原文。
- 设置页可一键备份数据库；ZIP 只包含 SQLite 数据库，不包含配置、令牌或日志。
- 设置页提供 QQ 接入向导、群选择和二维码登录；手机 App 支持扫码配对。网页也可通过 Tailscale 或配置后的公网入口访问。
- 钉钉接入读取本机数据库并接收通知，不支持发送钉钉消息。学习通 Cookie 支持浏览器自动读取、托盘读取、手动粘贴三种方式。
- Windows 出货包采用 onedir：运行时文件放在 `_internal` 等目录，启动时不再解压到临时目录。
- 更新校验 SHA-256；两个源版本与 SHA-256 一致时主下载走 Gitee 镜像、GitHub 兜底，不一致时保持 GitHub 主源。Linux / macOS 可检查和下载，但需手动安装。

## 快速开始

### Windows

1. 打开 [Releases](https://github.com/ExpertKT/notice-hub/releases)，下载最新版本的 `QQ-Notice-Hub.zip`。
2. 将 ZIP 解压到安装目录，例如 `D:\notice-hub`。目录里有启动器、服务程序以及 `_internal`、`_launcher_internal` 运行时目录；保持目录结构完整，不要在压缩包内运行 exe，也不要单独移动 exe。
3. 在解压后的目录运行 `QQ-Notice-Hub.exe`。如有 Windows 安全提示，选择「仍要运行」；防火墙提示时允许专用网络。随后按网页向导配置接入。

### Linux（Ubuntu / Debian）

源码运行并安装用户级 systemd 服务：

```bash
git clone https://github.com/ExpertKT/notice-hub.git ~/notice-hub
cd ~/notice-hub
bash packaging/linux/install.sh
```

启用后在浏览器打开 <http://127.0.0.1:8766/>。NapCat 在 Docker 或其他外部环境运行；Linux 版没有托盘。详见 [Linux 部署说明](docs/linux.md)。

### macOS

源码运行并安装用户级 LaunchAgent：

```bash
git clone https://github.com/ExpertKT/notice-hub.git ~/notice-hub
cd ~/notice-hub
bash packaging/macos/install.sh
```

启动后在浏览器打开 <http://127.0.0.1:8766/>。NapCat 使用官方 Mac-Installer 或其他外部方式运行；notice-hub 只连接 OneBot，不提供托盘。详见 [macOS 部署说明](docs/macos.md)。

### Android App

1. 在同一个 [Releases](https://github.com/ExpertKT/notice-hub/releases) 页面，下载 `notice-hub-android-*.apk`，传到手机上安装（安卓端本轮没有改动，与 beta.1 是同一份）。
2. 电脑端页面打开「设置」，找到「手机 App」那一块，屏幕上会出现一个二维码。
3. 用手机 App 扫这个二维码，地址和配对信息会自动填好。
4. 二维码扫不动时，改用那一块里的 **6 位数字配对码**，手输进 App 即可。
5. 手机和电脑连同一个 Wi-Fi 最省事；手机用流量时，先在电脑端设置里开通公网地址，或者让两端都连上同一个 Tailscale。

App 只是电脑端服务的外壳，消息和待办仍然存在电脑上；想自己编译见 [docs/安卓App.md](docs/安卓App.md)。

### 从源码跑（开发者）

Windows PowerShell：

```powershell
git clone https://github.com/ExpertKT/notice-hub.git
cd notice-hub
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe main.py doctor
.\.venv\Scripts\python.exe main.py run
```

`doctor` 检查配置和依赖，`run` 启动服务（默认网页端口 `8766`）。Linux 和 macOS 源码部署请按 [Linux 部署说明](docs/linux.md) 或 [macOS 部署说明](docs/macos.md) 操作。

## 平台支持

| 能力 | Windows | Linux | macOS |
| --- | --- | --- | --- |
| 安装方式 | 安装包（双击） | 源码 + `install.sh`（systemd user） | 源码 + `install.sh`（LaunchAgent） |
| 网页界面 / 推送通道 | ✅ | ✅ | ✅ |
| QQ（NapCat）接入 | ✅ 内置一键接入 | ✅ 自行部署 NapCat（Docker/本机） | ✅ 自行安装 NapCat |
| 系统托盘 / 浮球 | ✅ | ❌ | ❌ |
| 应用内自动更新 | ✅ 检查+下载+安装 | ❌ | ⚠️ 只能检查/下载，安装需手动 |
| 读本机微信/钉钉数据库 | ✅ | ❌ | ❌ |
| 学习通 Cookie 自动读取 | ✅ | ❌ 需手动粘贴 | ❌ 需手动粘贴 |
| 学习通网页功能本身 | ✅ | ✅（粘一次 Cookie） | ✅（粘一次 Cookie） |

带 ❌ / ⚠️ 的几项依赖 Windows 的系统接口（DPAPI 加密、进程内存读取、注册表），不是没适配，是平台机制不同。macOS 上微信数据库的密钥只能靠重签名微信 + lldb 断点取，普通用户做不到，且微信每次升级都要重来，所以明确不做。

各平台的部署细节与差异见 [Linux 部署说明](docs/linux.md) 和 [macOS 部署说明](docs/macos.md)。

## 配置速查

从 `.env.example` 复制 `.env`。首页只列高频项，完整说明请看 `.env.example` 和 `docs/`。

| 变量 | 作用 |
| --- | --- |
| `QQ_DIGEST_GROUPS` | 要监控的群号，逗号分隔；留空则不处理群消息 |
| `QQ_DIGEST_ONEBOT_TOKEN` | 与 NapCat OneBot HTTP 配置一致 |
| `QQ_DIGEST_WEB` | 是否启用网页待办台 |
| `QQ_DIGEST_WEB_PORT` | 网页端口，默认 `8766` |
| `QQ_DIGEST_WEB_BASE_URL` | 公网入口，供手机和推送链接使用 |
| `WXPUSHER_APP_TOKEN` | WxPusher 应用 Token（可选） |
| `WXPUSHER_UIDS` | WxPusher 接收人（可选） |
| `QQ_DIGEST_LLM_API_KEY` | 可选模型 Key（旧名 `DASHSCOPE_API_KEY` 仍兼容）；不填则使用本地规则摘要 |

## 开发者

### 模块

| 文件 | 作用 |
| --- | --- |
| `qq_digest.py` | 消息过滤与摘要入口 |
| `qq_live_digest/receiver.py` | 接收 NapCat OneBot v11 上报 |
| `qq_live_digest/store.py` | SQLite 存储、去重和恢复 |
| `qq_live_digest/summarizer.py` | 提取待办、截止时间和推送文本 |
| `qq_live_digest/push.py` | 推送通道与失败回退 |
| `qq_live_digest/service.py` | 调度、补采、重试和服务生命周期 |
| `qq_live_digest/webapp.py` | 待办台、月历、收件箱和设置页 |
| `qq_live_digest/napcat_admin.py` | NapCat 探测、启停、配置和登录二维码 |
| `qq_live_digest/hosting.py` | QQ/NapCat 托管切换与状态持久化 |
| `packaging/launcher.py` | 出货包启动器、托盘和单实例互斥 |
| `main.py` | CLI 入口 |

### 命令与测试

`main.py` 当前注册的子命令：`run`、`tick`、`preview`、`stats`、`show`、`tasks`、`catchup`、`doctor`、`send-test`、`attach-test`。

```powershell
# 全量测试（数字随改动增长，不在此写死）
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .

# Windows 出货包
.\packaging\build.ps1
```

Android 工程使用 Gradle，构建环境为 **JDK 17 + Android SDK 35**。发布时打 tag，再执行 `gh release create` 上传出货包和 APK。

## 常见问题

<details>
<summary><b>连接检查失败 / 由于目标计算机积极拒绝（10061）</b></summary>

NapCat 或目标端口尚未运行。点「一键接入」或「启动并登录」，用手机 QQ 扫码，登录后刷新检测。

</details>

<details>
<summary><b>订阅读不到 / Failed to fetch</b></summary>

局域网地址只对同一网络有效。手机使用流量时，在设置页开通公网地址或配置 Tailscale，并重新复制带配对信息的订阅地址。

</details>

<details>
<summary><b>群里发了通知，网页没有</b></summary>

确认群在订阅白名单并已保存；再检查「待确认」分组。关机期间只补采最近 24 小时消息。

</details>

<details>
<summary><b>二维码过期 / 主号风控</b></summary>

登录二维码由 NapCat 自己每隔一段时间换新，点「刷新二维码」只是立刻重取当前那张；想立刻换一张就点「重启引擎」再扫。订阅二维码的内容是订阅地址本身，地址没变码就不会变，所以设置里的「刷新二维码」会在没变时直接告诉你「订阅地址没变」。NapCat 是第三方 QQ 协议客户端，登录主号有风控风险；本项目只读群消息，是否使用小号请自行判断。

</details>

## 隐私与安全

不要提交 `.env`、日志、SQLite、附件缓存或任何含 Token、UID、API Key 的文件。提交 Issue 和截图前，请打码 QQ 号、群号、域名、本机路径与消息内容。若密钥泄露，应立即作废并重新生成。

## 已知限制

- NapCat 和电脑端服务必须运行并保持登录，才能实时接收；关机期间依赖历史补采。
- Windows 提供桌面托盘与自动安装更新；Linux 和 macOS 使用源码服务模式，更新包需手动安装。Android App 是电脑端服务的手机客户端。
- 摘要默认由本地规则生成；可选模型失败时会自动回退。

基于上游 [wc985732-lang/qq-live-digest](https://github.com/wc985732-lang/qq-live-digest) 二次开发。上游部分及其派生部分适用 MIT；本人新增部分适用 PolyForm Noncommercial 1.0.0。

## License

个人与非商业使用免费；商业使用请联系 jkl2755908073@qq.com 获取授权。详见 [LICENSE](LICENSE)、[LICENSE-UPSTREAM](LICENSE-UPSTREAM) 与 [NOTICE](NOTICE)。
