# macOS 部署

采用源码运行 + LaunchAgent；不打包 `.app`，不做代码签名。服务以当前 macOS 用户身份运行。

## 安装

安装 Homebrew、Git 和 Python 3.12：

```bash
brew install git python@3.12
```

将仓库放在长期保留的目录，然后创建虚拟环境、安装依赖并注册 LaunchAgent：

```bash
git clone https://github.com/ExpertKT/notice-hub.git ~/notice-hub
cd ~/notice-hub
PYTHON="$(brew --prefix python@3.12)/bin/python3.12" bash packaging/macos/install.sh
```

脚本会检查 Python 至少为 3.10，创建 `.venv` 并安装 `requirements.txt`。若 `.env` 不存在，会从 `.env.example` 复制；随后将 `QQ_DIGEST_WEB_HOST` 设为 `127.0.0.1`。已有 `.env` 的其它设置会保留。LaunchAgent plist 根据仓库路径生成到 `~/Library/LaunchAgents/com.qqnoticehub.service.plist` 并加载。

打开本机网页：<http://127.0.0.1:8766/>。

## LaunchAgent

安装脚本会加载服务。手动停止或启动：

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.qqnoticehub.service.plist"
launchctl load "$HOME/Library/LaunchAgents/com.qqnoticehub.service.plist"
launchctl list | grep com.qqnoticehub.service
```

服务退出失败时由 `KeepAlive` 重新启动。标准输出和错误日志分别写到仓库下的 `logs/launchd.out.log`、`logs/launchd.err.log`；应用日志写到 `logs/qq-live-digest.log`。

卸载 LaunchAgent、保留数据和配置：

```bash
bash packaging/macos/uninstall.sh
```

显式传入 `--purge` 才会询问是否删除仓库下的 `data/`。该目录含本地 SQLite 数据库中的消息、待办及状态；确认删除后无法恢复。`.env`、日志和 `.venv` 会保留。若曾把 `QQ_DIGEST_DATA_DIR` 指到仓库外，需单独管理那份目录。

## QQ 与推送

支持网页管理、配置好的推送通道，以及由用户单独管理的 NapCat OneBot 接入。需要 NapCat 时，请使用 [NapCat 官方 Mac-Installer](https://github.com/NapNeko/NapCat-Mac-Installer)，在 NapCat 中配置 OneBot v11；notice-hub 只连接 OneBot，不安装、启动或管理 NapCat。接收地址默认是 `http://127.0.0.1:8765/onebot/event`，NapCat HTTP API 默认是 `http://127.0.0.1:3001`。

## 当前不支持

- 系统托盘及浮球窗口。
- 读取本机微信、钉钉数据库。
- 学习通浏览器 Cookie 自动读取（依赖 Windows DPAPI/CNG）。
- 自动安装更新。检查更新与下载仍可用；安装时会显示更新说明链接和 ZIP 路径，请手动下载安装。
