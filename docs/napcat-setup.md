# Windows 11：NapCatQQ 接入 qq-notice-hub

> 本文只配置“接收群消息”。项目的 OneBot v11 HTTP 接收器监听 `127.0.0.1:8765`；NapCat 应作为 **HTTP 客户端**，向该地址 POST 事件。不要把它配置成 HTTP 服务端。
>
> 关键结论均附官方来源；未能从官方资料确认的内容标为“未核实”。本文未安装软件、未启动服务、未修改项目代码。

## 1. 当前版本与下载

查询官方 GitHub Releases API（查询时间以该 API 返回为准）：

- 最新标签：`v4.18.33`
- 发布时间：`2026-10-06T16:19:21Z`
- Release 页面：https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.33
- API 原始查询：https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest
- Windows 无头包（官方 Release asset）：https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.33/NapCat.Shell.Windows.OneKey.zip
- Windows Shell 包：https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.33/NapCat.Shell.zip

版本和发布时间是上述 API 实际返回值；下载地址来自同一 API 返回的 asset。发布页内容可能变化，安装前请重新检查。

## 2. 前置条件与选择

NapCat 是基于 NTQQ 的协议端。官方安装说明的顺序是“安装对应版本 NTQQ，再下载 NapCat，再按教程启动”。因此应先安装官方 QQ NT（Windows 版），但具体兼容版本未核实：https://napneko.github.io/guide/install

- **Shell**：无头、资源占用低，适合本项目的后台消息采集；官方文档提供 Windows 手动 Shell 和 Windows OneKey 包：https://napneko.github.io/guide/boot/Shell
- **Framework**：有头、便于人机交互和窥屏；官方文档将其与 Shell 分开说明：https://napneko.github.io/guide/boot/Framework
- 本项目推荐 **Shell**。QQ 安装目录、所需 NTQQ 具体版本和是否必须使用 OneKey 包，官方当前页面未给出可固定的版本矩阵，均未核实。

只下载、解压官方 Release 包，不要从第三方镜像下载。本文不执行安装。

## 3. 自动安装

如果页面检测不到 NapCat，点击接入区的「自动下载并安装 NapCat」。程序只从官方 GitHub Release 下载约 1 MB 的 `NapCat.Shell.Windows.OneKey.zip`，解压到项目数据目录下的 `<数据目录>\\napcat`，不会修改电脑上自己的 QQ。安装完成后即可回到页面点击「启动 NapCat 并登录」。不想继续使用时删除该 `napcat` 文件夹即可卸载；也可以继续按下文手工安装，二者互为替代方案。

## 4. 启动、扫码与登录态

> **推荐路径**：你不用自己启动 NapCat。装好解压出 `NapCatWinBootMain.exe` + `QQ.exe` + `NapCatWinBootHook.dll` 之后，直接在 notice-hub 页面上点「启动 NapCat 并登录」，程序会带独立资料目录把它拉起来。本节余下步骤是手工启动/手工进 WebUI 的备选路径。NapCat 的 HTTP 服务端与 WebUI 端口分别默认 `3001` 与 `6099`，页面上的「托管状态」会显示 NapCat 是否在跑、是否已登录。

1. 按 Shell 官方 Windows 教程启动 NapCat；首次启动后查看控制台输出的 WebUI URL。WebUI 默认端口为 `6099`，实际端口可能因占用自动递增：https://napneko.github.io/config/basic
2. 浏览器打开控制台显示的 WebUI 地址（形如 `http://127.0.0.1:6099/webui?token=...`）。
3. 在 WebUI 进入“QQ 登录”，选择 `QRCode`，用主号 QQ 扫码确认。官方文档确认扫码入口；手机端/控制台可看到刷新后的 WebUI token，首次进入通常需要修改 WebUI 密码：https://napneko.github.io/config/basic
4. 登录成功后进入“网络配置”，创建客户端并勾选“保存时启用”。

### 必须使用独立 QQ 资料目录

NapCat 必须使用独立的 QQ 资料目录启动，否则可能抢用你日常 QQ 的登录态。启动 NapCat 时给启动参数加上 `--user-data-dir=<一个空目录>`，例如 `--user-data-dir=F:\NapCatData\qq-notice-hub`；该目录应专门给 NapCat 使用，不要指向日常 QQ 的资料目录。

登录凭据是否以何种文件、何种有效期持久化，以及每次重启是否必然免扫码，官方当前文档未明确，标记为**未核实**。实际应以 QQ/NapCat 重启后的行为为准；不要把 QQ 登录文件复制或提交到仓库。

### 同一个 QQ 号不能在两个电脑端同时在线

这是腾讯服务端的规则，NapCat 无法绕过：NapCat 登着你的号，你自己的电脑版 QQ 就登不上（反之亦然）。

notice-hub 的「**托管**」就是替你做这个切换：托盘图标或网页设置里的「开始托管」= 先关掉电脑版 QQ、再启动 NapCat；「结束托管」= 停掉 NapCat、把电脑版 QQ 开回来。它**只关进程、再开进程**，不修改你的 QQ 任何文件。

想两边随时都能用，最省事的办法是**申请一个 QQ 小号专门给机器人**。

## 4. 配置 HTTP 上报到本项目

### 推荐：页面上一键接入

启动项目后打开主页面，在「接入状态」里选择登录方式：

- **扫码登录（推荐）**：程序按独立资料目录启动 NapCat，页面显示二维码，用手机 QQ 扫；
- **用指定 QQ 号快速登录**：填 QQ 号免扫码——**前提是这个号已经在这个独立资料目录里成功登录过一次**（NapCat 的快速登录依赖本机已有登录态，见 `bootmain\ReadMe.txt`「需要登录过一次」）。

点「启动 NapCat 并登录」后，程序会自动探测本机 NapCat、以 `--user-data-dir=<data>\napcat-profile` 启动它，再写入上报地址、端口和 token。登录成功后勾选群并保存即可。

程序不替你安装 QQ；如果没有安装 NapCat，页面会给出官方下载地址（`detect_boot()` 只识别同时具备 `QQ.exe` + `NapCatWinBootHook.dll` 且非纯壳的同级目录，绝不返回你日常 QQ 的 `QQ.exe`）。下面的手工 WebUI 配置是备选方案。

### 备选：WebUI 手工配置

在 NapCat WebUI：**网络配置 → 新建 → HTTP 客户端**，填写：

- 名称：`qq-notice-hub`（名称需唯一）
- 启用：开启；保存时启用
- 上报地址：`http://127.0.0.1:8765`
- 消息上报格式：`array`
- 上报自身消息：关闭（`reportSelfMessage=false`）
- token：填写与项目 `.env` 中 `QQ_DIGEST_ONEBOT_TOKEN` 完全相同的值
- debug/raw：关闭

NapCat 官方定义：HTTP 客户端是“NapCat 作为 HTTP 请求发起方，将事件推送至插件/应用框架”；官方字段包括 `url`、`messagePostFormat`、`reportSelfMessage`、`token`、`debug`：https://napneko.github.io/config/basic

项目端确认的接收地址和环境变量：

- 地址：`127.0.0.1:8765`
- 配置名：`QQ_DIGEST_ONEBOT_HOST`、`QQ_DIGEST_ONEBOT_PORT`、`QQ_DIGEST_ONEBOT_TOKEN`
- 示例位置：`项目目录\.env.example` 第 92–106 行
- 事件应由 NapCat POST 到 `http://127.0.0.1:8765`；路径是否由项目接收器追加、不能凭 NapCat 文档确定，若 WebUI 要求路径请以项目实际路由/启动日志为准（未核实）。

### 还需要：HTTP 服务端（端口 3001）

只配 HTTP 客户端还不够。本项目还会**主动调用 NapCat 的 OneBot API**（启动/每 30 分钟做历史补采 `get_group_msg_history`，以及取群列表 `get_group_list`），所以 NapCat 里要再建一个 **HTTP 服务端**：

- 名称：`api`（名称唯一）
- 启用：开启
- 监听：`127.0.0.1`，端口 `3001`
- token：与 `QQ_DIGEST_NAPCAT_API_TOKEN` 一致（本项目里该值留空时会复用 OneBot token，`.env` 中两者已设为同一个）
- 消息格式：`array`

代码依据（已核实，非猜测）：`qq_live_digest/config.py:202` 默认 `napcat_api_url = "http://127.0.0.1:3001"`；`qq_live_digest/catchup.py:67` 用 `NapCatClient` 调用 `get_group_msg_history`；接口地址由 `.env` 的 `QQ_DIGEST_NAPCAT_API_URL` 覆盖。字段名（host/port/token 等）以 WebUI 实际表单为准。

### 配置文件方式（不推荐）

官方说明：v4.5.3 后支持 `./config/onebot11.json` 作为默认配置，账号配置名为 `./config/onebot11_xxxx.json`；相对路径基准和 Windows 实际绝对目录未在页面确认，标为**未核实**。官方明确建议除非熟悉否则不要手改：https://napneko.github.io/config/basic

HTTP 客户端最小结构（JSON5，按官方字段）：

```json5
{
  "network": {
    "httpServers": [],
    "httpClients": [
      {
        "name": "qq-notice-hub",
        "enable": true,
        "url": "http://127.0.0.1:8765",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": "替换为QQ_DIGEST_ONEBOT_TOKEN",
        "debug": false
      }
    ],
    "websocketServers": [],
    "websocketClients": []
  }
}
```

官方配置页原文同时给出 HTTP 服务端的 `host`/`port` 和 HTTP 客户端的 `url`；本项目场景只使用后者。不要把 `127.0.0.1:8765` 填到 `httpServers.port`，否则 NapCat 会监听端口而不是向项目上报。

## 5. 验证链路与群列表

先启动项目，再启动 NapCat 并让一个测试群发送普通消息。项目日志应显示收到 OneBot v11 事件；HTTP token 不匹配时应检查两端 token 是否完全一致。不要用真实敏感内容做首测。

获取机器人可见群列表（打到上一步建的 **3001** 端口 HTTP 服务端，不是 8765）：

```powershell
$token = '替换为QQ_DIGEST_NAPCAT_API_TOKEN'
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:3001/get_group_list' `
  -Headers @{ 'Authorization' = "Bearer $token"; 'Content-Type' = 'application/json' } `
  -Body '{}' | ConvertTo-Json -Depth 5
```

已核实：本项目的接收器 `qq_live_digest/receiver.py:206` 的 `do_POST` **不校验路径**，任何 path 都会被当作事件处理——所以 8765 端只用于接收上报，取群列表必须走 3001 的 HTTP 服务端。OneBot API 的通用接口定义：https://napneko.github.io/onebot/api

返回中的每项通常包含 `group_id` 和 `group_name`；具体返回字段以实际响应为准，字段兼容性未在本次调研中实测。把需要的群号加入项目白名单配置，勿把 token 写入命令历史或仓库。

**另一种拿群号的办法（不需要 3001 也能用）**：`.env` 里 `QQ_DIGEST_GROUPS` 留空时项目是 fail-closed 的，任何群消息都会被忽略，但**日志会打印群号**——`qq_live_digest/receiver.py:258` 的 `logger.info("忽略白名单外的群：%s", record["group_id"])`。让目标群随便发一条消息，然后看 `项目目录\logs\` 就能拿到群号。

## 6. 隐藏启动 / 开机自启

项目已有 `项目目录\install-task.ps1`，应沿用其 Windows 计划任务风格：创建“登录时”或“开机时”任务，动作指向 NapCat Shell 的启动脚本/可执行文件，设置“隐藏窗口”，工作目录指向 NapCat 安装目录，并配置失败重启。

NapCat 官方页面未提供 Windows 计划任务字段或推荐的静默启动命令，因此具体 exe/参数、任务 XML 和是否需要先启动 QQ 均为**未核实**。首次应先手动启动并确认 WebUI、登录态、HTTP 上报均正常，再按现有项目脚本创建任务。任务账户必须是保存 QQ 登录态的同一 Windows 用户，避免权限/配置目录不一致。

## 7. 风险、只读边界与断线

- 主号登录机器人存在账号风控、冻结或限制风险；NapCat 官方安全页面明确提示使用风险由使用者承担：https://napneko.github.io/other/security
- 本项目目标是只读接收群消息。不要给 NapCat 配置发送消息的 API 调用，不要运行 `/send_msg` 等动作；NapCat 本身具备 OneBot API 能力，权限边界靠使用方式保证。
- HTTP 客户端是单向事件推送；NapCat 官方将其与双工 WebSocket 区分：https://napneko.github.io/config/basic
- 断线自动重连间隔、登录掉线后的重新扫码规则、事件重试/丢失语义，本次官方页面未明确，均为**未核实**。应把项目日志和 NapCat 控制台纳入监控，并在断线后用测试群消息验证恢复。

## 下一步清单

1. 在 Windows 11 安装对应 NTQQ，并从官方 Release 下载 Shell 包。
2. 手动启动 NapCat，扫码登录主号，修改 WebUI 密码。
3. 启动 `qq-notice-hub`，确认 `.env` 中 host/port/token。
4. WebUI 创建并启用 HTTP 客户端：URL `http://127.0.0.1:8765`，token 与项目一致，`reportSelfMessage=false`。
5. 用测试群消息验证事件，再获取群列表并配置白名单。
6. 确认重启后的登录态和断线恢复后，再参考 `install-task.ps1` 配置隐藏计划任务。
7. 记录 NapCat 与项目日志中的失败请求、token 错误和重连信息；不要提交登录态、token 或群成员隐私数据。
