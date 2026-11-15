# .github

这里放 GitHub issue、PR、贡献规范和 CI 配置，GitHub 在提 issue、开 PR 或推送分支时读取。

| 文件/目录 | 作用 |
| --- | --- |
| `workflows/tests.yml` | GitHub Actions 测试流水线。 |
| `ISSUE_TEMPLATE/bug_report.yml` | 收集复现步骤、实际/预期结果和环境。 |
| `ISSUE_TEMPLATE/feature_request.yml` | 收集功能场景和需求说明。 |
| `ISSUE_TEMPLATE/config.yml` | issue 模板入口配置。 |
| `PULL_REQUEST_TEMPLATE.md` | 约束改动、影响范围、验收和文档同步信息。 |
| `CONTRIBUTING.md` | 贡献前的脱敏、测试、文档和 PR 要求。 |

常见改动：改 CI 先检查 `.github/workflows/tests.yml` 的真实 job；改提交模板只改对应模板并保持 `tests.test_acceptance_gate` 的静态契约。提交前运行相关 unittest 两轮并在 PR 填实际退出码。
