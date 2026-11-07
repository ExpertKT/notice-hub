# 安装与升级

装完之后长这样：一个 `QQ-Notice-Hub.exe`，双击它启动服务、打开网页、在右下角放一个托盘图标。之后所有操作都在网页里做。

建议让 agent 来装：把这一段发给它，或者让它直接接手这台电脑。

## Windows：解压包

1. 从 [Releases](https://github.com/ExpertKT/notice-hub/releases) 下 `QQ-Notice-Hub.zip`。
2. 解压到你想放的位置，例如 `D:\notice-hub`。别在压缩包里直接双击 exe，也别把 exe 单独拖出去——`_internal`、`_launcher_internal` 这些运行时目录要跟它在一起。
3. 双击 `QQ-Notice-Hub.exe`。第一次会生成 `.env`、启动服务、打开浏览器，并在桌面建一个快捷方式。
4. 防火墙弹窗选「允许访问」，勾上「专用网络」。手机扫码和日历订阅都要靠它，点「取消」手机就连不上。
5. 页面里按向导走：扫码登录 QQ → 勾选要盯的群 → 保存。

首次启动要几秒。托盘图标出现就说明服务起来了。

### 从源码跑

```powershell
git clone https://github.com/ExpertKT/notice-hub.git
cd notice-hub
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe main.py doctor   # 检查配置和依赖
.\.venv\Scripts\python.exe main.py run      # 启动，网页端口默认 8766
```

### Linux（Ubuntu / Debian）

```bash
git clone https://github.com/ExpertKT/notice-hub.git ~/notice-hub
cd ~/notice-hub
bash packaging/linux/install.sh
```

装的是用户级 systemd 服务，浏览器打开 <http://127.0.0.1:8766/>。NapCat 要在 Docker 或别的机器上自己跑，Linux 版没有托盘。

### macOS

macOS 暂未适配（安装脚本 `packaging/macos/` 保留但未验证）。

### 安卓 App

在同一个 Releases 页面下 `notice-hub-android-*.apk`。装好后，电脑端「设置 → 手机 App」会显示二维码，用 App 扫它；扫不动就手输那 6 位配对码。

App 是电脑端服务的壳，消息和待办还在电脑上。

## 开机自启

安装包里带 `install-task.ps1`，在**管理员** PowerShell 里跑。先空跑看一眼它会注册什么：

```powershell
.\install-task.ps1 -InstallDir '安装目录' -WhatIf
```

确认 `Execute` / `Arguments` 对了，去掉 `-WhatIf` 真注册。它会幂等地建三个计划任务：`NapCat-QQ`、`NoticeHub`、`NapCat-QQ-Watchdog`。卸载用 `.\uninstall-task.ps1`。

`NapCat-QQ-Watchdog` 开机跑一次，之后每 5 分钟查一次服务还在不在，没了就拉起来。脚本注册完会再查一遍：任务不在、或者没有重复触发，都会直接说出来，不会当成功。

从托盘点「退出」主动关掉的话，它不会把你拉起来，直到你下次自己启动。

旧版本用的是 `QQ-Live-Digest` 这个名字，装 `NoticeHub` 时会自动删掉旧任务，免得开机起两个实例。

网页「设置」里也有个「开机自动启动群务台」的勾选框，那个只写当前用户的开机启动项，不需要管理员权限，和上面这套计划任务是两回事。

## 升级

### 让软件自己更新（推荐）

1. 网页「设置 → 版本更新」→「检查更新」。
2. 有新版本时卡片会列出新增和修复，右边有「查看完整更新说明 ↗」。
3. 「下载更新包」只下载和校验，**不安装**。下完点托盘 →「重启并更新」，十来秒到半分钟换好，刷新网页。
4. 启动时如果有新版本，页面顶部还会出现一条横幅，点它直接跳到那张卡片。

更新期间别关机、别退托盘。`data\` 和 `.env` 原样保留，被覆盖的只是程序文件。

### 同一个版本里的小修（修复包）

有时候几个小毛病不值得单开一个版本号。这种修法会作为一个**修复包**发在同一个版本上，装法跟普通更新一样，只是不用等新版本：

1. 「设置 → 版本更新」里版本号后面会写「修复 N」，N 越大越新。
2. 默认「自动接收修复包」是开的：后台自己下好，托盘「重启服务」变成「重启并更新」，点一下才装。
3. 装之前会留一份上一版在 `data\updates\rollback\`。装完觉得不对，就在设置页点「回到上一版」，它会退回去，并且不会再把同一个包自动装回来。
4. 不想让它自动下载，把「自动接收修复包」取消勾选即可；这时仍能自己点「检查更新」。

### 手动覆盖

1. 下载新的 zip。
2. 先托盘 →「退出」，把 `QQ-Notice-Hub.exe` 关掉。不关会报「拒绝访问」，因为文件被占着。
3. 解压覆盖到原目录，保留 `.env`、`data\`、`logs\`。
4. 重新双击 `QQ-Notice-Hub.exe`。

覆盖时如果提示「拒绝访问」，多半是旧目录里某个文件被设成只读：右键 → 属性 → 取消「只读」→ 再覆盖一次。

> 只要 `.env` 里的令牌没变，手机网页和 iPhone 日历订阅的地址就没变，不用重新订阅。改过令牌就得重新加一次订阅。

## 日志在哪

| 文件 | 记什么 |
| --- | --- |
| `安装目录\logs\qq-live-digest.log` | 服务本身的运行日志 |
| `安装目录\logs\launcher.log` | 启动器与看门狗：服务退出了、重启了 |

托盘没了但进程还在（少见）：任务管理器结束 `QQ-Notice-Hub.exe`，再双击启动。

## 平台差异

| 能力 | Windows | Linux |
| --- | --- | --- |
| 安装方式 | 解压包双击 | 源码 + `install.sh` |
| 网页界面、推送 | ✅ | ✅ |
| QQ（NapCat）接入 | 内置一键接入 | 自己部署 NapCat |
| 托盘 | ✅ | ❌ |
| 应用内自动更新 | ✅ 检查+下载+安装 | ❌ |
| 读本机微信、钉钉数据库 | ✅ | ❌ |
| 学习通 Cookie 自动读取 | ✅ | 手动粘贴 |

❌ 那几项靠的是 Windows 的系统接口（DPAPI、进程内存、注册表），不是没做适配。macOS 暂未适配（安装脚本 `packaging/macos/` 保留但未验证）。

配置项见 [配置](config.md)，装完怎么用见 [日常使用](use.md)。
