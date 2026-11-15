# notice-hub 红队审计（最终版）

审计对象：`F:\qq-notice-hub`，HEAD `1eadd35`（1.1.0-beta.13），工作区干净。
方法：只读读代码 + 对本机正在运行的 8766 实例发无副作用请求 + 进程内跑纯函数实测（配对、正则）。
标记：【事实】= 亲眼在代码/实测里看到；【推断】= 由事实推出、未端到端复现；【未证实】= 证据不足，不下结论。

---

## 毒舌结论

这个项目的安全模型是「URL 里的一个令牌 = 全部权限」，同时它把整站挂上公网、把令牌编进二维码和 localStorage、装完就把 8766 在**所有网络配置（含公共 Wi-Fi）**上放行、还会不校验任何哈希就从 GitHub 下载 exe 解压后直接执行；而它的「健康检查」写死返回 `ok: true`，永远不可能报坏。**令牌漏一次就是全盘接管；而它连自己坏没坏都说不清。**

---

## 一票否决项

**「不校验哈希就下载并执行外部程序」（S2）+「自己零代码签名、安装前不复算」（S3+S15）** 合起来是一票否决：发给真实用户前必须处置，否则就是在替上游做分发器。

---

## 高危清单（按严重度）

1. **S1【事实·高】令牌走 URL、被持久化、被编进二维码**
   `webapp.py:1423-1441` 接受 `?token=`；`page.html:913-915` 写入 `localStorage['qq_digest_token']`；`:2028` 拼进图片 URL；`webapp.py:758` 生成 `"?token="+quote(token)`（配对二维码/深链）。服务端日志已脱敏（`:1228`、`:1354` WEB-2），但浏览器历史、截图、Funnel/代理日志不归它管。拍下二维码 = 拿到机主全部权限，且无轮换机制。
2. **S2【事实·高】NapCat 供应链只查 4 字节魔数**
   `napcat_admin.py:563-565` 仅 `b"PK\x03\x04"`；`:568-577` 只挡路径穿越，`extractall` 无解压后总大小上限（500 MB 只限制压缩包下载，`:558`）；`:579-582` 解压完即找 boot 准备运行。
3. **S3【事实·高】更新器安装前不复算哈希**
   落地是 `Expand-Archive -LiteralPath $zip -DestinationPath $root -Force`（`updater.py:791-800` 一带），校验与安装之间存在 TOCTOU；全仓 `sigstore|authenticode|Set-AuthenticodeSignature|codesign|gpg` 零命中。
4. **S15【事实·中高】更新清单同时携带 URL 与 sha256 → 哈希钉死形同虚设**
   `updater.py` 的 `download()` 直接用 `info["download_url"]`（还有 `fallbacks[].download_url`），**无域名白名单**；sha256 也来自同一份清单。谁能改清单，谁就同时控制「下哪」和「哈希是多少」。哈希校验只能防「传输损坏」，防不了「清单被换」。
5. **S16【事实·中高】规则引擎的正则防护是启发式，能写出灾难性回溯**
   静态拦截只挡嵌套量词与多个无界 `.*`；实测 `(a|aa)+$` **通过校验**，且实测回溯耗时随长度指数增长：n=24 0.025s、n=26 0.068s、n=28 0.191s、n=30 0.463s（每 +2 字符约 ×2.5）。该正则对**每条群消息**执行（`service._apply_rules` → `rules.match`），而消息正文由群成员控制 → 一人在群里发 40~50 个 `a` 加一个 `b` 就能把收消息线程挂住数分钟。
6. **S17【事实·中高】防火墙放行规则不限配置文件与来源**
   `host_actions.firewall_allow_command()` 生成的是 `netsh advfirewall firewall add rule name="群务台 8766" dir=in action=allow protocol=TCP localport=8766` —— **没有 `profile=`、没有 `remoteip=`**，netsh 默认对所有配置文件生效（含「公用网络」）且允许任意来源。叠加 S13 默认监听 `0.0.0.0`：在咖啡厅/酒店 Wi-Fi 上，同一网络里每台设备都能访问登录页与所有公开端点。
7. **S4【事实·中高】数据库无版本号、损坏无自愈、备份只靠手点**
   只有 `store.py:184-202` 的加列式迁移；坏库直接 `DatabaseError: file is not a database`，文件原样留着，无 `.bak`、无重建、无隔离；备份仅 `GET /api/backup`（`webapp.py:2259-2295`）手动触发。
8. **S5【事实·中】`upsert_task` 竞态无 IntegrityError 兜底**
   `store.py:76` `task_key ... UNIQUE`；`:649-665` 为 SELECT→INSERT，中间无互斥；全文件 `grep IntegrityError` 零命中 → 并发同 key 时后者抛错上冒，待办丢失。
9. **S18【事实·中】OneBot 接收器空令牌即全放行，绑定地址用户可改**
   `receiver.py:116-119` `if not token: return True`；`config.py:270` `onebot_token: str = ""` 默认空；`config.py:454` `QQ_DIGEST_ONEBOT_HOST` 可被改成 `0.0.0.0`。三者叠加 = 无鉴权的 QQ 消息注入（伪造消息入库、进 LLM、生成待办）。
10. **S6【事实·中】配对端点免鉴权且槽位可被外人占满**
    `POST /api/pair/start` 在公开路由表；`pairing.MAX_PENDING=5`、`PAIR_TTL_SECONDS=300`。实测连发 6 次不同 source，第 6 次被拒 → 任何人刷 5 次即可让机主 5 分钟内无法配对手机。
11. **S20【事实·中】健康检查永远不会报坏；监督只盯官方机器人**
    `webapp.py` `_route_get_health` 写死 `{"ok": True, "service": "qq-tasks", "channels": ...}`，不查库、不查线程、不查上次成功推送；`service._supervise()`（`service.py:468-486`）只在「official_bot_enabled 且有 appid/secret」时才重启，OneBot/钉钉/微信/学习通接收线程**无人监督**。→ 8765 端口被占导致接收器起不来时，服务照样 `ok: true`，用户以为在跑。
12. **S21【事实·中】HTTP 无连接超时，慢连接可耗尽线程**
    `_Handler` 只有 `server_version`，无 `timeout` 属性；`ThreadingHTTPServer` 每连接一线程且无上限（`webapp.py:1350`、`:3806`）。一旦 8766 对公网/局域网可达（S17），半开连接即可占线程。
13. **S8【事实·中】架构地图三处陈述已过期**
    `docs/architecture-map.md:12`「91 个路由分支」、`:156`「合计 91：GET 57 / POST 34」→ 实际 `routes.py` 为 **GET 60 / POST 35**；`:21`「手写 `if path ==` 链，`webapp.py:2835/3540`」→ 已改为查表分派；`:136`「`rules` 在 `lab.py:29` 仍标注未定稿」→ 注释已改、功能已发布。
14. **S9【事实·中】测试的绿色主要证明「模板字符串还在这」**
    `tests/*.py` 内 `PAGE_SOURCE`/`PAGE_HTML` 出现 624 次，其中 320 行拿页面/JS 源码片段做断言。改模板即红、行为坏了可能绿：本轮社区在线数那个跨三层根因的 bug 就是在全绿状态下长出来的。
15. **S10【推断·中低】社区计数向第三方上报 IP 与使用时段**
    `community.py` 打点 `abacus.jasoncameron.dev`；键内无安装标识，但请求本身暴露来源 IP 与时段。我在 `config.py`/`webapp.py` 引用里没找到开关（**未证实不存在**，只是没搜到）。
16. **S19【事实·低中】OneBot 鉴权里有非常时比较，与同函数自相矛盾**
    `receiver.py:123` `if token in params.get("access_token", [])`、`:126` `if candidate == token` 用的是 `in`/`==`；而同一函数 `:132` 的签名校验用了 `hmac.compare_digest`。`webapp.py:1433-1436` 的 WEB-1 注释正是为了解决这个侧信道——同一项目、两套标准。
17. **S7【事实·低】`_LAST_START` 只增不删**
    `pairing._prune()` 只清 `_REQUESTS`，`_LAST_START` 按来源 IP 永久累积。
18. **S11【事实·低】消息正文前 60 字进日志** `service.py:216-222`（日志轮转有：`logging_setup.py:30-33` 5 MB×5）。
19. **S12【事实·低】鉴权失败无限速无告警** 无 token 请求 → `webapp.py:2953-2955` 401，可无限重复；无计数、无锁定、无通知。
20. **S13【事实·低】Windows 默认监听 `0.0.0.0`** `config.py:23-25`（有令牌兜底，但可达面默认放大，且与 S17 叠加）。
21. **S14【事实·低】配对码 6 位数字可枚举** `pairing._new_code()`；但 `status()` 同时要求 secret（`secrets.compare_digest`），单独枚举 code 拿不到任何东西 → 现状可接受，别把 code 当秘密宣传。

---

## 完整缺陷表

| ID | 缺陷 | 证据/位置 | 触发条件 | 影响 | 严重度 | 置信度 | 类型 | 验证方法 | 修复方向 |
|---|---|---|---|---|---|---|---|---|---|
| S1 | 令牌走 URL/二维码并被持久化 | `webapp.py:1423-1441,758`；`page.html:913-915,2028,2146,2569` | 带 token 的链接被截图/转发/留历史 | 拿到链接=机主全部权限 | 高 | 高 | 事实 | `?token=` 打开后查 localStorage | 一次性换 HttpOnly Cookie；二维码用短时配对码 |
| S2 | NapCat 零完整性校验后执行 | `napcat_admin.py:563-565,568-577,579-582` | 上游包被换 / 恶意包 | 以用户权限执行任意代码 | 高 | 高 | 事实 | 换一个仅含 `PK\x03\x04` 的包 | 固定 SHA-256 + 解压后大小上限 + 签名 |
| S3 | 安装前不复算哈希、无签名 | `updater.py:791-800` | 校验与安装之间文件被替换 | 装上未验证代码 | 高 | 中 | 事实（TOCTOU） | 校验后替换 zip 再安装 | 安装脚本内复算 + 代码签名 |
| S15 | 清单同时给 URL 与哈希 | `updater.py:download()` 用 `info["download_url"]`/`fallbacks` | 清单被篡改 | 任意来源安装包 | 中高 | 高 | 事实 | 改本地清单指向自建 URL | 域名白名单 + 哈希硬编码在代码里 |
| S16 | 正则防护可绕过，回溯爆炸 | `rules.py` 静态检查；实测 `(a|aa)+$` 通过；n=30→0.463s | 规则写错 + 群成员发特定文本 | 收消息线程长时间阻塞 | 中高 | 高 | 事实（实测） | `parse_rules` 传 `(a|aa)+$` 看是否通过 | 换 `re2`/超时执行/长度上限 |
| S17 | 防火墙规则不限配置文件/来源 | `host_actions.firewall_allow_command()` | 首次放行后接入公共网络 | 公共同网设备可访问 | 中高 | 高 | 事实 | `netsh ... show rule name="群务台 8766"` 看 Profile/RemoteIP | 加 `profile=private,domain` + `remoteip=localsubnet` |
| S4 | 库无版本/无自愈/无自动备份 | `store.py:184-202`；`webapp.py:2259-2295` | 断电、磁盘错误 | 历史不可恢复 | 中高 | 高 | 事实 | 写随机字节后启动 | `user_version`+`integrity_check`+每日快照 |
| S5 | upsert 竞态无兜底 | `store.py:76,649-665` | 两线程同 task_key | 待办丢失/同步中断 | 中 | 中高 | 事实 | 并发同 key | `ON CONFLICT DO UPDATE` |
| S18 | OneBot 空令牌全放行 | `receiver.py:116-119`；`config.py:270,454` | 未设令牌且绑定改 `0.0.0.0` | 伪造消息注入 | 中 | 高 | 事实 | 空 token + 改 host 后 POST 事件 | 启动时自动生成令牌并强制鉴权 |
| S6 | 配对槽位可占满 | `pairing.py` MAX_PENDING=5/TTL=300；公开 POST | 连发 5 次 | 机主 5 分钟不能配对 | 中 | 高 | 事实（实测） | 连发 6 次 `pairing.start()` | 按来源限流 + 一键清空 |
| S20 | 健康检查永真；接收线程无监督 | `webapp.py:_route_get_health`；`service.py:468-486` | 接收器起不来/线程死 | 静默失效无人知 | 中 | 高 | 事实 | 占住 8765 再启服务，看 /health | 健康检查纳入线程/库/上次推送 |
| S21 | 无连接超时 | `webapp.py:1350,3806` | 半开连接 | 线程耗尽 | 中 | 中高 | 事实 | 开若干不发送的 TCP 连接 | `timeout=30` + 连接数上限 |
| S8 | 架构地图过期 | `architecture-map.md:12,21,136,156` | 新人据此施工 | 按错前提改代码 | 中 | 高 | 事实 | 对比 `routes.py` 实际条目 | 更新或标注过期 |
| S9 | 测试锁源码字符串 | `tests/*.py` 624 处/320 行 | 改模板即红、行为坏仍绿 | 虚假安全感 | 中 | 高 | 事实 | 抽 10 条看断言对象 | 逐步替换为行为断言 |
| S10 | 社区计数外联无开关 | `community.py` | 每次开页 | 隐私画像；离线仍尝试外联 | 中低 | 中 | 推断 | 断网观察外联；grep 开关 | 加开关默认关，或改本地统计 |
| S19 | OneBot 非常时比较 | `receiver.py:123,126` vs `:132` | 网络可达 + 大量采样 | 逐步猜出令牌 | 低中 | 高 | 事实 | 代码审阅；同函数内对比 | 统一 `compare_digest` |
| S7 | `_LAST_START` 只增不删 | `pairing.py:_prune` | 大量来源 IP | 内存缓慢增长 | 低 | 高 | 事实 | 循环新 source | 与 `_REQUESTS` 一起清 |
| S11 | 消息正文入日志 | `service.py:216-222` | 每条消息 | 拷走日志即泄露 | 低 | 高 | 事实 | 查 logs | 开关/脱敏/保留期 |
| S12 | 无限速无告警 | `webapp.py:2953-2955` | 持续猜 | 无告警的长期扫描 | 低 | 高 | 事实 | 连发 100 次看有无 429 | 计数 + 延迟 |
| S13 | 默认 `0.0.0.0` | `config.py:23-25` | 默认安装 | 可达面扩大 | 低 | 高 | 事实 | 看默认配置 | 默认环回 |
| S14 | 配对码 6 位 | `pairing._new_code` | 无 secret 时无意义 | 单独枚举无效 | 低 | 中 | 事实+推断 | 只带 code 请求 status | 无需改 |

---

## 审查后确认「干净」的部分（避免误伤）

以下我专门查过并**未**发现问题，不作为缺陷上报：

- 静态脚本 `/js/*`：`webapp.py:_route_get_js` 用 `resolve()` + `script.parent == js_root` + `.js` 后缀三重限制，穿越不成立。
- 所有 `rfile.read()` 调用点（`webapp.py:2971/3008/3043/3064/3467/3488/3535/3557/3598`、`settings_api.py:299`）逐个核对，**每个都在前面挡了 `MAX_BODY_BYTES`**。
- 日志轮转存在（`logging_setup.py:30-33`，5 MB × 5）。
- `settings.describe()` 对 appid/openid 做 `_mask`，且完全不包含 LLM/微信密钥 → 启动日志不泄密。
- `GET /api/settings` 实测无密钥回显（只有 `chaoxing_status` 状态与提示文案）。
- `tools/cleanup-install.ps1` 不是卸载器，但 `uninstall-task.ps1:4` 确实清理了 `NapCat-QQ`/`QQ-Live-Digest`/`NapCat-QQ-Watchdog` 三个任务。
- `/api/backup` 用 `tempfile.TemporaryDirectory` + 内存 zip，不留临时文件。
- 全仓无 `shell=True`/`os.system`/`eval`/`exec`/`pickle.load`。
- 计划任务未用 `-RunLevel Highest` → 以普通用户运行（这条是**减轻**项，不是缺陷）。
- 无「有副作用的 GET」（按 `start/stop/scan/accept/delete/send/apply/...` 扫 GET 路由，零命中）。

---

## 1. 最致命的三个

1. **S1**：一次截图 = 永久全权，且每天发生（打开页面就把令牌写进 localStorage 与 URL）。
2. **S2+S3+S15**：不校验就执行 + 自己不签名 + 哈希与 URL 来自同一份可改清单 → 供应链等于没有防线。
3. **S20+S4**：坏了自己不知道（`/health` 永真）+ 坏了救不回来（库无自愈无备份）。用户会在「以为在用」的状态下丢数据。

## 2. 最容易被自欺的五个假设

1. 「有 token 就安全」——把长期凭据当会话 cookie 用，无轮换。
2. 「HTTPS 就安全」——保链路不保内容；S15 说明连哈希都不能独立保证。
3. 「1356 项全绿 = 质量好」——320 条断言锁的是模板文本。
4. 「文档写了架构，架构就清楚」——路由数、分派方式、rules 状态三处全过期。
5. 「本地优先 = 不联网」——每次开页都向第三方打点，且没找到开关。

## 3. 测试与监控的缺口

- 无端到端行为测试；`tests/test_ui_browser.py:45` 本地因缺 playwright 而 **skip**，只有 CI 跑。
- 无并发/竞态测试（S5）、无破坏性测试（库损坏、磁盘满、下载被替换）。
- 无正则性能测试（S16 这类只能靠实测回溯耗时发现）。
- `/health` 是写死的成功；没有「上次成功推送时间 / 库可写 / 接收线程存活 / 更新源可达」这类真指标。
- 无告警通道：401 洪水、连续推送失败、库写失败、接收器起不来，一律静默。

## 4. 只能修三件

1. **令牌不再进 URL**（S1）：`?token=` 只换 HttpOnly+SameSite Cookie；二维码放 5 分钟一次性配对码。
2. **供应链钉死**（S2+S3+S15）：NapCat 与更新包都用硬编码在代码里的 SHA-256 校验，域名白名单，安装前复算。
3. **健康检查说真话 + 数据能自愈**（S20+S4）：`/health` 纳入线程/库/上次推送；`user_version`+损坏隔离+每日自动快照。

理由：1 的暴露面最大且改动集中；2 是唯一能造成「你没干坏事也被控」的路径；3 决定「出事时用户知不知道、能不能救」。

## 5. 仍缺的证据（缺证据不下结论）

- **S3 未端到端复现**：只读到安装脚本没复算，没做「校验后替换 zip」的受控实验。
- **S10 开关是否真的不存在**：只搜了 `config.py`/`webapp.py`，未穷尽 `community.py` 内部与启动参数。
- **Tailscale Funnel 真实暴露面未实测**：本机 funnel 未启用；若公网可匿名访问，S6/S21 从局域网级升级为公网级。
- **S16 的最坏耗时有外推**：实测到 n=30（0.463s），n=50 的数量级是按每 +2 字符约 ×2.5 推的，未实测（跑满会占用数分钟）。
- **`.env` 明文密钥是否会在设置页回显**：未逐个字段核对，不作结论。
