# tests.md — 测试手册

基准：HEAD `ad67e3e` + 工作区未提交改动，`.venv` 已装齐 `requirements.txt`、
**未装** `requirements-dev.txt`（playwright）。

---

## 1. 全量命令

README `README.md:197-199` 与 CI `.github/workflows/tests.yml:34,54,147` 用的是同一条：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

**当前基线（本机实测三遍，见第 4 节）**：

```
Ran 1325 tests in 249.460s
OK (skipped=1)
```

- 58 个 `test_*.py`（另有 `tests/test_acceptance_gate.py` 54 行，归 B 的队占用，
  本文不覆盖）
- 1 项 skip = `tests/test_ui_browser.py:45` 的
  `@unittest.skipUnless(HAVE_PLAYWRIGHT, "playwright 未安装")`
- 无 `pyproject.toml` / `setup.cfg` / `pytest.ini` / `conftest.py`，纯 stdlib unittest

## 2. 单测与集成测试的划分

没有正式的目录分层，按**是否起 HTTP 服务 / 子进程 / 摸宿主机**分三组。

### G1 纯函数单测（无外部依赖，最快）

| 文件 | 行数 | 测什么 |
|---|---|---|
| `test_event_extract.py` | 114 | 正文时间/地点抽取规则 |
| `test_deadline_relative.py` | 39 | 「今天18:00」「下周」等相对时间 |
| `test_rules.py` | 229 | 分类/重要性规则 |
| `test_summarizer.py` | 442 | 摘要拼装、`finalize_digest` |
| `test_retry.py` | 102 | 重试与退避 |
| `test_qr.py` | 48 | 二维码 PNG 生成 |
| `test_digest_screen.py` | 27 | 摘要脱敏 |
| `test_logging_setup.py` | 80 | 日志配置 |
| `test_no_duplicates.py` | 107 | 无重复定义守卫 |

### G2 带存储/HTTP 的集成测试（占大头，用临时目录）

| 文件 | 行数 | 测什么 |
|---|---|---|
| `test_webapp.py` | 6758 | **HTTP 路由集成主战场**；`PAGE_SOURCE`/`PAGE_HTML` 断言 255 条 |
| `test_service.py` | 859 | `DigestService` 编排、`on_message`→`tick`→`_sync_tasks` |
| `test_store.py` | 457 | 11 张表读写、列迁移 |
| `test_updater.py` | 1170 | 更新检查/下载/校验/安装脚本生成 |
| `test_wechat_receiver.py` | 970 | 微信内存读取、密钥分类 |
| `test_napcat_admin.py` | 846 | NapCat 下载/解压/配置/进程 |
| `test_attachments.py` | 879 | 附件解析（Excel/PDF/图片） |
| `test_hosting.py` | 559 | QQ 托管 start/stop |
| `test_push.py` | 528 | 各推送通道 |
| `test_announce.py` | 492 | 公告缓存与版本过滤 |
| `test_browser_cookie.py` | 477 | 浏览器 cookie 导入 |
| `test_chaoxing.py` | 615 | 学习通判活/课程 |
| `test_launcher_watchdog.py` | 1058 | launcher 看门狗、`--quit` |
| `test_wechat_integration.py` | 321 | 微信端到端（内存桩） |
| `test_wechat_probe.py` | 127 | 微信探测 |
| `test_inbox.py` | 194 | 收件箱分类/提升 |
| `test_dingtalk.py` | 207 | 钉钉本地库 |
| `test_receiver.py` | 205 | OneBot 接收 |
| `test_group_suggest.py` | 195 | 群名建议 |
| `test_replies.py` | 122 | 回复候选 |
| `test_search.py` | 171 | FTS 检索 |
| `test_config.py` | 295 | `Settings.from_env` |
| `test_doctor.py` | 175 | 诊断输出 |
| `test_catchup.py` | 152 | 历史补采 |
| `test_lab.py` | 154 | 实验室四功能 |
| `test_onboarding_api.py` | 187 | 向导接口 |
| `test_routes.py` | 276 | **路由契约锁**（与 api-contract.md 对应） |
| `test_calendar_public.py` | 75 | 公网 ICS |
| `test_tunnel.py` | 157 | Tailscale Funnel |
| `test_community.py` | 247 | 社区计数 |
| `test_selfcheck.py` | 115 | 自检聚合 |
| `test_overview_sources.py` | 100 | 概览来源 |
| `test_pairing.py` | 66 | 配对 |
| `test_version_bump.py` | 170 | 版本号单调 |
| `test_verify_manifest.py` | 65 | 清单校验 |
| `test_update_sources.py` | 51 | 更新源 URL |
| `test_tasks_latency.py` | 123 | 待办延迟基线 |
| `test_perf_baseline.py` | 107 | 性能基线 |
| `test_win_text.py` | 118 | Windows 文本 |
| `test_host_actions.py` | 216 | netsh/UAC |
| `test_temp_hygiene.py` | 55 | 临时目录卫生 |
| `test_main.py` | 60 | CLI 入口 |
| `test_codebuddy.py` | 102 | LLM 后端 |

### G3 平台相关（有 skip 条件）

| 文件 | 条件 | 缺失时行为 |
|---|---|---|
| `test_ui_browser.py:45` | `HAVE_PLAYWRIGHT`（装了 `requirements-dev.txt` 才有） | **整文件 skip**，这就是那 1 个 skip |
| `test_floatball_host.py:8`、`test_floatball_window.py:5` | 浮球启用 / Windows | skip |
| `test_webapp.py` | 无 `node` 时 `skipTest`（约 `:6830`） | skip |
| 一批用例 | `skipUnless(os.name == "nt")` | 非 Windows skip |
| `test_napcat_admin*.py`、`test_hosting.py`、`test_updater.py`、`test_browser_cookie.py`、`test_wechat_*.py`、`test_launcher_watchdog.py` | 各自的外部条件（进程/文件/浏览器） | skip |

**关键点：软依赖缺失只 skip，不会红。** 所以「CI 全绿」不等于「这些路径被测到了」。
重写方在干净机器上跑全量，拿到的绿灯覆盖度可能低于本机。

## 3. 当前通过情况

三遍全绿，见下节。1 项 skip 的原因已定位（`test_ui_browser.py:45`）。

## 4. 偶发失败（连续三遍实测）

串行跑三遍，每遍完整全量：

```
RUN1  Ran 1325 tests in 249.460s   OK (skipped=1)   EXIT=0
RUN2  Ran 1325 tests in 250.010s   OK (skipped=1)   EXIT=0
RUN3  Ran 1325 tests in 245.188s   OK (skipped=1)   EXIT=0
```

**三遍完全一致，未观察到偶发失败。**

已知的历史 flake（当前未复现，但重写时可能回来）：

| 现象 | 历史原因 |
|---|---|
| `ConnectionAbortedError (WinError 10053)` | 本机 socket 抖动；单跑即过 |
| 端口竞争 | 测试起 HTTP 服务用随机端口，**并发跑会互相打架**。必须串行 |
| CI 上被用户名/代码页/短路径搞崩 | `812d554`、`38e53e1`、`40c18d7`、`9a1a40f` 逐个修掉 |

## 5. 测试数据怎么准备

- **全部用临时目录**：测试内部建 `TemporaryDirectory` / `tempfile`，
  不碰仓库 `data/`、`logs/`。唯一例外是 `tests/test_webapp.py:1354` 用一个**同名**
  临时文件 `tasks.sqlite3`（不是仓库里那个遗留产物）。
- **不需要预先存在的文件**：`.env` 也是测试内构造（`tests/test_webapp.py` 的
  env 模板）。
- **环境变量**：测试用 `mock.patch.dict(os.environ, ...)` 隔离，
  不需要在 shell 里预设。
- **会因「缺东西」行为不同的用例**：
  - 缺 `playwright` → `test_ui_browser` 整文件 skip
  - 缺 `node` → `test_webapp` 里浏览器相关 `skipTest`
  - 缺 `pypdf`/`pypdfium2`/`openpyxl` → 附件类 skip
  - 非 Windows → 一批 `skipUnless(os.name == "nt")` skip
  - 有真实 QQ/NapCat 进程 → 部分用例走真实分支（`test_napcat_admin`、`test_hosting`）

## 6. CI 口径

`.github/workflows/tests.yml`，5 个 job + 1 个汇总门：

| job | runs-on | 跑什么 | 行 |
|---|---|---|---|
| `unittest` | windows-latest, py3.12 | 全量 discover | `:34` |
| `linux` | ubuntu-latest | 全量 discover | `:54` |
| `macos` | macos-latest | 全量 discover | `:147` |
| `tray-smoke` | windows-latest | `packaging\build.ps1` 打包 + `tools\tray-smoke.ps1 -NoUi` | `:90` |
| `browser` | windows-latest | `python -m unittest tests.test_ui_browser -v`（playwright + 预装 Edge） | `:119` |
| `ci-green` | ubuntu-latest | 汇总：任一 job 非 success 即红 | `:154-171` |

本地与 CI 的差异：

- **本地没装 playwright** → `browser` job 的那些用例本地是 skip，CI 是真跑。
- **`tray-smoke` 需要先打包**（CI 里先建 `.venv` 再跑 `build.ps1`），本地跑一次要几分钟。
- 触发条件 `on: push / pull_request`（`:3-5`），**没有分支过滤** ——
  推 `rewrite/clean-room` 会自动跑。但 `ci-green` 要成为分支保护的必需检查，
  得在 GitHub 侧手工设。

---

## 必须由人补的项

1. **`tests/test_acceptance_gate.py`（54 行）没读** —— 归 B 的队占用，不在本文范围。
2. **255 条 `PAGE_SOURCE` 断言的具体清单**没逐条列（backlog 自称「约 325 条，两周量」，
   口径与本文的 255 条统计方法不同：本文数的是 `PAGE_SOURCE`/`PAGE_HTML` 出现次数，
   backlog 数的是待清理条目）。要以哪个口径排期需人工定。
3. **哪些用例在干净 Linux/macOS 上会真的跑而不是 skip** —— 本机只有 Windows，
   没法实测。CI 的 linux/macos job 是唯一证据来源。