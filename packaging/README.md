# packaging

这里放 PyInstaller 规格、启动器和跨平台安装/发布输入；发布者和安装脚本使用它。

| 路径 | 作用 |
| --- | --- |
| `build.ps1` | 构建服务和启动器、复制资源并生成 ZIP；必须 UTF-8 BOM。 |
| `noticehub.spec` | 收集 `main.py`、PIL、pypdfium2 和整个 `web_static`。 |
| `launcher.spec` | 收集托盘、tkinter、PIL 和启动器依赖。 |
| `launcher.py` | 单实例托盘、拉起服务、健康检查、更新线程和退出。 |
| `app.manifest` | 声明 Windows PerMonitorV2 DPI。 |
| `fix-napcat.ps1`、配套 `.cmd` | 找已有 NapCat 并写入 `.env`。 |
| `创建桌面快捷方式.cmd`、`先看我.txt` | 创建安装快捷方式和包内首次使用说明。 |
| `linux/` | systemd user unit 模板及 Linux 安装脚本。 |
| `macos/` | LaunchAgent plist、安装和卸载脚本。 |
| `build/`、`dist/`、`*.zip`、`build-last.log` | 构建中间物、输出包和日志，不是源码。 |

常见改动：改打包输入后运行 `powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build.ps1`；构建后用 `powershell -NoProfile -ExecutionPolicy Bypass -File tools\gate.ps1 -SkipL3` 检查，不要把 `dist/` 当源码改。
