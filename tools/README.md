# tools

这里放装包、渲染、清理、发布清单和 Windows 计划任务验收脚本；维护者和发布流程使用它们。

| 文件 | 实际作用 |
| --- | --- |
| `e2e-install.ps1` | 把 ZIP 解到一次性目录，首启、探活并检查安装/升级保留；`-InstallDir` 不能指向用户实例。 |
| `e2e-render.ps1` | 用 Node 检查内联 JS，再用无头 Edge 验证页面真实渲染。 |
| `gate.ps1` | 运行分层门禁、检查包时效和发布清单；当前 freshness 输入需与 `packaging/build.ps1` 继续对齐。 |
| `announce.py` | 列出、添加、移除和校验公告。 |
| `packcheck.py` | 检查 PyInstaller 包中的代码内容。 |
| `verify_manifest.py` | 校验更新清单字段和下载 URL。 |
| `version_bump.py` | 检查或写入版本号的多个落点；`--dry-run` 只打印。 |
| `syntaxcheck.py` | 检查 Python、PowerShell 和 `PAGE_HTML` 内联 JavaScript 语法。 |
| `wechat_probe.py` | 独立诊断微信密钥候选、数据库页和解密结果；输出可能包含敏感诊断信息。 |
| `tray-smoke.ps1` | 在临时安装目录做桌面托盘冒烟；无交互桌面时按脚本结果跳过。 |
| `watchdog-task.ps1` | 每 5 分钟检查指定安装根目录的服务并按目录启动，不按进程名误杀。 |
| `cleanup-install.ps1` | 列出或清理安装残留；写操作必须显式 `-Apply`。 |
| `run-hidden.vbs` | 用隐藏窗口异步启动 PowerShell 脚本。 |
| `wiki_sync.ps1` | 把 `docs/wiki` 同步到 GitHub Wiki，会操作远程。 |

PowerShell 脚本按 Windows PowerShell 5.1 编写；`e2e-install.ps1` 的 `-Overwrite` 会覆盖目标目录，验收只能用一次性目录。典型渲染命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\e2e-render.ps1 -Url http://127.0.0.1:8877/ -InstallDir D:\nh-tools
```

改 Python 工具跑对应 `tests.test_<模块>`；改页面或渲染脚本还跑 `.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_web_static_assets tests.test_ui_browser`，并先设 `NH_REQUIRE_BROWSER=1`。冷装脚本结束后确认并关闭临时实例，不使用 8766。
