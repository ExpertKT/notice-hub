# G1 · 架构

本文件只定义目标形状。G1 不建包、不改 import。

## 包名

新包名 `noticehub`。

领域是通知进入、去重、抽取、归档、待办和推送。QQ 只是其中一个入站适配器。沿用 `qq_live_digest` 会把上游仓库名留在每一条 import 上，也盖不住微信、钉钉、学习通已经在跑的事实。

`QQ-Notice-Hub.exe`、更新清单、`.env` 的 `QQ_DIGEST_*` 键名保持不变。包名和安装名、配置名是三件事。

## 分层

```
noticehub/domain          模型与规则。零 I/O，零框架
noticehub/ports           仓储、时钟、通知器、群历史抓取、规则集读取
noticehub/application     用例。只依赖 domain 与 ports
noticehub/adapters/qq     OneBot 与官方机器人事件归一成 MessageEvent
noticehub/infrastructure  ports 的实现：SQLite、HTTP、文件、进程
noticehub/bootstrap       Settings、from_legacy()、装配、日志、CLI
```

`qq_live_digest/webapp.py` 继续担任 Web 入站，直到主线在 G3 把它收成只做参数校验和用例调用。重写方不建 `noticehub/web`。

## 和上游不同构的理由

上游把解析、入库、摘要、推送写在同一条接收链上，配置用全局 `Settings`，存储是具体类 `Store`。

这里拆开的原因：

- 入站事件在适配器结束。去重、资格判断、建待办在 application，适配器不写库。
- 时间由注入的 `Clock` 给出。领域函数不调用 `now_local()`。
- Web 与 CLI 只拿 application 和 bootstrap 交出来的对象。它们不 import `infrastructure`，也不 import 今天的 `store`。
- 待办是独立聚合，摘要条目是另一类型。上游用同一个 dict 从消息流到任务行。

协议字段名（OneBot 的 `group_id`、`message_id`，任务状态 `candidate/open/done/dismissed/expired`）保持原词，因为它们是线上数据和外部协议，不是内部结构。

## 依赖规则

G2 落地后用 `import-linter` 断言。G1 不引入该依赖。

1. `domain` 不 import I/O 或框架（`sqlite3`、`http`、`pathlib` 读写、`os.environ`、Web、适配器）。
2. Web（今天的 `webapp` / `routes` / `settings_api` / `updates_api`）不直接 import `infrastructure` 或 `store`。
3. `application` 不 import `webapp`、`adapters`、`infrastructure`。
4. 模块 import 时不读环境变量，不取全局配置单例。配置只在 `bootstrap` 装配时构造，经参数进入用例。
5. 无循环依赖。

`service.py` 今天顶层 `from .webapp import TaskWebServer`，Web 再从 `self.server.*` 取运行时属性。这是属性级耦合，import 图上看不到环。G2 的 application 不再 import Web。谁启动 HTTP 线程留在 `bootstrap` / 现有 `main.py`，由主线在 G3 把回调改成注入。

## 这一轮不拆的东西

结构只动三处：Web 路由（主线）、配置注入（重写方定义类型，主线换读取点）、存储接口（重写方定义并实现，主线换调用点）。

微信、钉钉、学习通、NapCat、托管、打包、更新、安卓保持原模块。application 通过现有函数调用它们，不把它们收进新分层。

## 版本

程序常量 `APP_VERSION` / `APP_CHANNEL` 只写在 `version.py`。重命名提交可以把该模块挪到 `noticehub.version`，常量仍只写在那里。README、公告、更新清单里的版本文案保持与常量相同，不另做一套比较逻辑。`packaging/launcher.py` 的 import 由主线在同一次重命名里改。
