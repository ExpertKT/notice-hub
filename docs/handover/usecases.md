# usecases.md — application 层用例清单（从 Web 层反推）

口径：`routes.py` 里 **59 条 GET + 33 条 POST 字面路由**，另加 2 条前缀模式路由（`/js/*`、`/api/tasks/<id>`），共 94 条登记项；任务书里说的「91 个路由」是字面量路径去重后的唯一路径数（GET 59 + POST 33 − `/api/settings` 重叠 1 = 91）。本文按 94 条登记项归并，全部覆盖。`入参` 指 handler 真正解析并校验的字段；`副作用` 只算写库／发 HTTP／起子进程／写文件。行号以当前工作副本为准（HEAD `ad67e3e` + 未提交改动）。

## A. 待办与日历派生（核心）

| 用例名 | 入参 | 返回（顶层键） | 副作用 | 使用它的路由 | 现有实现位置 | 核心 |
|---|---|---|---|---|---|---|
| `list_tasks` | 无 | headline, subline, progress, stats, candidates/today/week/later/done, pinned | 无（只读库） | `GET /api/tasks` | webapp.py:2816；派生 webapp.py:962 `group_tasks`、webapp.py:1014 `overview`；store.py:746 | 是 |
| `create_task_manually` | summary:str 必填，空白折叠后 1–200 字；deadline:str 选填，ISO | ok, task_id, summary, deadline | 写库 `upsert_task`（task_key=`manual:<16hex>`） | `POST /api/tasks/create` | webapp.py:3275 | 是 |
| `apply_task_action` | task_id:int（路径）；action ∈ confirm/dismiss/done/reopen/snooze 必填（或 `done:bool` 兜底）；snooze 的 until 由服务端算 | ok, id, action, done, status | 写库 `tasks` + `task_events` | `POST /api/tasks/<id>` | webapp.py:3530；store.py:1000 `apply_task_action`；webapp.py:949 `snooze_default_until` | 是 |
| `correct_task` | task_id:int；correction ∈ {not_notice,not_task,not_urgent,duplicate,category,deadline,clear_deadline} 必填；value 随类型 | ok, id, action, done, status | 写库 `apply_task_correction` + `corrected` 事件 | `POST /api/tasks/<id>`（action=correct） | store.py:1290 | 是 |
| `set_task_urgent` | task_id:str 纯数字 1–2^18；urgent:bool\|null 必填 | ok, 整条 task | 写库 `urgent_override` | `POST /api/tasks/urgent` | webapp.py:3228；store.py:801 | 是 |
| `set_task_pinned` | task_id:str 纯数字；pinned:bool 必填（已完成任务拒绝置顶） | ok, 整条 task | 写库 `pinned` | `POST /api/tasks/pin` | webapp.py:3250；store.py:818 | 是 |
| `compute_task_stats` | 无 | total, candidate, open, done, dismissed, expired, overdue | 无（只读库） | `GET /api/tasks`、`GET /api/meta` | store.py:1065 | 是 |
| `render_calendar_ics` | 无（token 走查询串鉴权） | VCALENDAR 文本（含 VTIMEZONE） | 无 | `GET /calendar.ics` | webapp.py:2584；ics.py:49 | 是 |
| `describe_calendar_subscription` | 无 | lan_base, port, events, calendars[] | 无 | `GET /api/sync/info` | webapp.py:1608 `_sync_info_response`；webapp.py:761 | 是 |
| `test_calendar_url` | url:str 选填，须落在本服务公布地址白名单（webcal 会被改写） | ok, events, url | 发 HTTP（请求自身，禁代理） | `GET /api/sync/test` | webapp.py:2496 | 否 |

## B. 摘要与收件箱（核心）

| 用例名 | 入参 | 返回（顶层键） | 副作用 | 使用它的路由 | 现有实现位置 | 核心 |
|---|---|---|---|---|---|---|
| `list_notices` | 无（固定 limit=8） | items[{id,kind,created_at,summary,body≤600}] | 无 | `GET /api/notices` | webapp.py:2849；store.py:562 | 是 |
| `start_inbox_classify_job` | groups:list[str] 必填 1–500 个；since/until:`YYYY-MM-DD` 选填；limit:int 1–20000 默认 1000 | ok, started | 起后台线程；发 LLM HTTP；写库 `inbox_marks` | `POST /api/inbox/classify` | webapp.py:3356；inbox.py:51 | 是 |
| `promote_inbox_item` | msg_id:str 必填 ≤256；title:str 选填 ≤500；due:ISO 选填 | ok, task_id, error | 写库 `upsert_task`(task_key=msg_id) + `inbox_marks.task_id` | `POST /api/inbox/promote` | webapp.py:3378；inbox.py:87 | 是 |
| `poll_background_job` | kind ∈ history/classify（由路由决定） | running, stage, done, total, note, result | 无（读进程内全局 `_JOB_STATE`） | `GET /api/history/fetch/status`、`GET /api/inbox/classify/status` | webapp.py:776 `_job_snapshot` | 是 |
| `read_correction_insights` | days:int 默认 30，钳到 1–365 | stats, insights[] | 无 | `GET /api/insights` | webapp.py:2865；store.py:1175/1209 | 否 |

## C. 历史回溯（核心）

| 用例名 | 入参 | 返回（顶层键） | 副作用 | 使用它的路由 | 现有实现位置 | 核心 |
|---|---|---|---|---|---|---|
| `probe_history_floor` | group_id:str 必填 ≤128 | ok, floor_ts, total_seen, error | 发 HTTP（NapCat `get_group_history` 分页） | `GET /api/history/floor` | webapp.py:2589；catchup.py:237 | 是 |
| `start_history_backfill_job` | groups:list[str] 必填 1–500；since/until:`YYYY-MM-DD` 选填，since≤until | ok, started | 起后台线程；发 HTTP；写库 `insert_message` | `POST /api/history/fetch` | webapp.py:3337；catchup.py:255 | 是 |
| `cancel_background_job` | 无 | ok, cancelled | 置 cancel 事件（与 classify 互斥同一把锁） | `POST /api/history/fetch/cancel` | webapp.py:3322；webapp.py:795 `_run_job` | 是 |

## D. 实验室（全部非核心，只出候选不写库）

| 用例名 | 入参 | 返回（顶层键） | 副作用 | 使用它的路由 | 现有实现位置 | 核心 |
|---|---|---|---|---|---|---|
| `describe_lab_features` | 无 | ok, result{search,replies,events,rules}, enabled | 无 | `GET /api/lab/status` | webapp.py:2198；lab.py:40 | 否 |
| `search_archived_messages` | q:str 必填 ≤2000；group_id/since/until:str 选填 ≤2000（**不解析成日期**）；limit:int 1–500 选填 | ok, result | 写库（建 FTS 索引）不确定：`search.py:80 ensure_index` 会建索引 | `GET /api/lab/search` | webapp.py:2206；lab.py:64；search.py:141 | 否 |
| `list_reply_candidates` | limit:int 1–500 选填 | ok, result | 写库：门面里的 `replies.list_replies` 会先 `scan()` 把命中写进 `reply_pending` | `GET /api/lab/replies` | webapp.py:2240；lab.py:68；replies.py:214→120 | 否 |
| `ack_reply_candidate` | msg_id:str 必填 | ok, result | 写库（`replies.ack`，见 replies.py:188） | `POST /api/lab/replies/ack` | webapp.py:2987；lab.py:72 | 否 |
| `extract_events_from_text` | text:str 必填 ≤2000 | ok, result | 无（纯文本规则抽取） | `POST /api/lab/events` | webapp.py:3010；lab.py:76；event_extract.py:275 | 否 |

## E. 渠道接入与运行态（非核心）

| 用例名 | 入参 | 返回（顶层键） | 副作用 | 使用它的路由 | 现有实现位置 | 核心 |
|---|---|---|---|---|---|---|
| `read_channel_status` | source ∈ dingtalk/wechat/chaoxing（由路由决定） | ok, enabled, running, state, groups, scan_* | 发 HTTP/文件扫描（wechat 会扫内存） | `GET /api/dingtalk/status`、`GET /api/wechat/status`、`GET /api/chaoxing/status` | webapp.py:1630/1649；chaoxing_receiver.py:269 | 否 |
| `read_napcat_status` | 无 | ok, online, good, user_id, nickname, stage, qr_*, waiting_seconds, error | 发 HTTP ×2 + 端口探测 + 进程枚举 | `GET /api/napcat/status`（并被 selfcheck/overview/setup 复用） | webapp.py:1548 | 否 |
| `read_napcat_doctor` | 无 | ok, steps[], broken, hint, error | 读文件/进程（只读诊断） | `GET /api/napcat/doctor` | webapp.py:1599；napcat_admin.py:277 | 否 |
| `list_napcat_groups` | 无 | ok, groups[{group_id,name,member_count,selected}] | 发 HTTP + 写库群名缓存 | `GET /api/napcat/groups` | webapp.py:2463；webapp.py:1533 | 否 |
| `suggest_groups` | 无 | ok, source, groups[], counts, error | 发 HTTP + 写库群名缓存 + LLM | `GET /api/groups/suggest` | webapp.py:2448；group_suggest.py:150 | 否 |
| `read_napcat_qrcode` | 无 | PNG 字节或 404 | 读文件 | `GET /api/napcat/qrcode` | webapp.py:2473；napcat_admin.py:906 | 否 |
| `install_napcat` | 无 | ok, installed, already_installed, root, version, bytes, error | 下载并解压到磁盘 | `POST /api/napcat/install` | webapp.py:3462；napcat_admin.py:524 | 否 |
| `launch_napcat` | uin:str 选填 | ok, already_running, pid, command, profile_dir, boot | 起子进程 | `POST /api/napcat/launch` | webapp.py:3473；napcat_admin.py:724 | 否 |
| `restart_napcat` | uin:str 选填 | ok, stopped[], pid, api_up, waiting_for_login | 杀进程 + 起子进程 | `POST /api/napcat/restart` | webapp.py:3495；napcat_admin.py:861 | 否 |
| `run_napcat_autosetup` | 无 | ok, steps[], qrcode_path, uin, applied_via, restart_required | 写配置文件 + 起子进程 | `POST /api/napcat/autosetup` | webapp.py:3517；napcat_admin.py:1013 | 否 |
| `read_dingtalk_groups` | 无 | ok, groups[], suggest_source, suggest_error, category, detail | 读本地库 + LLM 建议 | `GET /api/dingtalk/groups` | webapp.py:2660；dingtalk_receiver.py:541 | 否 |
| `read_wechat_groups` | 无 | ok, groups[{id,name,alias,selected}], suggest_* | 扫微信内存（首次数分钟）、更新进程内 `_WECHAT_READ_STATUS` | `GET /api/wechat/groups` | webapp.py:2695；wechat_receiver.py:1094 | 否 |
| `start_source_scan` | source ∈ dingtalk/wechat（由路由决定） | ok, started, scan_state, scan_stage, scan_elapsed_seconds, category, error | 起 reader 扫描线程 | `POST /api/dingtalk/scan`、`POST /api/wechat/scan` | webapp.py:3123；service.py:328 | 否 |
| `read_chaoxing_courses` | 无（用已存 cookie） | ok, courses[] / 502 | 发 HTTP | `GET /api/chaoxing/courses` | webapp.py:2727；chaoxing_receiver.py:57 | 否 |
| `read_chaoxing_cookie_status` | 无 | has_cookie, last_checked_at, result, error | 无（读 meta + 比对 cookie 指纹） | `GET /api/chaoxing/cookie/status` | webapp.py:2304；webapp.py:1693 | 否 |
| `validate_chaoxing_cookie` | 无 | ok, has_cookie, last_checked_at, result, error | 发 HTTP + 写 meta `chaoxing_cookie_validation` | `POST /api/chaoxing/cookie/validate` | webapp.py:3022；webapp.py:1711 | 否 |
| `import_browser_chaoxing_cookie` | 无 | ok, saved, source, courses, attempts[], category, next_step | 读浏览器 cookie 库；写 `data/chaoxing.cookie`；改 settings + reload reader | `POST /api/chaoxing/auto_cookie` | webapp.py:3027；browser_cookie.py:87 | 否 |

## F. 配对、公网、防火墙、向导、托管（非核心）

| 用例名 | 入参 | 返回（顶层键） | 副作用 | 使用它的路由 | 现有实现位置 | 核心 |
|---|---|---|---|---|---|---|
| `start_pairing` | 无 body（device 取 UA、source 取来源 IP） | ok, code, secret, expires_in | 写进程内配对表 | `POST /api/pair/start` | webapp.py:2977；pairing.py:41 | 否 |
| `poll_pairing_status` | code:str ≤16；secret:str ≤128，均必填 | ok, status, token（仅 approved） | 无 | `GET /api/pair/status` | webapp.py:2161；pairing.py:65 | 否 |
| `list_pending_pairings` | 无 | ok, pending[{code,device,source,waiting_seconds}] | 无 | `GET /api/pair/pending` | webapp.py:2811；pairing.py:76 | 否 |
| `decide_pairing` | code:str 6 位数字必填 | ok, status / error | 改进程内配对表 | `POST /api/pair/approve`、`POST /api/pair/deny` | webapp.py:1452 `_pair_decision`；pairing.py:93 | 否 |
| `describe_pair_candidates` | 无 | public_url, tailscale_ready, base, lan[], candidates[], qr_error, firewall, steps | 枚举网卡/子进程探 Tailscale/查防火墙 | `GET /api/pair/info` | webapp.py:2775；webapp.py:350/386/721 | 否 |
| `read_tunnel_status` | 无 | ok, state, message, url, auth_url, retryable, installed | 起子进程（tailscale status） | `GET /api/tunnel/status` | webapp.py:2806；webapp.py:540 | 否 |
| `start_tunnel` | confirm:true 必填（仅 `/api/tunnel/start`）；已设 token | ok, state, message, url, auth_url, retryable | 起后台安装/登录子进程；写审计日志 | `POST /api/tunnel/start`、`POST /api/pair/public` | webapp.py:3181；webapp.py:450；tunnel.py:280 | 否 |
| `read_firewall_status` | 无 | ok, port, state, message, at, checked | 读请求/结果文件 + netsh 只读探测 | `GET /api/firewall/status` | webapp.py:2769；webapp.py:598 | 否 |
| `request_firewall_allow` | confirm:true 必填 | ok, state=pending, request_id, port | 原子写 `data/firewall.request`；写审计日志 | `POST /api/firewall/allow` | webapp.py:3206；webapp.py:578 | 否 |
| `read_onboarding_state` | skip:str（逗号分隔步骤名，≤4 字段）仅 `/next` 用 | accepted_at / step,title,body,index,total / state,action,message | 无 | `GET /api/onboarding`、`GET /api/onboarding/next`、`GET /api/onboarding/status` | webapp.py:2144/2150/2156；1918/2032 | 否 |
| `advance_onboarding` | confirm:true（`/run` 必填）；action ∈ {connect_qq,configure_push,install_napcat,install_tailscale,open_funnel,allow_firewall}；`/accept` 只需 JSON 对象 | ok, state, action, message / accepted_at | 写 meta `onboarding_accepted_at`；起子进程；写防火墙请求文件；写审计日志 | `POST /api/onboarding/accept`、`POST /api/onboarding/run` | webapp.py:2950；webapp.py:1982 | 否 |
| `run_hosting_control` | uin:str 选填 | start/stop 各自的 ok+pid+state 结构 | 杀 QQ 进程 / 起 QQ 进程 | `POST /api/hosting/start`、`POST /api/hosting/stop` | webapp.py:3405；hosting.py:455/498 | 否 |
| `read_hosting_status` | 无 | ok, qq_running, pids, napcat_*, prefs | 进程枚举 + 读状态文件 | `GET /api/hosting/status` | webapp.py:2764；hosting.py:384 | 否 |
| `render_sync_qr` | text:str 必填且唯一，≤512 字符/字节，可打印无空格，scheme ∈ webcal/http/https，无凭据 | PNG | 无 | `GET /api/sync/qr.png` | webapp.py:2524；qr.py `png_bytes` | 否 |
| `render_pair_qr` | base:str 必填且唯一，http/https 带 hostname、无凭据 | PNG | 无 | `GET /api/app/qr.png` | webapp.py:2549 | 否 |

## G. 设置（非核心）

| 用例名 | 入参 | 返回（顶层键） | 副作用 | 使用它的路由 | 现有实现位置 | 核心 |
|---|---|---|---|---|---|---|
| `read_settings` | 无 | 托管 prefs + catchup prefs + 钉钉/微信/学习通/实验室/更新通道字段 | 无 | `GET /api/settings` | webapp.py:2747；hosting.py:554 | 否 |
| `save_settings` | 一个 JSON 对象，键按九个域分组（update_channel / lab_* / napcat_dir / wechat_* / dingtalk_* / chaoxing_* / llm_* / push 凭据 / catchup_* / 托管），**不同域不可混发** | ok + 该域回显 | 写 `.env`；改 `os.environ`；热改 settings；触发 reload reader；删/写 `chaoxing.cookie`；写 meta | `POST /api/settings` | settings_api.py:291；九个 `_save_*`：360/379/406/441/512/540/574/588/606/636 | 否 |
| `save_subscriptions` | groups:list[str]；aliases:dict[str,str]（均可空） | ok, groups, applied | 写 `.env`（`QQ_DIGEST_GROUPS`/`..._ALIASES`）；热改 settings | `POST /api/subscriptions` | webapp.py:3426 | 是 |
| `test_push_channels` | 无 | ok, results[{channel,target,ok,error}] | 对每个通道发 HTTP 测试消息 | `POST /api/settings/test-push` | settings_api.py:251；push.py:339 | 是 |
| `test_llm` | 无（读已存配置） | ok, model, reply, seconds, endpoint, error | 发 HTTP | `POST /api/settings/test-llm` | settings_api.py:220；attachments.py:733 | 否 |

## H. 运维、发布与展示（非核心）

| 用例名 | 入参 | 返回（顶层键） | 副作用 | 使用它的路由 | 现有实现位置 | 核心 |
|---|---|---|---|---|---|---|
| `update_backup_archive` | 无 | ZIP 字节（内含 data/digest.sqlite3） | 建临时目录/临时库并删除 | `GET /api/backup` | webapp.py:2259 | 否 |
| `check_update` | check:真值 选填（强制同步查） | ok, current, latest, has_update, channel, checking, download, applying, pending | 发 HTTP；写 `updates/state.json`；起后台线程 | `GET /api/update` | webapp.py:2366；updates_api.py:494 | 否 |
| `download_update` | 无 | ok, started | 起后台下载线程；写文件；写 `updates/pending.json` | `POST /api/update/download` | webapp.py:3452；updates_api.py:537 | 否 |
| `apply_update` | 无 | ok, restarting, script | 写 `applying.flag` + 安装脚本；起子进程；2 秒后 `os._exit(0)` | `POST /api/update/apply` | webapp.py:3457；updates_api.py:605；updater.py:856 | 否 |
| `read_announcements` | refresh:真值 选填 | ok, items[], from_cache, checked_at, error, current_version, checking | 发 HTTP；写 `updates/state.json`；可能起后台线程 | `GET /api/announcements` | webapp.py:2359；updates_api.py:160；announce.py:265 | 否 |
| `report_community_counts` | 无 | ok, counts{在线,今日}（读不到为 null） | 起后台线程发第三方 HTTP；60 秒进程内缓存 | `GET /api/community/counts` | webapp.py:2182/2075；community.py:46/83 | 否 |
| `read_setup_status` | 无 | ok, platforms{qq,wechat,dingtalk,chaoxing,push,llm} | 间接探测（复用各通道 status） | `GET /api/setup/status` | webapp.py:2309；webapp.py:2079 | 否 |
| `read_health` | 无 | ok, service, channels[] | 无 | `GET /api/health`、`GET /health` | webapp.py:2111 | 否 |
| `read_qq_group_status` | 无 | ok, last_attempt_at, last_success_at, group_count, last_error, is_fetching, next_run_at | 无（读内存 + meta 快照） | `GET /api/qq/groups/status` | webapp.py:2298；webapp.py:3667 | 否 |
| `fetch_inbox_image` | msg_id:str 必填且唯一，须匹配 `onebot:<group>:attach:<16hex>`（≤256） | PNG/JPEG 字节或 404 | 写无（只读库 + 读 `data/files`） | `GET /api/inbox/image` | webapp.py:2314 | 否 |
| `serve_static_asset` | 路径本身（`/js/*` 需 `.js` 且在 web_static/js 内） | HTML/SVG/manifest/JS/302 | 读文件 | `GET /`、`GET /index.html`、`GET /floatball`、`GET /floatball.html`、`GET /setup`、`GET /calendar`、`GET /icon.svg`、`GET /manifest.webmanifest`、`GET /sw.js`、`GET /js/*` | webapp.py:2380/2373/2397/2579/2117/2122/2127/2917 | 否 |
| `read_donate_qr` | 无 | JPEG 字节或 404 | 读文件 `assets/donate-wechat.jpg` | `GET /api/donate/qr.png` | webapp.py:2132 | 否 |
| `read_app_meta` | 无 | groups, channels, reminders, started_at, insights, push_budget, quiet_hours, channel, push, push_help, llm, providers, stats | 无 | `GET /api/meta` | webapp.py:2887；service.py:1167 | 否 |
| `build_selfcheck` | 无 | ok, items[]（每项 key/label/state/value/hint）, last_push_at | 间接探测（7 个来源 + 子进程） | `GET /api/selfcheck` | webapp.py:2407；selfcheck.py:163 | 否 |
| `build_overview` | 无 | ok, sources{qq,napcat,wechat,dingtalk,chaoxing,funnel,push} 各 state/detail/action | 间接探测（同 selfcheck，但只读） | `GET /api/overview` | webapp.py:1736 | 否 |

## 1. 用例总数与分类统计

- **共 73 条**，覆盖 94 条路由登记项（59 GET + 33 POST + 2 前缀模式）；每条路由至少归属 1 条用例，`/api/tasks/<id>` 一条路由触发 2 条用例（`apply_task_action` + `correct_task`）。
- 按资源分组：待办与日历 10｜摘要与收件箱 5｜历史回溯 3｜实验室 5｜渠道接入 17｜配对/网络/向导 15｜设置 5｜运维与展示 13。
- 核心链路 **18 条**（A 组 9 + B 组 4 + C 组 3 + `save_subscriptions` + `test_push_channels`），非核心 55 条。
- 副作用分布：写库 12 条、写 `.env`/配置文件 6 条、发 HTTP 20 条、起子进程 9 条、写状态/审计文件 6 条；纯只读 25 条。

## 2. 有副作用且被多条路由共用的用例（重写最易踩坏）

1. **`read_napcat_status`** — 一条实现被 `GET /api/napcat/status`、`GET /api/selfcheck`、`GET /api/overview`、`GET /api/setup/status` 四路复用，内部发 2 次 NapCat HTTP + 端口探测。selfcheck 靠 `_precomputed_status` 只算一次；重写若丢掉这层复用，自检会从秒级变成十几秒。
2. **`read_channel_status`** — three 路由 + selfcheck/overview/setup 共用；`wechat` 分支会扫微信内存并写进程内 `_WECHAT_READ_STATUS`，返回体既当状态又当缓存。
3. **`list_napcat_groups` / `suggest_groups`** — 都调 `_remember_group_names` 顺手写库群名缓存（webapp.py:1533）。副作用藏在只读路由里，重写时最容易漏。
4. **防火墙请求 `_write_firewall_request`** — `POST /api/firewall/allow` 与 `POST /api/onboarding/run`（action=allow_firewall）共用，写同一份 `data/firewall.request`；后者还会写 `_onboarding_run` 全局态。
5. **Funnel 启动 `_funnel_setup(port).start()`** — `POST /api/tunnel/start`、`POST /api/pair/public`、`POST /api/onboarding/run`（install_tailscale/open_funnel）三路共用同一状态机实例缓存，且都是后台任务。
6. **`_host_action_audit`** — tunnel/防火墙/向导三处写 `logs/host-actions.log`；审计口径分裂会让重写后的溯源丢失。
7. **后台任务锁 `_JOB_STATE` + `_start_job`/`cancel`** — history 与 classify 两族 5 条路由共用一把互斥锁，`poll_background_job` 读的是同一份全局状态；跨用例的可变全局状态，重写必须显式建模为单一「任务槽」。
8. **`compute_task_stats`** — `GET /api/tasks` 与 `GET /api/meta` 共用；后者又叠 `correction_insights`，两处口径若分开算会不一致。

## 3. 看不出归属的用例

| 用例 | 卡在哪 |
|---|---|
| `save_settings` | 九个域各自校验 + 写 `.env` + 热改 `Settings` + 回调 reload，横跨配置、文件、进程内对象三层。不确定它算 application 用例还是「设置适配器」的一堆独立命令；缺的是 `Settings` 在 domain 里到底有没有身份。 |
| `advance_onboarding` | 状态机 `_onboarding_run` 是模块级全局变量（无持久化），门槛判定来自 `_overview_payload` + 防火墙 + funnel 三处，写操作又是起进程/写文件；跨 5 个适配器，看不出该落哪一层。 |
| `build_overview` / `build_selfcheck` / `read_setup_status` | 三者都是「把 7 个通道的 status payload 映射成 state」，是编排还是展示层组装无法从代码判断——取决于 selfcheck 的判定表算不算领域规则。 |
| `read_app_meta` | 一半来自 `service._web_meta`（运行时统计：pushers、started_at、insights），一半来自 `settings_api` 的展示态（PUSH_HELP_LINKS、LLM_PROVIDERS）。是视图拼装还是应用服务，不确定。 |
| `install_napcat` / `launch_napcat` / `restart_napcat` / `run_napcat_autosetup` / `run_hosting_control` | 全是宿主机操作（下载解压、写配置、杀/起进程），几乎不含领域规则；不确定 application 是否该暴露，还是只作为 ports 的一个宿主能力门面。 |
| `describe_pair_candidates` / `read_tunnel_status` / `test_calendar_url` | 网卡枚举、Tailscale 子进程、访问自身 URL——基础设施探测，不是业务用例。 |
| `fetch_inbox_image` | 直接 `store._connect()` 手写 SQL 绕过仓储，又自己读 `data/files` 做路径/类型安全校验；领域规则（附件归属）与适配器细节混在一处，归属要等 ports 定义附件接口才能定。 |
| `report_community_counts` | 第三方计数上报，副作用是真起线程发外网请求，但与任何业务资源无关；可能根本不该进 application。 |
| `correct_task` 与 `apply_task_action` 的关系 | 两者都写 `tasks` 并追加 `task_events`，但 `correction` 会改 category/importance/deadline，`apply_task_action` 只改 status。是两条并行写路径还是同一用例的两个分支，现有代码没给出依据，需要产品口径。 |
| `search_archived_messages` | lab 门面只做透传，`search.py:80 ensure_index` 会建 FTS 索引（写库），但路由与门面都没暴露这一副作用，判不清是「查询」还是「查询兼建索引」。 |

> 不确定项已就地标注；凡标「不确定」处缺的是：domain 实体（Settings/附件/任务槽）的边界定义，以及 `ports` 打算暴露哪些宿主能力。这两份定了，第 3 节可收敛到 0–2 条。


---

## 4. 交接说明（给 G1 契约）

- 本文的「用例名」是建议的 `application` 层用例命名，**不是现有代码里的函数名**。
  「现有实现位置」一列给出今天它实际落在哪个模块的哪个函数，方便重写时对照。
- 标「核心」的 18 条是 `domain`/`ports`/`application` 必须优先定义签名的部分；
  其余 55 条可以先由适配器门面承担，不进 application。
- 第 3 节「看不出归属」的 11 条，共同缺口是两份还没定的东西：
  **domain 实体边界（Settings / 附件 / 任务槽）** 与 **ports 暴露哪些宿主能力**。
  这两份定了，第 3 节可收敛到 0–2 条。
- `save_settings` 一条对应九个域方法（`settings_api.py` 的 `_save_*`），
  是 application 用例还是「设置适配器的一堆独立命令」取决于 `Settings` 在 domain 里
  有没有身份 —— 这是 G1 必须先拍的一件事。