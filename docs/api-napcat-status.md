# `/api/napcat/status` 契约

NapCat / QQ 接入状态查询接口。前端「连接」卡片、手机接入向导、状态灯都以本文件为准。

- **方法 / 路径**：`GET /api/napcat/status`
- **鉴权**：`X-Token: <token>` 请求头，或 `?token=<token>` 查询参数（常量时间比较）。服务端 token 为空时只接受本机来源（`127.0.0.1` / `::1`）。
- **HTTP 状态码**：**总是 200**。业务失败放在响应体的 `ok` / `stage` / `error` 里，不要靠状态码判断。
- **实现**：`qq_live_digest/webapp.py:2105-2132`（`_napcat_status_payload`）、`:2124-2129`（`stage` 判定）、`:1919-1930`（`_napcat_wait_seconds`）。

## 响应字段

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `ok` | bool | OneBot 接口可用：`get_status` 与 `get_login_info` 两个 action 都成功。**`ok: true` 不等于已登录**。 |
| `online` | bool | QQ 账号是否已登录。已登录只有 `ok && online` 一种口径。 |
| `good` | bool | NapCat 报回来的连接健康位，后端照抄。**目前前端未使用**（`web_static/page.html` 全库无 `.good` 引用）——不要据它做判断。 |
| `user_id` | string | 登录的 QQ 号；未登录为空串。 |
| `nickname` | string | 登录的 QQ 昵称；未登录为空串。 |
| `napcat_installed` | bool | 是否找到 **NapCat 引擎文件**（`napcat_admin.detect_boot`）。**不代表 QQ 已安装**，也不代表引擎在运行。 |
| `napcat_root` | string \| null | NapCat 数据目录绝对路径；未找到为 `null`。 |
| `qr_stamp` | string | 二维码变化标记，格式 `<st_mtime_ns>-<size>`；**空串 = 当前没有二维码文件**。页面只在它变化时重取图片。 |
| `napcat_running` | bool | 本安装的 NapCat 启动器进程是否在跑（按 `--user-data-dir` 匹配进程命令行，不看进程名）。查进程失败时为 `false`（假阴性）。 |
| `stage` | string | 状态机当前阶段，见下节。 |
| `waiting_seconds` | number | 当前阶段已持续的秒数，保留 1 位小数。 |
| `error` | string | 给人看的文案。**只用于显示，不要解析**（会随版本改写）。 |

## `stage` 状态机

判定顺序（`webapp.py:2118-2129`），先命中先返回：

| 条件 | `stage` | 含义 / 该做什么 |
| --- | --- | --- |
| 两个 action 都成功 | `online` | 接口通了。是否真的登录看 `online`。 |
| 进程在跑 **且** OneBot 端口在监听，但 action 失败 | `error` | 配置 / token / OneBot 服务本身的问题。查 `/api/napcat/doctor`。 |
| 进程在跑 **或** 端口在监听（且接口失败） | `waiting_qr` | 引擎在、只是还没登录。`error` 文案会区分「已有二维码」和「二维码还没生成」。 |
| 引擎文件在、进程不在 | `stopped` | 点「启动并登录」。 |
| 引擎文件都没有 | `not_installed` | 先装 NapCat（需要先装电脑版 QQ）。 |

两条会影响调用方写法的事实：

1. **`waiting_qr` 期间 `ok` 一定是 `false`。** NapCat 的 OneBot HTTP 服务**要等扫码登录成功才监听**（`napcat_admin.py:714-715`、`:309-311`），所以这段窗口里 `_request_napcat_action` 在探端口那一步就抛错（`webapp.py:1934-1935`）。「连不上」是正常状态，不是故障。
2. **不要用 `stage === "online"` 判断登录。** `stage: "online"` 只说明两个 action 调通了，`online` 仍可能是 `false`（接口在、账号没登录）。判断登录一律用 `ok && online`。

## `waiting_seconds`

- 由后端进程内状态记账（`webapp.py:1919-1930`）：同一 `stage` + 同一 `qr_stamp` 持续多久，就返回多少秒。
- `stage` 变化，或 `waiting_qr` 期间换了二维码（`qr_stamp` 变化），**重新从 0 起算**——那是一次新的扫码。
- 服务进程重启后从 0 起算；它不是「用户第一次打开页面」的计时。
- `online` 阶段固定返回 `0.0`。

## 示例

未安装引擎：

```json
{"ok": false, "online": false, "good": false, "user_id": "", "nickname": "",
 "napcat_installed": false, "napcat_root": null, "qr_stamp": "",
 "napcat_running": false, "stage": "not_installed", "waiting_seconds": 0.0,
 "error": "NapCat 没在运行（连不上 http://127.0.0.1:3001）"}
```

已扫码、等手机确认：

```json
{"ok": false, "online": false, "good": false, "user_id": "", "nickname": "",
 "napcat_installed": true, "napcat_root": "F:\\notice-hub\\data\\napcat",
 "qr_stamp": "1761331200123456789-41235", "napcat_running": true,
 "stage": "waiting_qr", "waiting_seconds": 37.5,
 "error": "已经收到二维码，正在等手机确认；确认后 OneBot 会自己起来，不用刷新页面"}
```

已登录：

```json
{"ok": true, "online": true, "good": true, "user_id": "10001", "nickname": "某某",
 "napcat_installed": true, "napcat_root": "F:\\notice-hub\\data\\napcat",
 "qr_stamp": "1761331200123456789-41235", "napcat_running": true,
 "stage": "online", "waiting_seconds": 0.0, "error": ""}
```

## 前端约定

- 按 `stage` 分支显示，不要解析 `error` 文案（文案会改，测试也不锁全文）。
- 轮询节奏：未登录时 5 秒一次（`page.html:924`，仅当二维码区可见时轮询）。
- 二维码只在 `qr_stamp` 变化时重取（`page.html:908`），不要每次轮询都刷新图片。
- `waiting_qr` 且 `waiting_seconds` 超过 90 秒，才建议用户点「重启引擎」。

## 相关接口

- `GET /api/napcat/qrcode`：返回 PNG；没有二维码时 `404 {"ok": false, "error": "QR code not found"}`。
- `GET /api/napcat/doctor`：`napcat_admin.diagnose()` 的链路自检，指出第一处断点（`broken` + `hint`）。
- `POST /api/napcat/install` / `launch` / `restart` / `autosetup`：写操作，成功与否看响应体的 `ok`。
