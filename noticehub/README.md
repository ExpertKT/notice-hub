# noticehub

核心 Python 包；`main.py`、网页服务和后台任务都从这里调用。

| 区域 | 文件/子包 | 作用 |
| --- | --- | --- |
| 顶层 | `service.py`、`webapp.py` | 组装组件、调度消息、提供 HTTP 与页面。 |
| 顶层 | `receiver.py`、`adapters/qq/` | 接收 OneBot 事件并转成 `MessageEvent`。 |
| 顶层 | `summarizer.py`、`rules.py`、`inbox.py` | 摘要、规则和收件箱分类。 |
| 顶层 | `push.py`、`infrastructure/notify.py` | 实现通道和投递去重/重试。 |
| 顶层 | `config.py`、`bootstrap/` | 读取环境配置并装配 CLI。 |
| 顶层 | `napcat_admin.py`、`hosting.py`、`wechat_receiver.py`、`chaoxing_receiver.py`、`dingtalk_receiver.py` | 管理 QQ/NapCat、微信、学习通、钉钉接入。 |
| 顶层 | `updater.py`、`updates_api.py`、`announce.py` | 更新、公告和下载状态。 |
| 子包 | `domain/`、`application/`、`ports/`、`infrastructure/`、`adapters/` | 领域规则、应用流程、协议、存储/外部实现和平台适配。 |
| 工具 | `attachments.py`、`browser_cookie.py`、`search.py`、`qr.py`、`ics.py` | 附件、浏览器 cookie、全文检索、二维码和日历输出。 |

常见改动：消息链查 `receiver.py` → `application/ingest.py` → `service.py`；接口改 `routes.py` 和 `webapp.py`；持久化改 `infrastructure/sqlite/store.py`。专项验证用 `.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_service tests.test_webapp`。
