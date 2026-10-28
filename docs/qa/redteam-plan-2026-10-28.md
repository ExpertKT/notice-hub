# 红队审计 → 执行计划（2026-10-28）

来源：`docs/qa/redteam-audit-2026-10-28.md`（S1–S21）。基线 HEAD `1eadd35`，版本 1.1.0-beta.13。
目标：把「可被单点攻破」和「坏了自己不知道」两类问题清掉，且不破坏现有 1356 项门禁。

## 批次划分

### P0 · 今天就做（高危 × 1 天）

| 项 | 内容 | 主责 | 触碰文件 | 验收 |
|---|---|---|---|---|
| S1a | 令牌不再进 URL：`?token=` 仅用于换取 `HttpOnly; SameSite=Lax` 会话 Cookie，之后页面不再依赖 URL 令牌 | 后端 | `qq_live_digest/webapp.py`（`_authorized`、`_route_get_root`）、`tests/test_webapp.py` | 浏览器实测：首次访问后刷新，URL 无 token 仍可操作；`localStorage['qq_digest_token']` 不再写入 |
| S1b | 配对二维码改用 5 分钟一次性配对码，不再编入长期令牌 | 后端+前端 | `webapp.py:758` 一带、`pairing.py`、`web_static/js/pair.js` | 手机扫码完成配对，桌面端会话不受影响；旧令牌二维码失效 |
| S17 | 防火墙规则限定 `profile=private,domain` + `remoteip=localsubnet` | 力工（脚本） | `qq_live_digest/host_actions.py`、`packaging/launcher.py`、`tests/test_host_actions.py` | `netsh advfirewall firewall show rule name="群务台 8766"` 输出含 Profile=Private,Domain 与 RemoteIP=LocalSubnet |
| S20 | `/api/health` 说真话：纳入「库可写、接收线程存活、上次成功推送时间、更新源可达」，坏项降级返回 503 | 后端 | `webapp.py`（`_route_get_health`）、`service.py`（暴露状态）、`tests/test_webapp.py` | 手工占住 8765 后启动，`/api/health` 返回 503 且 `reasons` 指出 onebot 未起来 |

红线：不动 `data/**`、`.env*`、`logs/**`、`.venv/**`、`.reports/**`、`packaging/**`（S17 的 `packaging/launcher.py` 属例外，单独授权）；不 commit / 不打包 / 不重启 `F:\notice-hub`。
验收命令：`python -m unittest tests.test_webapp tests.test_host_actions tests.test_pairing` + 全量 `python -m unittest discover -s tests -t .`。

### P1 · 本周（供应链与数据安全 × 3 天）

| 项 | 内容 | 主责 | 触碰文件 | 验收 |
|---|---|---|---|---|
| S2 | NapCat 包：硬编码 SHA-256 校验 + 解压后总大小上限（如 800 MB）+ 解压后校验 boot 产物存在 | 后端 | `napcat_admin.py`、`tests/test_napcat_admin.py` | 篡改 1 字节的包被拒；超限包被拒；正常包仍可装 |
| S3+S15 | 更新器：下载 URL 域名白名单（`github.com`/`objects.githubusercontent.com`/`gitee.com`）、期望哈希来自代码内常量而非清单、安装脚本内复算一次 | 后端 | `updater.py`、`updates_api.py`、`tests/test_updater.py` | 清单指向 `example.com` 被拒；清单哈希被改后安装失败。**落地口径**：`PINNED_SHA256` 是「可选登记」——表里有该版本就必须与清单 sha256 相等（不等拒装），表里没有只记 warning 并放行（旧包不可能预知下一版哈希，空表不能等于禁止升级）；此时安全边界退化为域名白名单 + 清单哈希必存 + 下载/装机两处复算 |
| S4 | 数据库：写 `PRAGMA user_version`、启动做 `integrity_check`、损坏时隔离为 `digest.sqlite3.corrupt-<ts>` 并给页面明确指引、每日自动快照（保留 7 份） | 后端 | `store.py`、`service.py`、`webapp.py`（备份展示）、新增 `tests/test_store_recovery.py` | 写随机字节后启动：不崩、生成隔离文件、页面提示可恢复 |
| S5 | `upsert_task` 改 `INSERT ... ON CONFLICT(task_key) DO UPDATE`，去掉 SELECT→INSERT 窗口 | 后端 | `store.py`、`tests/test_store.py` | 两线程同 key 并发 100 次，无异常、只有一条记录 |

### P2 · 两周内（加固 × 一周）

| 项 | 内容 | 主责 |
|---|---|---|
| S16 | 规则正则改有界执行：单条正则超时（如 50 ms）即该条不命中并记警告；或换 `re2` 语义等价子集 | 后端 |
| S18 | OneBot 令牌为空时**自动生成**并写入 `.env`；绑定到非环回地址且无令牌时拒绝启动 | 后端 |
| S21 | `_Handler.timeout = 30`，并对并发连接数设上限 | 后端 |
| S8 | 更新 `docs/architecture-map.md`：路由数改 GET 60 / POST 35、分派方式改查表、`rules` 状态改已发布 | 力工 |
| S9 | 抽样 20 条 `PAGE_SOURCE` 断言，能改成行为断言的改掉；不能改的加注释说明为何必须锁源码 | 前端+审查 |

### P3 · 择机（低危与体验）

S10 社区计数加开关（默认关或首启询问）、S19 统一 `compare_digest`、S7 清理 `_LAST_START`、S11 日志正文改长度/开关、S12 401 计数与延迟、S13 Windows 默认改 `127.0.0.1`。

## 顺序与依赖

1. P0 的 S1a 必须先做：P1/S3+S15 与 P2/S18 的安全假设都建立在「令牌不轻易外泄」之上。
2. S20 与 S4 同期做：健康检查要把「库损坏」当成一项真实指标，两者接口要对齐。
3. S2/S3 与 S17 无依赖，可并行。
4. S9 放最后：它改的是测试，前面几批都要重跑测试，先改会反复返工。

## 独立验收（不由作者做）

- P0/P1 每批完成后交「审查」跑：全量 1356+ 项、目标用例、以及针对该批的手工破坏性验证（篡改包、损坏库、占端口）。
- S1 必须配浏览器实测截图（1280/390 两档）：URL 无 token、刷新不掉登录、二维码可用。
- 每批合入后在临时冷装实例上验一次，不碰 `F:\notice-hub`。

## 风险

- **S3 残留风险（已知，P1 待补签名）**：没有代码签名时，能改仓库/镜像的人可以**同时**替换安装包和清单里的 sha256——域名白名单只防「把下载指向别处」，不防「内容被换」。`PINNED_SHA256` 只有在发版后回填才提供独立的第二来源；未回填的版本仅靠清单哈希 + 两处复算，两者同源时可被一起换掉。后续需要签名方案（Ed25519 签清单 + 内置公钥，或 Windows 代码签名证书），列入 P1。

- S1 改鉴权 = 全站会话机制变更，最大回归面在 `tests/test_webapp.py`（含大量 401 断言）；建议先加「新老双读」过渡（Cookie 优先、URL 令牌 fallback 一个版本）。
- S17 收紧防火墙可能让「手机连不上」复现（正是当初放宽的原因），必须同时给页面「当前网络是公用网络」的提示。
- S4 引入自动快照会写盘，需要保留份数上限，避免 C 盘再次被吃（用户机器 C 盘只剩约 14 GB）。

## 落地状态（2026-10-28 实测）

| 项 | 状态 | 改动文件:行 | 验证方式 |
|---|---|---|---|
| S1 会话 Cookie | 已落地 | `webapp.py:64-92` HMAC 会话值；`:1509` 下发 `nh_session=…; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000`；`:1534` 鉴权顺序 Cookie→X-Token→`?token=`；`:3166` `POST /api/session/exchange`；前端 `page.html` 换 Cookie 后 `history.replaceState` 抹 token、不再写 localStorage | 实测（18566/18766 临时实例）：无凭据 401；URL token 200 且下发 HttpOnly Cookie；仅 Cookie 200；exchange 正确 200 只回 `{"ok":true}`；错令牌 401。另 `tests/test_token_session.py`（8 条）锁前端契约 |
| S1b 配对码进二维码 | 未做（不在本批范围） | 现状仍为 6 位码 + secret（`pairing.py:53-79`），二维码里是否还编长期令牌未改 | 未验证；与 S14 同一处语义，暂按现状可接受 |
| S2 NapCat 包校验 | 已落地 | `napcat_admin.py:647-667` 两阶段 TOFU；解压流式，字节 800 MB / 条目 5000 双上限 | 篡改 1 字节被拒（无登记 → 返回实算 sha256 并拒装；带 `accept_sha256` 再来 → 登记并安装；已登记但哈希不符 → 删包拒装）；`tests/test_napcat_admin.py` |
| S3+S15 更新器 | 已落地，**已知残留：无代码签名** | `updater.py:49` 域名白名单（子域才算，`evil-github.com` 拒）；`:538` 清单 sha256 必须存在；`:542-553` `PINNED_SHA256` 有登记必须相等、空表放行并记 warning；`:816` `Get-FileHash` 在 `:862` `Expand-Archive` 之前 | `tests/test_updater.py`。残留说明见下方「风险」第 1 条：域名白名单只防「指向别处」，不防「内容被换」；能改仓库/镜像的人仍可同时替换包与清单哈希 |
| S4 数据库加固 | 已落地 | `store.py` `SCHEMA_VERSION=1` + `user_version` 高版本拒启 + 启动 `integrity_check` + 真损坏才隔离为 `.corrupt-<时间戳>` + `<data_dir>/snapshots/` 每日快照保留 7 份（失败只 warning） | `tests/test_store_recovery.py` |
| S5 upsert 竞争 | 已落地 | `store.py` `upsert_task` 改单条 `INSERT … ON CONFLICT(task_key) DO UPDATE … RETURNING id`，用户字段不被覆盖 | 双线程同 key 只留一行，有用例（`tests/test_store.py`） |
| S6/S7 配对限速与清理 | 已落地 | `pairing.py` 配对来源 10 分钟最多 3 次；`clear_pending()` + `POST /api/pair/pending/clear`；`_prune` 同时裁 `_LAST_START` | `tests/test_pairing.py`；`tests/test_token_session.py` 覆盖「清空待配对」前端提示 |
| S8 架构地图 | 已落地 | `docs/architecture-map.md`：路由改 GET 60 字面量 + 1 前缀、POST 37 字面量 + 1 前缀、合计 99 path / 92 个处理方法，并加核对日期 | 按 `qq_live_digest/routes.py` 实际表项数出（脚本计数） |
| S9 源码断言 | 部分落地 | 新增 `tests/test_token_session.py` 锁前端契约 | `tests/test_webapp.py` 里约 454 条 `PAGE_SOURCE`/`PAGE_HTML` 断言未批量迁移，列为后续债 |
| S10 社区计数开关 | 已落地 | `config.py` `community_telemetry: bool = False`（`QQ_DIGEST_COMMUNITY_TELEMETRY`）；`community.py` 关时零请求零缓存、返回 `{"online": null, "today": null}`；`settings_api.py` `_save_telemetry_settings`；`js/guide.js` 双 null → 整行不出现且不重试 | `tests/test_config.py`（默认关/解析/保存路径）、`tests/test_community.py` |
| S11 消息正文入日志 | 已知残留（未改） | `service.py:224` 仍是正文前 60 字入日志，未加开关/脱敏 | 只读核对，无相关用例 |
| S12 鉴权失败限速 | 已落地，已知残留：无告警通知 | 60 秒窗口内 401 > 30 次即锁 60 秒并返回 429 | `tests/test_webapp.py` 相关限速用例；只 429，不主动通知用户 |
| S13 Windows 默认监听 | 未做 | `config.py:25-26` 仍是 `0.0.0.0`（非 Windows 为 `127.0.0.1`） | 只读核对；与 S17 叠加风险仍在，靠防火墙规则与令牌兜底 |
| S14 配对码枚举 | 现状可接受（无需改） | `pairing.py:53-58` 6 位码，`status()` 同时要求 secret（`secrets.compare_digest`） | 单独枚举 code 拿不到任何东西；不要把 code 当秘密宣传 |
| S16 正则回溯 | 已知残留（部分缓解） | `rules.py` 静态拒绝嵌套量词/多个无界 `.*`，输入 500 字符、模式 500 字符上限；**没有超时执行、未换 re2** | `tests/test_rules.py` 覆盖静态拒绝；灾难性回溯仍可能在通过静态检查的模式上发生 |
| S17 防火墙收紧 | 已落地 | `host_actions.py:159-190` 规则带 `profile=private,domain` + `remoteip=localsubnet`，且先删同名旧规则再加；`packaging/launcher.py:541-552` 提权走替换命令 | `tests/test_host_actions.py`（命令串、删后再加顺序）、`tests/test_launcher_watchdog.py`（watchdog 请求路径）。页面已加「公用网络会被拦」说明（`js/pair.js:202`） |
| S18 OneBot 空令牌 | **部分落地（已知残留）** | `receiver.py:295-320` 非环回 + 空令牌拒绝启动；环回 + 空令牌仍放行并在启动时 warning 一次。**没有做自动生成令牌**：NapCat 的 `httpClients.token` 取自同一个 `settings.onebot_token`，且只在「写配置那一刻」取值；已运行的 NapCat 拿着空令牌时单方面收紧，结果是「服务看着健康、群消息全被静默丢弃」，比留一条有记录的弱点更坏 | `tests/test_receiver.py`：非环回拒启、空令牌放行 + warning、`NapcatTokenContractTest`（NapCat 配置里的令牌 == 接收器校验的令牌）。残留已记入 `docs/qa/backlog-beta12.md` |
| S19 常量时间比较 | 已落地 | `receiver.py:127-131` query token 与 Authorization 头都改 `hmac.compare_digest` | `tests/test_receiver.py::test_token_comparison_is_constant_time` + 原有 Bearer/签名通过用例 |
| S20 `/api/health` 说真话 | 已落地 | `webapp.py:2238` 真探库、库挂返 503，响应不含路径/令牌/群 ID | `tests/test_webapp.py` 健康检查用例 |
| S21 超时与并发上限 | 已落地，已知残留：无并发自动化测试 | `webapp.py:1433` socket 超时 30 秒；`:3878` 并发上限 64 | 代码核对；没有专门压并发/慢连接的自动化用例 |

全量门禁：`Ran 1405 tests` / `OK (skipped=1)`（2026-10-28 13:44）
