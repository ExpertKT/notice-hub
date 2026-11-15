# 仓库结构

按用途找代码和资料；产品行为或安装流程变更时，同步更新 `docs/wiki/changelog.md` 与对应说明页。

| 目录 | 放什么 | 常见改动落在哪 |
| --- | --- | --- |
| `noticehub/` | Python 服务、业务逻辑与网页静态资源 | 按功能分在 `web/` `receivers/` `qq/` `ingest/` `delivery/` `floatball/` `update/` `remote/` `platform/` 九个目录（另有更早的分层目录并存）；页面改 `noticehub/web/web_static/`，逐个文件的作用见 `noticehub/README.md` |
| `tests/` | 单元、集成和浏览器测试 | 与被测模块同主题的 `tests/test_*.py` |
| `docs/` | 用户文档、产品截图与开发资料 | 用户说明改 `docs/wiki/`；截图放 `docs/screenshots/`；开发过程资料只读归档在 `docs/internal/` |
| `tools/` | 发布、构建、同步和诊断工具 | 修改发布/验收脚本时改对应工具，并核对其调用参数 |
| `packaging/` | Windows、Linux 等平台的打包配置与构建脚本 | 安装包组装改 `packaging/build.ps1` 或对应平台子目录 |
| `android/` | Android App 的 Gradle 工程 | App 界面与行为改 `android/app/` |
| `assets/` | 桌面启动器、托盘等静态资源 | 图标等源资源放在本目录 |
| `release/` | 发布用更新清单与签名文件 | 仅在获准发布时更新清单及签名 |
| `scripts/` | 跨平台开发和维护辅助脚本 | 改对应脚本，并保持仓库根与安装包调用约定一致 |
| `windows/` | Windows 计划任务/自启脚本 | 修改 `run.ps1`、`install-task.ps1`、`uninstall-task.ps1`、`status.ps1` 或 `检查状态.cmd` |
| `.github/` | GitHub Actions、Issue 与 PR 模板 | CI 工作流改 `.github/workflows/`；协作模板改对应模板文件 |

根目录保留项目入口、依赖清单、环境示例、公告和仓库规则等少量文件；新增文件前先判断是否属于现有用途目录。
