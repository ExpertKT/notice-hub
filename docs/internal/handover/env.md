# env.md — 环境与启动手册

基准：`F:\qq-notice-hub`，HEAD `ad67e3e`（2026-10-27），分支 `main`。
本文给外派重写方：一台干净机器上照本文能跑到「消息进来 → 待办出现」。
凡推断未取证处标「待确认」。

---

## 1. Python 版本

- **要求 3.12 或更高**。
  证据：`README.md:10` badge `Python 3.12+`；CI 全部 job 固定 `3.12`
  （`.github/workflows/tests.yml:16,25,45,74,108,138`）。
- 本机实际：仓库根 `__pycache__\qq_digest.cpython-314.pyc` → 开发机用的是 **3.14**。
- 代码里用到 3.10+ 语法（`X | None`、`dict[str, tuple[str, bool]]` 顶层泛型），
  所以 `3.12` 是下限，不是等号。

## 2. 依赖安装

```powershell
cd F:\qq-notice-hub
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt`（运行必需）：

| 包 | 用途 |
|---|---|
| `qq-botpy==1.2.1` | 官方 QQ 机器人 SDK（`bot.py`） |
| `openpyxl>=3.1,<4` | 读 Excel 附件 |
| `pillow>=10,<13` | 图片处理 / 二维码生成 |
| `pystray==0.19.5` | 托盘（launcher） |
| `pypdf>=5,<7` | PDF 解析 |
| `pypdfium2>=5,<6` | PDF 渲染（OCR 前处理） |

`requirements-dev.txt`（只测试用，不进包）：`playwright==1.64.1`，仅 `browser` CI job 用。
**未装 playwright 时测试 skip 不红**（`tests/test_ui_browser.py:45` `skipUnless(HAVE_PLAYWRIGHT)`）。

缺依赖的后果分类：
- `openpyxl`/`pypdf`/`pypdfium2` 缺失 → 附件解析类测试 skip。
- 缺 `playwright` → 整个 `tests.test_ui_browser` skip。
- `pystray` 缺失 → launcher 起不来（打包产物），纯 service 不受影响。

## 3. 启动顺序

入口是仓库根 `main.py`（不是包内模块）。

```powershell
.\.venv\Scripts\python.exe main.py doctor      # 先自检
.\.venv\Scripts\python.exe main.py run         # 常驻运行
```

`main.py` 的子命令（`main.py:422-482`）：

| 命令 | 作用 |
|---|---|
| `run` | 常驻监听 + 汇总 + 推送（单实例锁 `data/service.lock`，`main.py:61-100`） |
| `tick` | 只跑一次调度 |
| `preview` | 预览待处理消息会推什么 |
| `send-test` | 发一条测试推送 |
| `doctor` | 自检配置/数据库/推送通道；`--bundle` 生成脱敏诊断包 zip |
| `stats` | 统计 |
| `show` | 回看摘要 |
| `tasks` | 待办清单 + 打印手机访问地址；`--import-existing` 从历史摘要回填 |
| `catchup` | 从 NapCat 补采历史 |
| `attach-test` | 解析群文件/图片，只打印不推送 |

`DigestService.start()` 的真实启动次序（`service.py:341-408`）：

1. 官方 QQ 机器人（`bot.start()`，需 `official_bot_enabled` + appid + secret）
2. OneBot/NapCat 接收器（`receiver.start()`，监听 `onebot_port`）
3. 钉钉本地库 reader（`dingtalk_group_whitelist` 非空才起）
4. 学习通 reader（`chaoxing_enabled` + cookie）
5. 微信 reader（`wechat_group_whitelist` 非空才起）
6. 附件 worker
7. **Web 服务最后起**（`TaskWebServer`，`service.py:389-402`），由 `service.py:27` 顶层
   `from .webapp import TaskWebServer` 导入——这是模块级双向依赖，重写接缝在这里（见 risks.md）
8. catchup 调度 + supervisor 守护线程

两个 exe 的关系（`packaging/`）：

- `qq-live-digest.exe` ← `packaging/qq_live_digest.spec`，打 service 本体
- `QQ-Notice-Hub.exe` ← `packaging/launcher.spec`，打托盘/launcher（`launcher.py`）
- 构建时 launcher 的 exe 与 `_launcher_internal` 被拷进 service 目录
  （`packaging/build.ps1:26-27`），所以安装根里只有一个 `QQ-Notice-Hub.exe`。

## 4. 端口

| 端口 | 用途 | 配置项 | 默认 |
|---|---|---|---|
| 8766 | Web（页面 + `/api/*`） | `QQ_DIGEST_WEB_PORT` | `config.py:251,440` |
| 8765 | OneBot/NapCat 反向 WS | `QQ_DIGEST_ONEBOT_PORT` | `config.py:269,455` |

NapCat 自身 WebSocket 端口（3001 等）在 `napcat_admin.py` 里硬编码/探测，不是配置项。

## 5. 数据与日志目录

- `data_dir`：默认 `<项目根>/data`（`config.py:303,320`），可用 `QQ_DIGEST_DATA_DIR` 覆盖
- `log_dir`：默认 `<项目根>/logs`（`config.py:304,321`），可用 `QQ_DIGEST_LOG_DIR` 覆盖
- 建目录：`config.py:685-687 ensure_dirs()`

`data/` 下的文件：

| 文件 | 内容 |
|---|---|
| `digest.sqlite3` | 唯一真实数据库（WAL 模式，`store.py:177`），11 张表 |
| `messages.jsonl` | 消息审计流水（`store.py:167,220-225`） |
| `digests.jsonl` | 摘要审计流水（`store.py:168`） |
| `chaoxing.cookie` | 学习通 cookie |
| `qrcode.png` | NapCat 登录二维码 |
| `service.lock` | 单实例锁 |
| `hosting.json` | QQ 托管偏好 |
| `firewall.request` | 防火墙放行请求（原子写） |
| `tasks.sqlite3` | **遗留产物，代码零引用**（仅 `tests/test_webapp.py:1354` 用同名临时文件） |
| `updates/state.json`、`updates/pending.json` | 更新状态与待安装记录 |

`logs/` 下：服务日志 + `host-actions.log`（宿主动作审计）+ launcher 自己的日志。

## 6. 必需配置项

`Settings.problems()`（`config.py:558-571`）定义了「阻塞」项。最小可跑 `.env`：

```ini
# —— 必填之一：要么有推送通道，要么 ——
QQ_BOT_APPID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
QQ_BOT_SECRET=yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
QQ_DIGEST_PUSH_OPENID=01ABCDEF0123456789ABCDEF0123456   # 至少一个推送通道
QQ_DIGEST_GROUPS=123456789,987654321
QQ_DIGEST_WEB_TOKEN=随便一串长随机字符串

# —— 可选 ——
QQ_DIGEST_ONEBOT_TOKEN=napcat的token
QQ_DIGEST_WEB_PORT=8766
QQ_DIGEST_ONEBOT_PORT=8765
QQ_DIGEST_DATA_DIR=F:\notice-hub\data
QQ_DIGEST_LOG_DIR=F:\notice-hub\logs
```

「不给就跑不起来」的判定：

| 配置 | 后果 |
|---|---|
| 无任何推送通道 | `problems()` 报错（`config.py:563-564`），但不阻止进程启动 |
| `QQ_DIGEST_GROUPS` 空 | 不处理任何群（fail-closed，`config.py:565-566`），会提示 |
| `onebot_enabled` 但无 `QQ_DIGEST_ONEBOT_TOKEN` | 提示接口无鉴权 |
| `catchup_enabled` 但无 napcat token | 补采失败 |
| `web_enabled=false` | 整个 Web 层不起 |

**没有任何一项是进程级硬崩**——服务能起，只是功能空转。这点对重写很重要。

LLM 配置（`QQ_DIGEST_LLM_*`）也是软的：`llm_active`（`config.py:574-576`）在
`backend in {auto, codebuddy}` 或有 dashscope key 时为真，否则走规则兜底。

## 7. 崩溃恢复

| 手段 | 位置 | 说明 |
|---|---|---|
| 服务日志 | `log_dir` 下 | 用 `logging_setup.setup_logging`，`main.py:57` |
| 一键诊断 | `main.py doctor --bundle` | 生成脱敏 zip，含配置描述、DB 各表行数、integrity_check（`doctor.py:181-210`） |
| 单实例锁 | `main.py:61-100` | 避免计划任务与手动启动双跑 |
| supervisor 线程 | `service.py:410-428` | 只负责官方 QQ 机器人掉线重连（5s 起步、指数退避到 300s） |
| launcher 看门狗 | `packaging/launcher.py` | 托盘进程守护 service；`--quit` 信号文件 |
| 计划任务 | `packaging/install-task.ps1` | 注册 `QQ-Live-Digest` 开机自启 |

**数据库损坏没有恢复路径**：`Store()` 对损坏文件直接抛
`DatabaseError: file is not a database`（实测），无 `.bak`、无 rename、无重建。
`doctor.py:196-197` 只是把异常打成一行提示。
用户手动备份：`GET /api/backup`（`webapp.py:2259-2295`，`mode=ro` → `source.backup()` → zip）。

## 8. 冒烟：消息进来 → 待办出现

**没有 HTTP 灌假消息的入口**，所以冒烟要在 Python 里做。以下脚本可复制运行。

> 下面这段 `smoke.py` 只是**示例**，我没有实际执行过它（会写临时 data 目录、起服务）。
> 路径与字段名都对着 `service.py:179 on_message` / `:797 tick` / `store.list_tasks`
> 逐个核过，但没有真机跑通 —— **外派方请先自己跑一遍确认**。

把下面内容存成 `%TEMP%\nh-smoke\smoke.py`：

```python
from qq_live_digest.config import Settings
from qq_live_digest.service import DigestService
from qq_live_digest.timeutil import now_local

s = Settings.from_env(env_file=".env")
svc = DigestService(s)
stamp = now_local()
record = {
    "msg_id": "smoke-1", "source": "qqbot", "event": "GROUP_MESSAGE_CREATE",
    "group_id": "g1", "group_name": "学院通知群", "sender_id": "u1", "sender_name": "辅导员",
    "ts": stamp.isoformat(), "received_at": stamp.isoformat(),
    "content": "【通知】请各班班长今天18:00前提交实验报告，逾期不候。",
}
print("on_message ->", svc.on_message(record))
digests = svc.tick()
print("tick produced:", len(digests))
for t in svc.store.list_tasks(statuses=("candidate", "open"), limit=10):
    print("  TASK", t.get("id"), t.get("summary"), t.get("deadline"))
```

然后：

```powershell
# 1. 建隔离环境（不要碰 F:\notice-hub 的 data/）
$env:QQ_DIGEST_DATA_DIR="$env:TEMP\nh-smoke\data"
$env:QQ_DIGEST_LOG_DIR="$env:TEMP\nh-smoke\logs"
$env:QQ_DIGEST_GROUPS="g1"
$env:QQ_DIGEST_WEB_TOKEN="smoketoken"

# 2. 跑冒烟脚本
.\.venv\Scripts\python.exe $env:TEMP\nh-smoke\smoke.py

# 3. 起 Web 服务，另一个终端查接口
.\.venv\Scripts\python.exe main.py run
curl "http://127.0.0.1:8766/api/tasks?token=smoketoken"
```

预期：`on_message -> True`；`tick produced: >= 1`；待办列表里出现
「提交实验报告」且 `deadline` 落在当天 18:00（由 `event_extract.py` 从正文补时间，
`service.py:984 _merge_extracted_event`）。

> 上面这条链路的每一步都有测试覆盖：`tests/test_service.py:148,161,183` 就是
> 「灌 record → on_message → tick → _sync_tasks → 断言 tasks 表有这条」。
> 如果冒烟跑不通，先跑 `python -m unittest tests.test_service -v` 看是不是环境问题。

替代验证（无 LLM 时）：`python main.py tasks --import-existing --days 30`
能从历史摘要回填待办（`service.py:1120`）。

---

## 必须由人补的项

1. **真实 QQ 群消息**：上面的冒烟是灌假消息。真实链路（QQ 机器人收消息 → NapCat）
   需要真 AppID/AppSecret + 真群，只能人工验。
2. **NapCat 安装路径与 QQ 目录**：`napcat_admin.py` 里有探测逻辑，但具体装在哪、
   QQ.exe 在哪台机器上是本机事实。
3. **推送通道凭据**：WxPusher/Server酱/PushPlus/webhook 的 key 只能人工填。
4. **学习通/微信/钉钉** 的 cookie、数据库路径、内存读取权限，都是本机相关。
5. `packaging/build.ps1` 写死 `.venv\Scripts\python.exe`——在别的机器上必须先建同名 venv。
