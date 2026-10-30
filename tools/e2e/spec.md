# tools/e2e — 冷装浏览器 E2E（W1 骨架）

这份文件是**契约的落地说明**：接口、时序、环境变量、失败判定、报告字段。剧本作者只依赖
`harness` 导出的三个名字，不要 import 内部模块。

## 1. 目录

```
tools/e2e/
  run.py                 入口（过滤/列表/复用/保留/有头/换包）
  harness/__init__.py    对外导出：scenario / App / cold_install（+ Scenario / RunReport / CaseResult）
  harness/install.py     cold_install()：解包、随机端口与令牌、起 exe、等 /api/health、teardown
  harness/app.py         App：浏览器 + 接口 + 只读 sqlite + 断言
  harness/cdp.py         标准库实现的 CDP/WebSocket 客户端（驱动本机 Edge）
  harness/paths.py       运行目录（`%TEMP%` / `NH_E2E_RUN_DIR`）与「绝不许落仓库」的校验
  harness/report.py      report.json / report.md / 终端表格 / 失败截图目录
  scenarios/             剧本（`@scenario` 登记；`__init__.py` 只做加载器）
  fakes/                 平台替身（W3 维护，接口见该包的 docstring）
  spec.md                本文
```

## 2. 写一个剧本

```python
from harness import scenario

@scenario("inbox/delete", viewports=("desktop", "mobile"), tags=("fakes:none",))
def run(app):
    app.goto("/")
    app.wait_for(".inbox-row")
    app.assert_count(".inbox-row", 3)
    app.shot("inbox")          # 截图落到本用例目录
```

* `name`：唯一，`--only` 按它或 tag 做**子串**匹配。
* `viewports`：`desktop`(1280×900) / `mobile`(390×844)，每个视口是一条独立用例。
* `tags`：过滤用；`fakes:<name>` 只表示「这个剧本需要某个替身」，替身由剧本自己起（见 §6）。

## 3. App 接口（负责人定死，不要加私货）

| 成员 | 语义 |
| --- | --- |
| `base_url` | 冷装实例地址，如 `http://127.0.0.1:18023` |
| `goto(path)` | 导航并等 `document.readyState == complete` |
| `click(sel)` | 真鼠标按下/抬起（CDP `Input.dispatchMouseEvent`），不是 `el.click()` |
| `fill(sel, value)` | 聚焦 + 原生 setter 赋值 + 派发 `input`/`change` |
| `press(sel, key)` | 支持 `Enter` / `Tab` / `Escape` / `Backspace` |
| `wait_for(sel, state="visible", timeout=10)` | `visible` / `attached` / `hidden` / `detached` |
| `assert_text(sel, expect)` | **包含**匹配（`expect in textContent`）；失败时回显实际文案 |
| `assert_count(sel, n)` | `querySelectorAll(sel).length == n` |
| `assert_attr(sel, name, expect)` | 属性**全等**（`getAttribute` 为 null 也算不等） |
| `assert_no_console_errors()` | 页面异常 + `console.error` + `Log` level=error，累计到现在 |
| `assert_no_failed_requests()` | CDP 网络事件里 status ≥ 400 的响应与未取消的失败请求 |
| `shot(name)` | 视口截图，存到 `<报告目录>/<场景>--<视口>/<name>.png` |
| `db(sql)` | 只接受 `SELECT`，只读打开 `data/digest.sqlite3`（`mode=ro`） |
| `api_get(path)` / `api_post(path, payload)` | 带 `X-Token` 打接口，非 2xx 直接抛断言失败 |
| `fake(name, **opts)` | 起一个 W3 替身并登记收尾（`stop()` 在 App.close 时调用） |

浏览器会话：App 打开后先用 `POST /api/session/exchange` 换 HttpOnly Cookie，再写进浏览器
（`Network.setCookie`），所以地址栏里从不出现 token。

## 3.1 剧本私有的浏览器层拦截（CDP Fetch）

要给某个接口喂固定数据（冷装拿不到缓存、要造 401/超时/异常响应、要控制「请求挂住再放行」的时序）时，
剧本可以自己在浏览器层拦请求，**既不改后端，也不往 harness 里加门面**：

1. `Fetch.enable`，`patterns=[{"urlPattern": <具体 path>, "requestStage": "Request"}]`；
2. 排水时处理 `Fetch.requestPaused`：命中的用 `Fetch.fulfillRequest` 回固定 JSON（响应头自己给
   `Content-Type: application/json`），其余用 `Fetch.continueRequest` 放行；
3. 收尾 `Fetch.disable`；收尾前先把还没回完的 paused 请求放行，别把它们留在那儿挂着。

两个现成实现，照抄即可：

* `scenarios/12_settings_scan_states.py` 的私有 `_Net`：窗口内对同一个接口**每次都回桩**
  （`serve_status_for()` / `pump_hold_status()`），适合页面有多个消费者、或要控制「暂停/恢复」的时序；
* `scenarios/26_announce_and_bell.py` 的私有拦截：只把 `/api/announcements` 换成三条固定数据，
  用来测角标 / 面板 / 已读持久化，不依赖外网。

两条硬约束：

* **拦截范围写死到具体 path，不许 `urlPattern: "*"` 全局接管**：`urlPattern` 一旦放开，别的接口的
  4xx/5xx 与失败请求也会被一起吞掉，收尾那条网络断言就成了摆设。
* **做了 Fetch 拦截的剧本要确认没把真失败藏起来**：`fulfillRequest` 接管的请求不再产生真实响应，
  所以剧本自己也要在关键节点查一遍「该失败的时候真的失败」（例如先造 500/401 再看 UI 报错），
  别只留一个「全部回 200」的桩。`run.py` 收尾会自动跑 `assert_no_failed_requests()` +
  `assert_no_console_errors()`（`NH_E2E_SKIP_REQUEST_ASSERT=1` 可关），但它只看**没被接管**的那些请求。

实现上的两个坑（踩过）：

* `Page.collect()` 只认 console / 网络事件，`Fetch.requestPaused` 会被当未知事件**丢掉**，请求就永远挂着；
  所以等待要靠自己的排水循环（`cdp.pump()` + `evaluate`），别指望 `wait_for` / `settle` 等得到被拦的请求。
* `Page.click()` 结尾会 `collect()`；点完之后立刻断言「拦截生效」大概率失败——先自己排水一次再断言。

## 4. cold_install() 时序

1. 取安装包（默认 `packaging/QQ-Notice-Hub.zip`；小于 1 MiB 直接报「打包没完成」）。
2. 在 `%TEMP%\nh-e2e-<guid>` 建根目录，解包到 `install/`（保留 zip 里的目录结构）。
3. 要一个**空闲端口**（跳过 8765–8768，且 ≥ 18000）与**随机令牌**。
4. 先写 `.env`（见 §5），再 `QQ-Notice-Hub.exe`：`cwd=install/`、`BROWSER=nonexistent-browser-probe`、
   `CREATE_NO_WINDOW`（不弹窗、不弹浏览器）。
5. 轮询 `GET /api/health`，≤90s 到 200 视为就绪；进程提前退出则立刻失败并给出日志目录。
6. `teardown()`：先 `QQ-Notice-Hub.exe --quit` 走官方退出，15s 内端口未关则 `taskkill /F /T`；
   最后删目录（`--keep`、或用例失败时保留并打印路径）。

## 5. 环境变量（`.env`，冷装自己写）

```
QQ_DIGEST_WEB=1                QQ_DIGEST_WEB_HOST=127.0.0.1
QQ_DIGEST_WEB_PORT=<随机>      QQ_DIGEST_WEB_TOKEN=<随机>
QQ_DIGEST_DATA_DIR=<install>\data
QQ_DIGEST_LOG_DIR=<install>\logs
QQ_DIGEST_LLM=0                QQ_DIGEST_VISION=0
QQ_DIGEST_UPDATE_CHANNEL=off   QQ_DIGEST_COMMUNITY_TELEMETRY=0
QQ_DIGEST_AUTOSTART=0
```

`UPDATE_CHANNEL=off` 是 launcher 认的关闭值（`packaging/launcher.py:453-459`），托盘不会去拉更新。
`WEB_HOST=127.0.0.1` 只监听本地，避免 Windows 防火墙弹窗。

### 5.1 微信剧本必须显式指定数据目录（硬约定）

用 `fakes:wechat` 的剧本一律由替身给出 `QQ_DIGEST_WECHAT_DIR`，冷装把它写进 `.env`；
**实例的 `.env` 里没有非空 `QQ_DIGEST_WECHAT_DIR` 时，harness 直接失败**，不许「没有就用本机微信」：

* `App` 的 `/api/wechat*` 请求会先查 `install.has_env("QQ_DIGEST_WECHAT_DIR")`，缺失就抛错（编者注：防止误读开发者自己机器上的真实微信库）；
* `app.fake("wechat")` 同样检查，缺失即报错；
* 报错文案里直接给出怎么修：给剧本加 `tags=("fakes:wechat",)`，或显式传 `extra_env`。

## 6. 平台替身（W3）

`tools/e2e/fakes/` 的接口是 `start(name, **opts) -> Handle`（`Handle.env` / `.info` / `.stop()`）。

**由 run.py 统一起，不由剧本起**：剧本只声明 `tags=("fakes:onebot", "fakes:wechat", …)`，run.py 在
**冷装之前**按 tag 依次 `start(name, **opts)`（此时已分配端口与令牌），把每个 `Handle.env` 合并进
冷装的 `.env`，再起被测实例；每个用例收尾（成功/失败/异常）都在 `finally` 里 `handle.stop()`。
这样「需要 env 的替身」才能生效，也保证替身目录跟着本轮 run dir 走。

参数约定（`root` 一律指向本轮用例目录，产物随 run dir 清理）：

| 替身 | 传给 `start()` 的参数 |
| --- | --- |
| `onebot` | `token`、`target_url=<base_url>/onebot/event`、`target_token` |
| `wechat` / `dingtalk` / `push` | `run_id`、`root=<用例目录>/<name>` |
| `chaoxing` | 无（接线受限，见该文件头） |

`--reuse` 只能用于 `fakes:none` 的剧本：复用实例的 `.env` 是上一轮写的，替身 env 补不进去，run.py 会直接判失败。

## 6.1 运行产物只在 run dir（硬约定）

一次运行的所有产物——Edge 的 `user-data-dir`、下载、截图、日志、SQLite 转储、替身目录、
`report.json`/`report.md`——一律放在**同一个 run dir**：

1. 显式指定 `NH_E2E_RUN_DIR`（或 `--run-dir`）时用它；
2. 否则用 `%TEMP%\nh-e2e-<run-id>`（`tempfile.mkdtemp`，`<run-id>` 形如 `20261029-151049-7595`）；
3. **建不出来就退出**（退出码 2），绝不回落到仓库根或当前目录；
4. run dir 解析后如果落在仓库内，直接报错——仓库里只允许有源码。
5. `--keep` 只影响「这一份 run dir 留不留」，不会把它挪回仓库。

（注意：本机 `.venv` 的 Python 写不了仓库外路径。用系统 `python` 跑，
或显式给 `NH_E2E_RUN_DIR` / `--run-dir`。）

## 7. 失败判定

任一情况该用例失败（不会中断其它用例）：

* 断言失败（`AssertionError`）；
* `console error` / 页面未捕获异常（只有调了 `assert_no_console_errors()` 才判）；
* 意外 4xx/5xx（只有调了 `assert_no_failed_requests()` 才判）；
* `wait_for` 超时、导航超时、`/api/*` 非 2xx；
* 冷装失败（该用例 FAIL，整轮继续；包全缺时 run.py 退出码 2）。

失败时自动补一张 `failure-<viewport>.png`，并把**保留的冷装目录**路径写进报告；
`--keep` 时全部保留。

## 8. 报告

终端表格 + `<run dir>\report.json` + `report.md`（run dir 规则见 §6.1，默认 `%TEMP%\nh-e2e-<run-id>`）。
字段：`scenario`、`viewport`、`ok`、`seconds`、`error`（失败断言原文）、`screenshots`、
`dir`（本用例截图目录）、`repro`（`python tools/e2e/run.py --only <scenario>`）。

## 9. 依赖

浏览器驱动**只用标准库**（`harness/cdp.py` 的 WebSocket + CDP），驱动本机
`msedge.exe`。`requirements-dev.txt` 里的 `playwright==1.64.1` 本机没装，且红线要求
「要装东西先报负责人」，所以没有引入任何新依赖。Edge 缺失时 run.py 直接报错退出码 2。

## 10. 怎么跑、退出码怎么读

```
cd <仓库>                                                # F:\qq-notice-hub
python -X utf8 tools/e2e/run.py --from-source --only 26_announce_and_bell
```

* **必须用系统 `python`，不能用 `.venv\Scripts\python.exe`**：本机 `.venv` 的解释器被限制成只能写仓库内，
  `harness/paths.py` 建不出仓库外的运行目录会拒绝并报「这台机器上 .venv 的解释器被限制为只能写仓库内」，
  进程以 **2** 退出（这是环境没准备好，不是剧本失败，报告不会生成）。换落点用 `--run-dir <仓库外目录>`
  或 `NH_E2E_RUN_DIR`（两者都仍禁止指向仓库内），但换落点救不了 venv——它对仓库外的写权限本身就是被限制的。
* **必须带 `-X utf8`**：不带时在中文 Windows 的 gbk 控制台上打印中文/符号会抛 `UnicodeEncodeError`，
  崩在 `run.py` 的打印处（行号随改动漂移），看起来像「跑不起来」而不是剧本失败。
* **退出码**：`0` 全部通过；`1` 至少一条用例失败（其余用例照跑，报告里逐条标）；`2` 运行环境/参数级失败
  （安装包缺失或太小、`.venv` 写不了、运行目录落进仓库、Edge 缺失），这时通常连 report 都不会生成。
  注意 `--reuse` 撞上需要替身的剧本**不是** 2：它落成该用例的失败（`1`），因为替身的 env 必须在冷装前写进 `.env`。
* **退出码单独取，别接在管道后面**：`python ... | Tee-Object` 拿到的是管道末尾命令的退出码，会把红跑读成 0。
  要留输出就重定向到文件，再单独读 `$LASTEXITCODE`（PowerShell）/ `%ERRORLEVEL%`（cmd）。
* **产物位置**：一律 `%TEMP%\nh-e2e-<run-id>`（或 `--run-dir` / `NH_E2E_RUN_DIR`），规则见 §6.1。
