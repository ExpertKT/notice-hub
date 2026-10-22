# Linux 部署

支持 Ubuntu 24.04 / Debian 系发行版的无托盘运行。当前验证目标是网页服务与后台通知服务；桌面托盘及依赖 Windows 的本机集成不在 Linux 支持范围内。

## 安装

安装 Python 3、venv 和 Git：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

克隆仓库到一个长期保留的目录，然后运行安装脚本：

```bash
git clone https://github.com/ExpertKT/notice-hub.git ~/notice-hub
cd ~/notice-hub
bash packaging/linux/install.sh
```

脚本会在仓库内创建 `.venv`、安装 `requirements.txt`、仅在 `.env` 不存在时复制 `.env.example`，并将按当前仓库路径填好的 systemd user unit 安装到 `~/.config/systemd/user/notice-hub.service`。现有 `.env` 不会被覆盖。

安装脚本创建新 `.env` 时，会将示例中的 `QQ_DIGEST_WEB_HOST=0.0.0.0` 改为 `127.0.0.1`；已经存在的 `.env` 不改动。未显式配置该变量时，Linux 默认也是 `127.0.0.1`，环境变量仍优先于默认值。然后可在本机浏览器打开 <http://127.0.0.1:8766/>。

如需从局域网访问网页，才将 `QQ_DIGEST_WEB_HOST` 设为 `0.0.0.0`，并配置防火墙及访问令牌。不要把服务端口直接暴露到公网。

## systemd

安装脚本会打印启用命令。也可以手动运行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now notice-hub.service
systemctl --user status notice-hub.service
journalctl --user -u notice-hub.service -f
```

停止或禁用：

```bash
systemctl --user stop notice-hub.service
systemctl --user disable notice-hub.service
```

服务异常退出后由 systemd 按 `Restart=on-failure` 重启。默认文件日志在仓库目录的 `logs/qq-live-digest.log`；systemd 日志可用 `journalctl` 查看。需要在用户注销后仍运行时，可由管理员为该 Linux 用户启用 linger：`sudo loginctl enable-linger "$USER"`。

## 可选：NapCat Docker

NapCat 不是网页服务的启动前提。需要接入 QQ 时，可按 [NapCat-Docker 官方 Compose 示例](https://github.com/NapNeko/NapCat-Docker/blob/main/compose/qq-ai-bot.yml)部署，并配置 OneBot v11 HTTP 上报；在 `.env` 中启用 `QQ_DIGEST_ONEBOT_ENABLED=1`。

宿主机上的服务默认监听 `QQ_DIGEST_ONEBOT_HOST=127.0.0.1`、端口 `8765`；宿主机服务调用 NapCat API 的默认地址是 `http://127.0.0.1:3001`。Docker 容器内的 `127.0.0.1` 指向容器本身，不是宿主机。若使用容器网络访问宿主机的 OneBot 接收端，应使用 Linux host network，或将 `QQ_DIGEST_ONEBOT_HOST` 绑定到可从容器到达的宿主机接口，并限制防火墙访问；此时把 NapCat 的反向 HTTP 地址配置为宿主机可达地址加 `/onebot/event`。只在本机运行 NapCat 时，`127.0.0.1` 是合适的地址。

## 当前不支持

- 系统托盘及浮球窗口。
- 应用内自动更新。
- 读取本机 Windows 微信、钉钉数据库。
- 学习通浏览器 Cookie 自动读取（Windows DPAPI/CNG）。
