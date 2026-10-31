# 批次 A 行为规格（clean-room 重写输入）

本文件只描述**行为**。实现者不需要、也不应该去看旧代码；照这里写出来的东西必须能通过现有测试，
并且与现有对外行为一致。凡是现有测试没锁死、又会影响兼容性的地方，集中列在 §8「需要拍板」，
实现前先按那里的结论做；没有结论的按本文正文写。

适用范围（6 个模块，包名 `noticehub`）：

| 模块 | 职责一句话 |
|---|---|
| `noticehub/timeutil.py` | 本地 naive 时间门面：现在时间、ISO 字符串互转 |
| `noticehub/weburl.py` | 生成用户可点的网页地址（局域网 IP 探测 + 令牌 + 任务锚点） |
| `noticehub/retry.py` | 外部调用的有限次重试与「哪些错值得重试」的判定 |
| `noticehub/logging_setup.py` | 进程日志：控制台 + 按大小轮转的文件 |
| `noticehub/bot.py` | 官方 QQ 机器人常驻客户端：生命周期、群消息/私聊转记录、线程内运行与停止 |
| `noticehub/catchup.py` | NapCat(OneBot) HTTP 客户端 + 历史补采公开面（编排实现可以放在别处，但必须能从本模块取到） |

---

## 0. 通用约定

**0.1 时间。** 系统内所有 `datetime` 都是**本地时区的 naive**（`tzinfo is None`），精度到**秒**（微秒一律截断为 0）。任何地方都不产出 aware datetime；读入 aware 值时先转本地再去掉 tzinfo。

**0.2 异常。** 模块自定义异常一律继承 `RuntimeError`，`str(异常)` 就是给用户/日志看的完整中文句子，不得只给代号。

**0.3 日志。** 用标准库 `logging`，每个模块用自己的模块名做 logger（`logging.getLogger(__name__)`）。个别模块有例外的固定频道名，见各自小节。日志文本属于**运维契约**（有人按文本 grep 排障），改文案要单独评审。

**0.4 配置。** 配置对象（`Settings`）由外部注入，模块自己不去读环境变量。批次 A 用到的配置键与默认值：

| 配置键 | 类型/默认 | 被谁用 |
|---|---|---|
| `web_host` | 字符串，Windows 上默认 `0.0.0.0`，其它系统默认 `127.0.0.1` | `weburl` |
| `web_port` | 整数，默认 `8766` | `weburl` |
| `web_base_url` | 字符串，默认空 | `weburl` |
| `group_whitelist` | 字符串元组，默认空 | `catchup`（白名单语义 fail-closed：空 = 什么都不收） |
| `group_aliases` | 群号 → 显示名的映射，默认空 | `catchup` |
| `catchup_enabled` | 布尔，默认 `False` | `catchup` |
| `catchup_hours` | 整数，默认 `24` | `catchup` |
| `catchup_count` | 整数，默认 `50` | `catchup` |
| `napcat_api_url` | 字符串，默认 `http://127.0.0.1:3001` | `catchup` |
| `napcat_api_token` | 字符串，默认空 | `catchup` |
| `http_timeout` | 整数秒，默认 `15` | `catchup` |
| `llm_max_retries` | 整数，默认 `2` | `retry` 的调用方 |
| `llm_retry_backoff` | 浮点秒，默认 `1.5` | `retry` 的调用方 |
| `appid` / `secret` | 字符串，默认空（环境键 `QQ_BOT_APPID` / `QQ_BOT_SECRET`） | `bot` |
| `sandbox` | 布尔，默认 `False` | `bot` |
| `data_dir` / `log_dir` | 路径 | `bot` / `logging_setup` |

**0.5 不做的事。** 这 6 个模块都不直接写数据库、不读环境变量、不弹窗、不启动子进程。

---

## 1. `noticehub/timeutil.py`

### 1.1 公开面

模块 `__all__` 恰好是这四个名字：`iso`、`now_local`、`parse_iso`、`to_naive_local`。前三个转换函数是**再导出**（实现可以放在别的模块，但必须能从 `noticehub.timeutil` 导入到同样的对象）。

签名：

- `now_local() -> datetime`
- `to_naive_local(value: Any) -> datetime | None`
- `iso(value: Any) -> str`
- `parse_iso(text: Any) -> datetime | None`

### 1.2 契约条目

- **T-01** `now_local()` 返回「当前本地墙钟时间」，naive，秒精度。它是本模块**唯一**读时钟的函数；其余三个必须是纯函数，连时钟都不许读。
- **T-02** `to_naive_local` 接受 `None`、`bool`、`int`、`float`、`str`、`datetime`，其它类型一律返回 `None`（不抛异常）。`bool` 要先于整数判断，`True`/`False` 都必须返回 `None`。
- **T-03** 整数/浮点入参按**Unix 秒**解释：`<= 0` 返回 `None`；`> 10_000_000_000` 视为**毫秒**，先除以 1000；时间戳换算失败（溢出、越界、非法浮点如 NaN）返回 `None`。恰好等于 `10_000_000_000` 时仍按秒处理（比较是严格大于）。
- **T-04** 字符串入参先 `strip()`；空串返回 `None`。先按 ISO 8601 解析，其中大写 `Z` 视为 `+00:00`；ISO 解析失败再尝试按浮点 Unix 秒解析；两次都失败返回 `None`。
- **T-05** `datetime` 入参：aware 的先转本地时区、去掉 tzinfo；naive 的原样保留（不做时区假设）。
- **T-06** 任何成功路径返回前都把 `microsecond` 截断为 0，且结果 `tzinfo is None`。
- **T-07** `iso(value)`：转换失败（即 `to_naive_local` 返回 `None`）时返回**空串** `""`，绝不返回 `"None"`；成功时返回 `isoformat(timespec="seconds")` 形式，形如 `2026-09-29T01:00:00`，**没有**时区后缀、没有小数秒。
- **T-08** `parse_iso(text)` 与 `to_naive_local` 行为完全等价（同一实现即可），入参名可以不同，但语义必须是「字符串→datetime|None」，且不得比 `to_naive_local` 更严格（数字也能吃）。
- **T-09** 三个转换函数都是纯函数：同一入参调用任意多次结果相同，无副作用、无 I/O、无全局状态。
- **T-10** 边界表（入参 → 返回）：

| 入参 | 返回 |
|---|---|
| `None`、`True`、`False`、`[]`、`{}`、`object()` | `None` |
| `0`、`-1`、`0.0` | `None` |
| `1700000000` | 该时间戳对应的本地时间 |
| `1700000000000`（毫秒） | 同 `1700000000` |
| `float("nan")`、`float("inf")` | `None` |
| `""`、`"   "`、`"not-a-time"` | `None` |
| `"2026-09-29T01:00:00Z"` | 本地时区下对应的 naive 时间 |
| `"2026-09-29T01:00:00.123456"` | 微秒被截断 |
| `"1700000000"` | 按 Unix 秒解析 |
| `iso(None)` | `""` |

### 1.3 外部依赖

只用标准库 `datetime` 与系统时钟。无网络、无文件、无数据库。时区来自进程所在机器的本地时区设置——同一份数据在不同时区的机器上换算出的字符串会不同，这是**预期行为**，不是 bug。

### 1.4 测试锁住的事实

本模块没有独立单测；但 `iso` / `now_local` 被 `test_store.py`、`test_service.py`、`test_summarizer.py`、`test_tasks_latency.py`、`test_webapp.py`、`test_push.py`、`test_application_tasks.py`、`test_wechat_integration.py` 当作**造数与比对**的工具，因此：

- `iso(...)` 的输出格式必须能原样写进数据库再读回来做字符串相等比较（秒精度、无时区后缀）。
- `now_local()` 必须能被直接拿去做「当前时间 - N 小时」的算术，且不因 aware/naive 混用抛错。

---

## 2. `noticehub/weburl.py`

### 2.1 公开面

- `detect_lan_ip() -> str`
- `build_web_url(settings, *, token: str = "", task_id: int | None = None, base_url: str = "") -> str`

模块没有 `__all__` 约束，但除这两个之外不应新增公开函数。

### 2.2 契约条目

- **U-01** `detect_lan_ip()` 返回「本机在默认出口上的局域网地址」。实现方式：建一个 UDP socket、`connect` 到 `223.5.5.5:80`、取本端地址；**不发送任何数据**、不建 TCP 连接；无论成功失败都必须关闭 socket。
- **U-02** 探测失败（任何 `OSError`，含无路由、被策略拦、断网）时返回 `"127.0.0.1"`，**不抛异常**、不重试、不写日志。这是离线机器必须能走通的路径。
- **U-03** `build_web_url` 选择基址的顺序：`base_url` 参数（非空时）→ `settings.web_base_url`；两者都为空则按 `http://{host}:{port}` 现拼，其中 host 取 `settings.web_host`，为空字符串时退化为 `"127.0.0.1"`。
- **U-04** 选定的基址先 `strip()` 再去掉**尾部**所有 `/`；如果它本身带路径（例如反向代理前缀），路径必须原样保留，不得解析重写。
- **U-05** 当 host 属于 `{"0.0.0.0", "::", ""}` 之一时，用 `detect_lan_ip()` 的返回值替换它——用户点开 `0.0.0.0` 是打不开的。
- **U-06** 端口直接取 `settings.web_port` 的字符串形式，不做范围校验。
- **U-07** 结果永远是「基址 + 一个 `/`」，即尾部一定带斜杠。
- **U-08** `token` 非空时追加查询串 `?token=<百分号编码后的 token>`，用标准 `urlencode`（空格编码为 `+`）编码；`token` 为空串时不出现 `?`。
- **U-09** `task_id is not None` 时追加片段 `#task-{int(task_id)}`。注意是**先判断非 None**，所以 `task_id=0` 也要追加（`#task-0`）；传非数字会抛 `ValueError`（实现者不要吞掉它，这是调用方 bug）。
- **U-10** 片段在查询串之后：`...?token=abc#task-7`。
- **U-11** 组合表：

| 场景 | 结果 |
|---|---|
| 无 base、host=`0.0.0.0`、port=8766、无 token/任务 | `http://<探测到的局域网IP>:8766/` |
| `base_url="https://nh.example.com/"` | `https://nh.example.com/` |
| `base_url` 带子路径 `https://x/nh/` | `https://x/nh/` |
| token=`a b&c` | 末尾 `?token=a+b%26c` |
| token 空、task_id=3 | `...#task-3` |
| token 非空、task_id=3 | `...?token=...\#task-3` |

### 2.3 外部依赖

一次 UDP `connect`（不产生流量）用于探测本机地址；其余全是字符串处理。无文件、无数据库。

### 2.4 测试锁住的事实

**没有任何测试覆盖这两个函数**（全仓库无引用）。它们的行为契约来自调用方：`noticehub/service.py` 用它拼「提醒里那条可点链接」，`noticehub/bootstrap/cli.py` 用它打印启动地址。见 §8 的 D-04。

---

## 3. `noticehub/retry.py`

### 3.1 公开面

- 常量 `TRANSIENT_HTTP_CODES`：值为 `{408, 409, 425, 429, 500, 502, 503, 504}` 的不可变集合
- 异常：`LLMError(RuntimeError)`、`LLMRequestError(LLMError)`（带 `.status: int`）、`LLMResponseError(LLMError)`
- `is_transient_http(status: int) -> bool`
- `llm_should_retry(error: BaseException) -> bool`
- `call_with_retries(func, *, retries: int = 2, backoff: float = 1.5, should_retry=None, logger=None, label: str = "外部调用")`

### 3.2 契约条目

- **R-01** `TRANSIENT_HTTP_CODES` 必须支持 `in` 判定，元素是整数状态码，集合内容**原样**为上面八个；`is_transient_http` 先把入参转 `int` 再查集合（`"429"` 也算真）。
- **R-02** `LLMRequestError(message, *, status: int = 0)`：`str(e) == message`，`e.status` 是整数，缺省 0 表示「网络/超时，没有 HTTP 状态码」。
- **R-03** 三个异常的关系：`LLMRequestError` 与 `LLMResponseError` 都是 `LLMError` 的子类，`LLMError` 是 `RuntimeError` 的子类。捕获 `LLMError` 必须能抓到前两者。
- **R-04** `llm_should_retry` 判定表（这是行为规格，不是实现要求）：

| 输入 | 结果 |
|---|---|
| `urllib.error.HTTPError`，code 在八码集合内 | 真 |
| `urllib.error.HTTPError`，code 不在集合内（如 400/401/403/404） | 假 |
| `LLMRequestError`，`status == 0` | 真 |
| `LLMRequestError`，`status` 在集合内 | 真 |
| `LLMRequestError`，`status` 不在集合内 | 假 |
| `LLMResponseError` | 真 |
| `urllib.error.URLError`、`TimeoutError`、`ConnectionError`、`OSError`、`json.JSONDecodeError` | 真 |
| 其它任何异常（`ValueError`、`RuntimeError`、`KeyError`…） | 假 |

注意 `HTTPError`、`URLError` 都是 `OSError` 子类，判定顺序必须让「HTTP 状态码」先于「OSError 一律真」，否则 401 会被误判成可重试。

- **R-05** `call_with_retries` 的总尝试次数是 `max(1, int(retries) + 1)`：`retries=0` → 1 次；`retries=2` → 最多 3 次；负数 → 1 次。
- **R-06** 第 n 次重试（n 从 1 起）之前 sleeps `max(0.0, float(backoff)) * 2^(n-1)` 秒：即 `retries=2, backoff=1.5` 时依次 sleep 1.5、3.0。算出来的延迟为 0 时**不调用 sleep**（避免无意义调用，测试里 `sleep` 的调用次数是断言对象）。
- **R-07** `should_retry` 为 `None` 时默认**任何异常都可重试**；传入时只有返回真才重试。
- **R-08** 抛出规则：最后一次尝试仍失败 → **原样重新抛出最后一次的那个异常**（不得包装成 `LLMError`，测试断言的是原异常类型）；`should_retry` 判假 → 立刻原样抛出，**不再 sleep、不再尝试**。
- **R-09** 只捕获 `Exception`。`KeyboardInterrupt`、`SystemExit` 等必须直接穿透。
- **R-10** 每次「准备重试」时写一条 WARNING 日志，格式原样为：`{label}第 {第几次}/{总次数} 次失败，准备重试：{异常文本}`，其中「第几次」是**刚失败的这次**（从 1 起）。logger 取 `logger` 参数，未给时用模块 logger。
- **R-11** 成功时返回 `func()` 的原值，不做任何转换。`func` 在本函数内最多被调用 `总尝试次数` 次，且只在同一线程内串行调用。
- **R-12** 调用方契约：LLM/视觉相关调用方传 `retries=settings.llm_max_retries`、`backoff=settings.llm_retry_backoff`（默认 2 / 1.5）、`should_retry=llm_should_retry`、`label="视觉/文档模型"`；调用方捕获 `LLMError` 后转成自己的错误类型。实现者不要把这套参数烤死在本模块里。

### 3.3 外部依赖

只用标准库：`time.sleep`（唯一副作用）、`logging`、`json`、`urllib.error`。无网络请求本身——重试的是调用方传进来的函数。

### 3.4 测试锁住的事实

来自 `tests/test_retry.py`：

1. `retries=3`、`backoff=0`、前两次抛 `LLMRequestError(status=429)`、第三次返回 `"ok"` → 返回值是 `"ok"`，`func` 被调用 **3 次**。
2. 一直抛 `LLMRequestError(status=401)`、`retries=3` → 抛出 `LLMRequestError`，`func` 只被调用 **1 次**（鉴权失败不重试）。
3. 一直抛 `LLMResponseError`、`retries=2` → 抛出的类型必须是 `LLMResponseError`（不是 `LLMError`、不是包装后的新异常）。
4. `is_transient_http(429)`、`(503)` 为真，`(401)` 为假。
5. `HTTPError(code=503)` 可重试；`HTTPError(code=403)` 不可重试；`json.JSONDecodeError` 可重试。
6. `retries=2, backoff=1.5`、第一次失败第二次成功 → `time.sleep` **恰好被调用 1 次**，参数**恰好 1.5**（因此首次重试的延迟必须等于 `backoff` 本身，不能是 `backoff*2`）。

---

## 4. `noticehub/logging_setup.py`

### 4.1 公开面

- 常量 `FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"`
- `setup_logging(log_dir: Path, *, level: int = logging.INFO, console: bool = True) -> logging.Logger`

### 4.2 契约条目

- **L-01** 先确保 `log_dir` 存在（含父目录），已存在不报错。
- **L-02** 目标 logger 是**根 logger**（`logging.getLogger()`）：设置它到 `level`，并返回它。
- **L-03** 幂等：函数开头移除根 logger 上**现有全部** handler，并逐个 `close()`；关闭时的异常一律吞掉（旧 handler 可能已坏）。重复调用后根 logger 上**恰好一个**文件 handler（`console=False` 时）。
- **L-04** 文件 handler 用「按大小轮转」：
  - 目标文件固定在 `log_dir` 下，文件名是 `qq-live-digest.log`（**带连字符的产品名**，见 D-01）；
  - `maxBytes = 5 * 1024 * 1024`；
  - `backupCount = 5`；
  - `encoding="utf-8"`。
- **L-05** 控制台 handler（`console=True` 时）：先尝试把 `sys.stdout` 重新配置成 UTF-8、`errors="replace"`（不支持或无 `reconfigure` 时静默跳过），再加一个写 `sys.stdout` 的 `StreamHandler`。目的：中文 Windows 上不能因为输出编码崩掉。
- **L-06** 文件与流两个 handler 都用 `FORMAT` 格式化；时间用 `asctime` 默认格式。
- **L-07** 把 `botpy` 这个 logger 的级别设为 `WARNING`（第三方机器人库话太多）。这是**跨模块副作用**，必须保留原位、不可放进条件分支。
- **L-08** 日志频道名（运维契约）：本进程所有应用日志走根 logger；`noticehub.*` 是模块 logger；第三方 `botpy` 被压到 WARNING。`noticehub.catchup` 这个名字由 `catchup` 的编排层固定使用（见 §6）。
- **L-09** 轮转行为：单个文件超过 5 MiB 时改名成 `.1`，最多保留 5 份历史（`.1`…`.5`），当前文件重新从空开始。轮转由 handler 自己完成，本函数不插手。
- **L-10** 本函数是「进程级单例式初始化」：不返回 handler、不暴露配置对象，只保证调用后根 logger 状态正确。
- **L-11** 不得在模块导入时产生副作用（不建目录、不加 handler）；一切只发生在调用时。
- **L-12** 并发：函数内部不加锁，调用方负责在启动早期调用一次；运行期再次调用会把已有日志句柄关掉，属于调用方必须避免的行为。

### 4.3 外部依赖

文件系统（创建目录、写日志文件、轮转改名）、`sys.stdout`。无网络、无数据库。轮转依赖「同一时刻只有一个进程写这个文件」——服务进程写、启动器只读它的尾部，这是既定部署形态（见 D-02）。

### 4.4 测试锁住的事实

来自 `tests/test_logging_setup.py`：

1. `console=False` 时根 logger 上**恰好 1 个** `RotatingFileHandler`；其 `maxBytes == 5*1024*1024`、`backupCount == 5`、文件名是 `qq-live-digest.log`。
2. 写一条 INFO 后 flush，文件里能读到那条文本（UTF-8）。
3. 连续写入约 6 MiB 后必须生成 `.1` 备份，且当前文件大小 < `maxBytes`。
4. 连续调用两次 `setup_logging`：第一次的 handler 被关闭（其 `.stream` 为 `None`），根 logger 上仍只有 1 个文件 handler，且是**新对象**（`is not` 旧对象）。

---

## 5. `noticehub/bot.py`

### 5.1 公开面

- 类型别名：`RecordCallback = Callable[[dict], Any]`、`OpenIdCallback = Callable[[dict], Any]`
- `botpy_version() -> str`
- `install_full_group_message_parser() -> bool`
- `class LiveBotClient(第三方 Client 的子类)`：`__init__(*args, on_message, on_openid=None, logger=None, **kwargs)`；属性 `ready: threading.Event`、`logger`；异步方法 `on_ready`、`on_error`、`on_group_message_create`、`on_group_at_message_create`、`on_c2c_message_create`；内部 `_emit`。
- `class BotRunner`：`__init__(settings, on_message, *, data_dir=None, logger=None)`；属性 `client`、`loop`、`thread`、`stopped`、`last_error`；只读属性 `api`、`is_alive`、`is_ready`；方法 `start()->bool`、`wait_ready(timeout: float = 30)->bool`、`stop(timeout: float = 10)->None`。

### 5.2 契约条目

- **B-01** 模块导入时若缺少第三方机器人库（`botpy`），必须以 `SystemExit` 终止，消息原样为：`缺少 qq-botpy，请先执行: pip install -r requirements.txt`。不得改成 `ImportError` 或延迟到运行期。
- **B-02** `botpy_version()` 返回已安装发行包的版本字符串；查不到包时返回 `"unknown"`，不抛异常。
- **B-03** `install_full_group_message_parser()`：若连接状态类**已有**解析方法，直接返回 `False`（不重复打补丁、不覆盖别人的实现）；否则补一个「解析群消息创建事件」的方法并返回 `True`。补丁的语义是：用事件负载里的 `id` 与 `d` 构造群消息对象，然后以事件名 `group_message_create` 派发。
- **B-04** `LiveBotClient` 构造：`on_message` 是必填关键字参数；`on_openid` 缺省 `None`；`logger` 缺省用模块 logger；额外位置/关键字参数原样透传给父类。`ready` 初始为「未置位」。
- **B-05** `on_ready`：从连接状态里取机器人用户名（取不到用 `"unknown"`），写一条 INFO 日志 `QQ 机器人已连接：{用户名}`，然后置位 `ready`。`ready` 一旦置位不再清除。
- **B-06** `on_error(event_method, *args, **kwargs)`：用 ERROR 级 + 异常堆栈记录 `QQ 机器人事件异常：{事件方法名}`，**不得**让异常冒泡。
- **B-07** 群消息两条入口（`GROUP_MESSAGE_CREATE` 与 `GROUP_AT_MESSAGE_CREATE`）都转成同一形状的记录并交给 `on_message`：
  - `msg_id`：消息 `id` 的字符串（可能为空串，不补造）
  - `source`：`"qqbot"`
  - `event`：`"GROUP_MESSAGE_CREATE"` 或 `"GROUP_AT_MESSAGE_CREATE"`（原样透传区分）
  - `group_id`：群 `group_openid`
  - `sender_id`：作者 `member_openid`
  - `sender_name`：作者 `username`
  - `ts`：消息时间戳（秒精度 ISO 串）；无时间戳时用当前时间
  - `received_at`：当前时间（秒精度 ISO 串）
  - `content`：消息文本
  - **没有** `group_name` 键（官方机器人路径拿不到群名）
- **B-08** `on_message` 抛异常时：用 ERROR 级 + 堆栈记录 `处理群消息回调失败：{msg_id}`，然后**吞掉**——回调出错绝不能断开机器人连接。
- **B-09** 私聊入口：构造记录 `{msg_id, user_openid, ts, content}`（`ts` 取消息时间，缺失用当前时间），写一条 INFO 日志 `收到私聊，user_openid={openid}（可写入 QQ_DIGEST_PUSH_OPENIDS）`；`on_openid` 非空时调用它，回调异常用 ERROR 级 + 堆栈记录 `处理私聊回调失败` 并吞掉。
- **B-10** `BotRunner.start()`：已在线程中运行时返回 `False`（不重复启动）；`appid` 或 `secret` 任一为空时，把 `last_error` 设为 `缺少 AppID/AppSecret`、写一条 ERROR 日志 `未配置 QQ_BOT_APPID/QQ_BOT_SECRET，无法启动官方机器人。`、返回 `False`；否则清掉「已停止」事件、起一个名为 `qq-bot` 的**守护线程**并返回 `True`。`start()` 必须立即返回，不等连接建立。
- **B-11** 线程内：新建独立事件循环并设为当前循环；先尝试打群消息解析补丁；意图只开 `public_messages`；用 `timeout=10`、`is_sandbox=settings.sandbox` 构造客户端；启动日志原样为 `启动 QQ 官方机器人 appid={前4位}*** sandbox={布尔} full_group_parser={布尔} botpy={版本}`。
- **B-12** 线程退出：任何异常（含 `BaseException`）都要把 `str(error)` 存进 `last_error`、用 ERROR 级 + 堆栈记录 `QQ 机器人线程退出：{error}`；`finally` 里取消该循环所有未完成任务、关闭循环（各自异常吞掉）、置位「已停止」事件、写 INFO `QQ 机器人线程已退出。`。线程函数**不得**向外抛异常。
- **B-13** `_save_openid`（作为 `on_openid` 传入）：把记录以 JSON 行（UTF-8、不转义中文、行尾一个换行）追加到 `data_dir/c2c_openids.jsonl`，父目录不存在时创建。这是**唯一**的私聊凭据落地方式（见 D-06）。
- **B-14** `wait_ready(timeout=30) -> bool`：客户端还没建好时返回 `False`；否则等 `ready` 事件，超时返回 `False`。
- **B-15** `stop(timeout=10)`：线程不在时直接返回（幂等，不抛错）。在线程时：把「关闭客户端」协程投递到该事件循环并最多等 3 秒（失败吞掉）→ 请求循环停止 → `join(timeout)` → 写 INFO `QQ 机器人已停止。`。`stop()` 之后 `is_alive` 应为假（正常路径），`client`/`loop` 不清空。
- **B-16** 属性语义：`api` 在无客户端时是 `None`；`is_alive` 只看线程是否存活（不代表连接成功）；`is_ready` 要求「有客户端且 ready 事件已置位」。两个判断不能互相替代——调度层用 `is_ready` 判断「引擎上线后补采」。
- **B-17** 回调执行线程：所有 `on_message`/`on_openid` 都在机器人自己的事件循环线程里同步调用；调用方不得假设它们在主线程。回调是**串行**调用的（同一线程）。
- **B-18** 重启语义：`stop()` 之后再 `start()` 允许，`stopped` 事件会重新清零；但同一时刻只允许一个线程（`is_alive` 为真时 `start()` 返回 `False`）。
- **B-19** 对外契约（不可改）：环境变量键 `QQ_BOT_APPID`、`QQ_BOT_SECRET`；环境键 `QQ_DIGEST_PUSH_OPENIDS` 是私聊推送白名单（由配置层解析），`B-09` 的提示文案里必须出现这个键名；记录里的键名（`msg_id`/`source`/`event`/`group_id`/`sender_id`/`sender_name`/`ts`/`received_at`/`content`/`user_openid`）是下游入库契约。
- **B-20** 幂等/时序：`_emit` 与私聊回调都不能因为回调异常影响后续消息处理；`ready` 置位之后 `wait_ready` 立即返回 `True`。

### 5.3 外部依赖

第三方 QQ 机器人库（发行包名 `qq-botpy`，连接超时 10 秒）、异步事件循环、线程、文件系统（`c2c_openids.jsonl`）。网络由第三方库负责，本模块不自己发 HTTP。

### 5.4 测试锁住的事实

**本模块没有测试**（全仓库无引用）。所有契约靠本文与调用方（`noticehub/application/schedule.py` 用 `is_ready` 判断补采时机；`noticehub/service.py` 负责启停）维持。见 §8 的 D-05、D-06。

---

## 6. `noticehub/catchup.py`

### 6.1 公开面

`__all__` 恰好六个名字：`NapCatError`、`NapCatClient`、`history_to_record`、`backfill`、`available_floor`、`backfill_range`。

后四个函数的**实现可以放在别的模块**（编排层），但必须能从 `noticehub.catchup` 导入到同名对象——现有调用方与测试都从本模块取。

- `NapCatClient(base_url: str, token: str = "", timeout: int = 15)`
  - `call(action: str, payload: dict | None = None) -> dict`
  - `group_history(group_id: str, count: int, message_seq: int | None = None) -> list[dict]`
  - `login_info() -> dict`
- `history_to_record(message: dict, settings, *, received_at=None, allowed_groups: set[str] | None = None) -> dict | None`
- `backfill(settings, store, logger=None, *, client=None, now=None, force=False, stats=None, attachment_sink=None) -> int`
- `available_floor(settings, group_id, *, client=None, page: int = 1000, max_pages: int = 20) -> dict`
- `backfill_range(settings, store, *, groups=None, since=None, until=None, max_messages: int = 20000, client=None, progress=None, cancel=None) -> dict`

### 6.2 `NapCatClient` 契约

- **C-01** 构造：`base_url` 转字符串后去掉尾部 `/`；`token` 转字符串；`timeout` 取 `max(5, int(timeout or 15))`——**下限 5 秒**，传 0/None 用 15。
- **C-02** `call` 在 `base_url` 为空时抛 `NapCatError("NapCat API 地址为空")`，不发请求。
- **C-03** 请求形状：`POST {base}/{action}`（`action` 前导 `/` 被吃掉），正文是 JSON（UTF-8、不转义非 ASCII），请求头 `Content-Type: application/json`；`token` 非空时加 `Authorization: Bearer {token}`，为空时**不加**这个头。
- **C-04** 超时：单次请求用实例的 `timeout` 秒，不做重试（本客户端**不重试**；重试策略由调用方决定）。
- **C-05** 响应处理：读完正文（UTF-8、非法字节替换）后解析 JSON。`retcode` 缺省视为 0；非 0 视为失败，错误明细取 `message` → `wording` → 整个响应对象依次回退。成功返回**完整响应对象**（不是只返回 `data`）。
- **C-06** 错误映射与**用户可见文案**（原样，含动作名）：
  - HTTP 层失败：`{action} HTTP {状态码}: {响应体前 200 字符}`
  - 网络/超时/OS 层失败：`{action} 请求失败: {原始异常}`
  - 正文不是 JSON：`{action} 返回不是 JSON: {正文前 200 字符}`
  - `retcode` 非 0：`{action} 返回错误: {明细}`
  - 以上全部抛 `NapCatError`，并以 `raise ... from` 保留原始异常。
- **C-07** `group_history`：动作名 `get_group_msg_history`；负载 `{"group_id": 字符串, "count": 整数}`，`message_seq` 非 `None` 时加 `{"message_seq": 整数}`；返回 `data.messages` 中**是字典**的条目（非字典丢弃），`data`/`messages` 缺失或为 `None` 时返回空列表。不做分页、不排序。
- **C-08** `login_info`：动作名 `get_login_info`；返回 `data`（是字典才返回，否则空字典）。失败按 `C-06` 抛。

### 6.3 `history_to_record` 契约

- **C-09** 入参是 NapCat 原始消息字典。按顺序过滤，任一条不满足都返回 `None`（不抛异常）：
  1. `post_type` 缺省按 `"message"`，不等于它就丢；
  2. `message_type` 缺省按 `"group"`，不等于它就丢；
  3. `group_id` 为空则丢；`allowed_groups` 为 `None` 时要求 `settings.accepts_group(group_id)` 为真（白名单 fail-closed），非 `None` 时要求群号在该集合内；
  4. `user_id` 与 `self_id` 都非空且相等（自己发的）则丢；
  5. 正文为空则丢：先从消息段数组取正文，仍为空再试 `raw_message`；
  6. `message_id` 与 `real_id` 都为空则丢。
- **C-10** 正文扁平化规则（与实时上报保持一致）：字符串原样 `strip()`；段数组按序拼接——`text` 取 `data.text`；`at` 拼 `@{name 或 qq 或 "某人"}`；`reply` 整段跳过；`file` 拼 `[文件]` 加文件名（无名字只 `[文件]`）；其余类型映射固定标签：图片 `[图片]`、表情（`face`/`mface`）`[表情]`、语音 `[语音]`、视频 `[视频]`、合并转发 `[合并转发]`、卡片（`json`）`[卡片消息]`、文件之外的未知类型 `[{类型}]`、骰子 `[骰子]`、猜拳 `[猜拳]`；非字典段用字符串化兜底。拼完整体 `strip()`。
- **C-11** 产出记录（键集合固定，缺键会导致入库失败）：
  - `msg_id` = `onebot:{group_id}:{message_id}`
  - `source` = `"onebot-catchup"`（与实时上报的 `"onebot"` 区分）
  - `event` = `"GROUP_MESSAGE_CREATE"`
  - `group_id`、`content`
  - `ts` = 消息时间（秒精度 ISO）；无时间用当前时间
  - `received_at` = `received_at` 参数 → 消息时间 → 当前时间，依次回退（秒精度 ISO）
  - `sender_id` = `user_id`
  - `sender_name` = `sender.card` → `sender.nickname` → `user_id` → `"群成员"`
  - `group_name` = `settings.group_name(group_id)`（别名 → 群号 → `"未知群"`）
- **C-12** 本函数是纯的：不写库、不发请求、不读时钟以外的东西（当前时间仅作兜底）。

### 6.4 `backfill` 契约

- **C-13** 前置短路：`force` 为假且 `settings.catchup_enabled` 为假 → 直接返回 0（不建客户端、不发请求）；白名单解析后为空 → WARNING `历史补采未执行：群白名单为空。` 并返回 0。
- **C-14** 窗口：`cutoff = 基准时间 - max(1, catchup_hours) 小时`（`catchup_hours` 至少按 1 小时算）。基准时间取 `now` 参数，缺省用当前时间。
- **C-15** 客户端来源：`client` 参数非空则用它（便于测试注入）；否则用 `napcat_api_url` + `napcat_api_token` + `http_timeout` 现建一个 `NapCatClient`。
- **C-16** 登录信息失败**不致命**：写 WARNING `历史补采：读取登录信息失败：{错误}`，按「拿不到 self_id」继续（此时不会过滤自己发的消息，靠入库去重与白名单兜底）。
- **C-17** 逐群处理：单群取历史失败只记 WARNING `历史补采失败 group={群号}：{错误}` 并继续下一个群（`failed_groups` +1）；成功则 `ok_groups` +1。消息按时间**升序**处理；早于 `cutoff` 的跳过；`user_id` 等于 self_id 的跳过；`history_to_record` 返回 `None` 的跳过。
- **C-18** 入库走「按键去重」：同一条消息第二次补采不新增（依赖存储层对 `msg_id` 的唯一约束，返回布尔表示是否真的插入）；只对**新插入**的消息调用 `attachment_sink`，附件入队失败只记 ERROR 级堆栈 `补采附件入队失败：{msg_id}`，不影响计数。
- **C-19** 统计字典（`stats` 非 `None` 时被**更新**，不是替换）：`{"groups": 白名单群数, "ok_groups": ..., "failed_groups": ..., "inserted": 总新增}`。
- **C-20** 日志文本（原样）：单群有新增 `历史补采 group={群名} 新增 {n} 条`；总计有新增 `历史补采完成：新增 {n} 条（回溯 {小时} 小时）`；无新增且 `force` 为真 `历史补采完成：没有新消息。`（INFO）；无新增且非 force 同一句走 DEBUG。
- **C-21** 返回值是本次**新增**条数（不是扫描条数）。重复调用同批数据第二次返回 0。

### 6.5 `available_floor` 契约

- **C-22** 目的：探测某个群「能翻到多早」。按页取历史（每页条数取 `page`，默认 1000），最多 `max(1, max_pages)` 页（默认 20）。
- **C-23** 翻页方式：每页取回后用 `message_seq` 继续往前翻；以下任一情况停止——取回空页、最老一条没有可解析的 `message_seq`、该 `message_seq` 已经出现过（防死循环）。
- **C-24** 「最老一条」按消息时间取最小者；若候选有时间而当前记录无时间，或候选时间更早，则替换。
- **C-25** 返回字典键固定：`group_id`、`floor_ts`（最老一条的时间 ISO 或 `None`）、`floor_message_seq`（最老一条的 `message_seq` 原值或 `None`）、`total_seen`（累计看到的条数）、`pages`（实际取了几页）、`error`（异常文本；正常为空串）。
- **C-26** 任何异常都被**捕获**成 `error` 字段返回，不向调用方抛；已翻页数与已见条数保留。
- **C-27** 调用会按页发请求，页数上限与每页条数由参数决定；不发并发请求。

### 6.6 `backfill_range` 契约

- **C-28** 参数含义：`groups` 为 `None` 时用白名单，否则用传入列表（空串过滤掉）；`since`/`until` 可给时间或 ISO 串，解析失败视为不设边界；`max_messages` 是**全局**扫描上限（`<=0` 表示一条都不扫）。
- **C-29** 逐群、逐页扫：每页条数固定 1000；每处理一条消息 `scanned` +1（全局与群级各自计数）；时间不在 `[since, until]` 内的跳过；命中时间范围的走 `history_to_record`（`allowed_groups` 只含当前群）；入库成功 `inserted` +1。
- **C-30** 停止条件（按优先级）：全局扫描数达到 `max_messages`（并把该群标记 `capped`）→ 取消回调返回真 → 空页 → 群最老一条的时间早于 `since` → 最老一条没有可解析 `message_seq` → 同一 `message_seq` 重复出现（防死循环）。
- **C-31** 取消语义：`cancel` 是零参可调用对象，**每页之前**和**每个群之后**各查一次；已插入的数据**不回滚**（这是有意为之：取消 = 停下，不是撤销）。因取消而中断时，返回值的 `error` 字段为 `"已取消"`。
- **C-32** 进度回调：`progress` 非 `None` 时，每处理完一个群调用一次，参数依次为 `"history"`、`已完成群数（从 1 起）`、`总群数`、`群号`。
- **C-33** 返回字典：`{"ok": 所有群都没有 error, "inserted": 总新增, "scanned": 总扫描, "groups": [群明细...], "error": "已取消" 或 ""}`。群明细键固定：`group_id`、`inserted`、`scanned`、`floor_ts`（该群最老一条时间 ISO 或 `None`）、`capped`（布尔）、`error`（文本）。
- **C-34** 单群异常只记在该群明细的 `error` 里，**继续**处理下一个群；整体 `ok` 因此为假。
- **C-35** 幂等：同一范围重复执行，第二次 `inserted` 为 0（同 `C-18` 的去重语义），但 `scanned` 仍按扫描计。

### 6.7 外部依赖

HTTP（NapCat 的 OneBot v11 接口，POST JSON，单次超时 ≥5 秒、默认 15 秒、不重试）、SQLite（经注入的存储层，仅通过 `insert_message` 写入并要求其按 `msg_id` 去重）、系统时钟、日志。附件入队是可选回调。

### 6.8 测试锁住的事实

来自 `tests/test_catchup.py`（该测试用假客户端注入，不联网）：

1. 白名单 1 个群、`catchup_enabled=True`、`catchup_hours=24`、`catchup_count=50`；5 条历史里：1 条正常、1 条自己发的、1 条 48 小时前、1 条白名单外的群、1 条 90 分钟前 → **新增 2 条**，库里 `messages=2`、`unprocessed=2`。
2. 同一批数据再跑一次 → **返回 0**，库里仍 2 条（去重）。
3. 客户端每次取历史都抛错时 → 返回 0，`stats` 为 `groups=1`、`ok_groups=0`、`failed_groups=1`。
4. `backfill_range` 指定 `groups=["other"]`、窗口 120 分钟：两页共 3 条、其中 2 条在窗口内 → `inserted=2`，该群明细 `scanned=3`；第二次调用时传给客户端的 `message_seq` 是**第一页最老那条的** seq（值 200）。
5. `available_floor(page=1, max_pages=2)`：两页各 1 条、seq 分别为 20 与 10 → `pages=2`、`floor_message_seq=10`。
6. 取消回调第一次返回假、第二次返回真：`backfill_range` 仍已插入 1 条，且库里保留这 1 条（取消不回滚），返回 `inserted=1`。
7. 历史消息只有图片段时：`content` 为 `[图片]`、`group_name` 取别名「学院通知群」、`sender_name` 取 `card`「辅导员」。

---

## 7. 契约条目计数

| 模块 | 条目数 | 编号区间 |
|---|---|---|
| `timeutil` | 10 | T-01 … T-10 |
| `weburl` | 11 | U-01 … U-11 |
| `retry` | 12 | R-01 … R-12 |
| `logging_setup` | 12 | L-01 … L-12 |
| `bot` | 20 | B-01 … B-20 |
| `catchup` | 35 | C-01 … C-35 |
| **合计** | **100** | |

测试锁住的事实共 **17 条**（第 3 节 6 条、第 4 节 4 条、第 6.8 节 7 条）。

---

## 8. 需要拍板（测试没说清、实现前必须定）

- **D-01 日志文件名里的旧产品名要不要保留。** `logging_setup` 目前把日志写到 `qq-live-digest.log`（连字符旧名），`tests/test_logging_setup.py` 三处断言写死了这个名字，启动器也从同一路径读尾部做报错提示。重写若改名 → 测试红 + 启动器读不到日志。建议：**保留文件名**，只把包/模块名换成新的。（这是全文档唯一出现旧产品名的地方，因为它是运行期文件契约，不能含糊。）
- **D-02 轮转与「第二个进程只读」。** 现有部署形态是「服务进程写、启动器只读」，所以按大小轮转是安全的。若重写要引入第二个写者（例如独立日志采集进程），轮转方案必须换（否则改名与读句柄会打架）。
- **D-03 重试的「最后一次兜底 raise」是否保留。** 现有实现循环里已经保证要么返回值要么抛出，函数末尾还有一句兜底 `LLMError("{label}失败：{last}")`，正常路径不可达。保留=多一层保险（但会让人以为失败会被包装）；删掉=更诚实。测试不覆盖这一句。
- **D-04 `weburl` 的链接形状没有测试。** 锚点固定为 `#task-{整数}`、永远是「基址 + `/`」。这个形状被前端和后端分享（用户点开直接跳到某条待办），属于**跨端契约**：前端路由是否认 `#task-N`？要不要改成 `?task=N`？需要前端确认后才能定死。
- **D-05 `detect_lan_ip` 用外部地址探测。** 目前借 `223.5.5.5:80` 的一次 UDP `connect`（不发包）取本机出口地址。离线或被策略拦时回落 `127.0.0.1`。要不要换成纯本地手段（枚举网卡、取首个非回环 IPv4）？后者结果更可控但实现更长；前者行为已被现网验证。
- **D-06 私聊凭据文件没有读者。** `bot` 把私聊记录追加进 `data/c2c_openids.jsonl`，但仓库里**没有任何代码读它**；推送白名单走配置项 `QQ_DIGEST_PUSH_OPENIDS`（用户手填）。要定：这个文件是「给用户自己查」的产物（保留），还是应该被自动读取（那要另加功能与去重规则）？
- **D-07 `bot` 无测试。** 群/私聊记录形状、日志文案、线程退出语义全靠本文。要不要为「记录形状」补一组不连网的测试（构造假消息对象直接调入口）？建议补，否则重写无从验收。
- **D-08 `start()` 失败语义。** 缺 AppID/Secret 时 `start()` 返回假且只写 `last_error`，**不抛异常**；上层只能靠返回值/`last_error` 判断。是否改成抛异常或返回错误对象，需要看上层（服务启动流程）的容错设计。
- **D-09 `stop()` 的超时参数没人用。** `stop(timeout=10)` 里关客户端固定等 3 秒、`join` 用 `timeout`。若上层传的值 < 3，实际还是会等 3 秒。要不要让两个超时共用同一个参数？
- **D-10 `backfill` 的「自己发的消息」过滤依赖登录信息成功。** 登录失败时不过滤自己发的消息（只靠白名单与去重）。要不要改成登录失败就跳过本轮补采（更安全但会漏采）？
- **D-11 `backfill_range` 的 `max_messages` 是全局还是每群。** 现行为是**全局**累计上限（达到后后续群直接标记 `capped` 且不再扫）。若产品期望「每个群各自上限」，语义要改，且测试需同步。
- **D-12 取消后的返回语义。** 现行为：已插入保留、`error="已取消"`、`ok` 仍可能为真（取决于群是否有 error）。是否要求 `ok=False` 表示「未完成」？调用方（网页进度条）依赖哪个字段？
- **D-13 日志文本算不算契约。** 本文按「算」处理（逐条给了原文）。若产品认为日志可自由措辞，请明确，否则实现者会保留这些中文句子。
- **D-14 `timeutil` 的毫秒阈值。** `10_000_000_000` 这个分界（秒 vs 毫秒）是硬编码约定；2038 年之后秒级时间戳仍远小于它，但「毫秒级但小于阈值」的假数据会被当秒解析。要不要改成显式参数或按位数判断？
