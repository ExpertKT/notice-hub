# noticehub

核心 Python 包；`main.py`、网页服务、托盘启动器都从这里调用。

包根只有 `__init__.py`（一句说明，不导入任何东西）和本文件。代码按功能收在下面九个目录里，
另有一组更早的分层目录与之并存（见最后一节）。

## 功能子包

| 子包 | 放什么 | 典型文件 |
| --- | --- | --- |
| `web/` | HTTP 接口、页面模板与前端静态资源 | `webapp.py`（组装 Flask app、页面模板）、`routes.py`（/api 路由）、`settings_api.py`、`updates_api.py`、`announce.py`、`search.py`、`community.py`、`inbox.py`、`web_static/` |
| `receivers/` | 各平台消息接收端 | `receiver.py`（QQ/OneBot 事件转 `MessageEvent`）、`wechat_receiver.py`、`dingtalk_receiver.py`、`chaoxing_receiver.py`、`browser_cookie.py` |
| `qq/` | NapCat 接入、托管与配对 | `bot.py`、`napcat_admin.py`、`messages.py`、`pairing.py`、`qr.py`、`group_suggest.py` |
| `ingest/` | 消息进库前的抽取与归纳 | `event_extract.py`、`summarizer.py`、`rules.py`、`attachments.py`、`ics.py`、`catchup.py` |
| `delivery/` | 出站投递与调度 | `service.py`（组装组件、调度消息）、`push.py`（各推送通道） |
| `floatball/` | Windows 悬浮球 | `floatball_host.py`、`floatball_page.py`、`floatball_render.py`、`floatball_win.py`、`win_text.py` |
| `update/` | 版本检查、下载与就地更新 | `updater.py`、`update_sources.py`、`update_signing.py`、`doctor.py`、`selfcheck.py` |
| `remote/` | 公网通道与托管动作 | `tunnel.py`、`cloudflare.py`、`hosting.py`、`host_actions.py` |
| `platform/` | 配置、日志、重试、版本号这类底座 | `config.py`、`logging_setup.py`、`retry.py`、`version.py`、`timeutil.py`、`ttl_cache.py`、`llm_json.py`、`lab.py`、`config_audit.py` |

## 更早的分层目录（仍在用）

`domain/`（实体与规则）、`application/`（用例：任务、日程、订阅、历史）、`ports/`（协议）、
`infrastructure/`（SQLite 存储与通知实现）、`adapters/qq/`（OneBot 适配）、`bootstrap/`（CLI 装配）。
它们与上面的功能子包互相 import，比如 `delivery/service.py` 用 `application/` 的用例、`infrastructure/sqlite/store.py` 落库。

这两套组织方式并存是迁移到一半的现状：同一件事在两边都有入口时，以实际 import 链为准——
消息链是 `receivers/receiver.py` → `ingest/` → `application/` → `infrastructure/sqlite/`，
HTTP 是 `web/routes.py` → `web/webapp.py` → `application/`。收敛成一套要单独一轮做。

## 常见改动

- 加一个接口：`web/routes.py` 加路由，逻辑落 `application/`，别把业务写进路由。
- 支持一个新平台：`receivers/` 下加接收端，产出同样的 `MessageEvent`。
- 改库表：`infrastructure/sqlite/store.py` 与 `deliveries.py`，同时看 `tests/test_store.py`。
- 改页面：`web/web_static/page.html` 与 `web/web_static/js/`，跑 `tests/test_ui_browser.py`（要 `NH_REQUIRE_BROWSER=1`）。

专项验证：`.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_service tests.test_webapp`。
