# tests

这里放 Python 单元、接口、脚本契约和 Edge 浏览器回归；改代码的人按受影响模块跑专项测试。

| 规则 | 说明 |
| --- | --- |
| `test_<模块>.py` | 通常覆盖同名模块，例如 `test_push.py` 测通道发送，`test_store.py` 测 SQLite 存取，`test_receiver.py` 测 OneBot HTTP。 |
| `test_application_*.py` | 测应用层待办、日历、通知流程。 |
| `test_*_receiver.py` | 测微信、钉钉和 OneBot 接收及解析。 |
| `test_webapp.py`、`test_routes*.py` | 测页面结构、HTTP 路由、鉴权和边界。 |
| `test_ui_browser.py` | 用 Edge 测真实页面、响应式布局、弹层和交互。 |
| `test_launcher*.py`、`test_install_task.py`、`test_watchdog_task.py` | 测启动器、计划任务和看门狗静态契约。 |
| `test_update*.py`、`test_updater.py` | 测更新来源、验签、下载、应用、补丁和回滚。 |
| `test_floatball*.py` | 测浮球绘制、窗口几何、动画和命中。 |
| `test_*signing*.py`、`test_verify_manifest.py`、`test_version_bump.py` | 测发布清单和版本工具。 |
| `fixtures/`、`qr_golden.json` | 测试输入和二维码黄金数据。 |
| 例外 | `test_acceptance_gate.py` 测总门禁；`test_no_duplicates.py` 测职责去重；`test_perf_baseline.py` 测性能护栏；`test_temp_hygiene.py` 测临时目录清理。 |

常见改动：Python 改动先设 `TEMP/TMP=F:\qq-notice-hub\nh-tmp`，用 `.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_<模块>` 连跑两次；浏览器测试还要设 `NH_REQUIRE_BROWSER=1`，全 skipped 不算通过。
