# windows

Windows 计划任务和状态检查脚本放在这里，由安装任务和维护者调用。

| 文件 | 作用 |
| --- | --- |
| `run.ps1` | 切到安装根目录，用 `.venv\Scripts\python.exe` 执行 `main.py run`，追加服务日志。 |
| `install-task.ps1` | 注册 `NapCat-QQ`、`NoticeHub`、`NapCat-QQ-Watchdog` 三个计划任务，顺手删掉旧名字 `QQ-Live-Digest`；有 `-InstallDir`、`-NapCatExecutable`、`-WhatIf`。 |
| `uninstall-task.ps1` | 按固定名称注销四个计划任务，不删除数据。 |
| `status.ps1` | 只读查询计划任务、端口、接口、OneBot 配置和日志。 |
| `检查状态.cmd` | 设置 UTF-8 控制台并调用同目录 `status.ps1`。 |

常见改动：改任务参数后跑 `.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_install_task tests.test_watchdog_task`；PowerShell 5.1 下保持脚本编码和路径解析可用，勿按进程名杀用户实例。
