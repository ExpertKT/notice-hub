# api-contract.md — HTTP 路由契约基线

生成方式：`F:\nh-tmp\rewrite\export_routes.py` 读 `qq_live_digest/routes.py` 两张表，
handler 定义位置用 `inspect.getsourcelines` 从 `_Handler` 的真实 MRO 定位。
机器可读版：`F:\nh-tmp\rewrite\routes-contract.json`。

- **92 条字面量路由**（GET 59 / POST 33）+ **2 条前缀模式路由**（不在 92 内）。
- `required` = 走 `_authorized()`（`?token=` 查询串 或 `X-Token` 头）；
  `public` = 免鉴权。
- 「响应要点」= handler 源码里能静态看出的顶层键；**`—` 表示该 handler 的响应体是
  拼装出来的，静态看不出来**，不代表无响应体。
- 「错误码」= handler 源码里出现过的状态码。

## 计数与口径（重要：任务书里写的 91 不准）

```
GET_ROUTES              59
POST_ROUTES             33
字面量路径去重           92
GET_PATTERN_ROUTES       1   (/js/*)
POST_PATTERN_ROUTES      1   (/api/tasks/<id>)
分派标签总数             94
_route_* 处理函数        87   (85 普通 + 2 前缀)
```

「91」是**字面量路径去重**的另一种数法：GET 59 + POST 33 = 92，唯一重叠是
`/api/settings`（GET 和 POST 都有），去重后 91 个唯一路径。本表按 92 行列，
另加 2 条模式路由，共 94 行。

## 动态注册情况：无

- 没有正则路由、没有按方法名反射注册。
- `routes.py:122-128 match_pattern()` 只做 `path.startswith(prefix)`。
- 分派只有两处：`webapp.py:2925-2947`（`do_GET`）、`webapp.py:3588-3605`（`do_POST`）。
- **但有两类「非字面量」路径必须算进契约**（见上表最后两行）：
  1. `GET /js/*` — 前缀 `/js/`，`public`
  2. `POST /api/tasks/<id>` — 前缀 `/api/tasks/`，按 `<id>` 分发到 action/correct 两条用例

## 一个 handler 服务多条路径的情况

| handler | 服务的路径 |
|---|---|
| `_route_get_` | `/`、`/index.html` |
| `_route_get_health` | `/api/health`、`/health` |
| `_route_get_floatball` | `/floatball`、`/floatball.html` |
| `_route_post_api_dingtalk_scan` | `/api/dingtalk/scan`、`/api/wechat/scan`（靠 body 的 `source` 区分） |
| `_route_post_api_tunnel_start` | `/api/tunnel/start`、`/api/pair/public` |
| `_route_post_api_hosting_start` | `/api/hosting/start`、`/api/hosting/stop`（**同一个 handler 名，内部再分**，见下） |
| `_route_post_api_lab_replies_ack` | `/api/lab/replies/ack`、`/api/lab/events` |
| `_route_post_tasks` | `POST /api/tasks/<id>`（action + correction 两条用例） |

注意 `/api/hosting/stop` 在 `routes.py:83` 登记的 handler 名就是
`_route_post_api_hosting_start`（`webapp.py:3405`），实现靠 `path.endswith("/start")`
在同一个方法里二选一（`webapp.py:3419`）。
**重写时这是必须修掉的契约瑕疵：两个语义相反的操作共用一个入口，靠 URL 后缀分支。**

`/api/hosting/start` 与 `/api/hosting/stop` 还有一处共同的坑：`hosting` 模块不可用时
返回的是 **HTTP 200 + `{"ok": false, "error": "托管模块不可用"}`**（`webapp.py:3415-3416`），
不是 5xx。客户端必须看 body 的 `ok`，不能只看状态码。

## 免鉴权路由清单（12 条）

```
GET  /api/donate/qr.png
GET  /api/health
GET  /api/onboarding
GET  /api/onboarding/next
GET  /api/onboarding/status
GET  /api/pair/status
GET  /health
GET  /icon.svg
GET  /manifest.webmanifest
GET  /sw.js
POST /api/onboarding/accept
POST /api/pair/start
+ 前缀 GET /js/*
```

由 `tests/test_routes.py:119-131` 锁死。

## 全量路由表
| 方法 | 路径 | 鉴权 | 实现位置 | 请求参数 | 响应要点 | 错误码 |
|---|---|---|---|---|---|---|
| GET | `/` | required | `qq_live_digest/webapp.py:2380` | — | — | 200 |
| GET | `/api/announcements` | required | `qq_live_digest/webapp.py:2359` | query: refresh | — | 200 |
| GET | `/api/app/qr.png` | required | `qq_live_digest/webapp.py:2549` | query: base | {ok,error} | 200, 400, 500 |
| GET | `/api/backup` | required | `qq_live_digest/webapp.py:2259` | — | {ok,code,error} | 404, 500 |
| GET | `/api/chaoxing/cookie/status` | required | `qq_live_digest/webapp.py:2304` | — | {ok,has_cookie,last_checked_at,result,error} | 200 |
| GET | `/api/chaoxing/courses` | required | `qq_live_digest/webapp.py:2727` | — | {ok,error,courses} | 200, 400, 502 |
| GET | `/api/chaoxing/status` | required | `qq_live_digest/webapp.py:2742` | — | {ok,expired,last_sync_at,last_error,has_cookie,banner_text,login_url} | 200 |
| GET | `/api/community/counts` | required | `qq_live_digest/webapp.py:2182` | — | {ok,counts} | 200 |
| GET | `/api/dingtalk/groups` | required | `qq_live_digest/webapp.py:2660` | — | {ok,groups,suggest_source,suggest_error,category,error,detail} | 200 |
| GET | `/api/dingtalk/status` | required | `qq_live_digest/webapp.py:2655` | — | {ok,enabled,running,groups,state,scan_state,scan_stage,scan_started_at,scan_elapsed_seconds} | 200 |
| GET | `/api/donate/qr.png` | public | `qq_live_digest/webapp.py:2132` | — | {ok,error} | 200, 404 |
| GET | `/api/firewall/status` | required | `qq_live_digest/webapp.py:2769` | — | {ok,port,state,message,at,checked} | 200 |
| GET | `/api/groups/suggest` | required | `qq_live_digest/webapp.py:2448` | — | — | 200 |
| GET | `/api/health` | public | `qq_live_digest/webapp.py:2111` | — | {ok,service,channels} | 200 |
| GET | `/api/history/fetch/status` | required | `qq_live_digest/webapp.py:2605` | — | — | 200 |
| GET | `/api/history/floor` | required | `qq_live_digest/webapp.py:2589` | query: group_id | {ok,floor_ts,total_seen,error} | 200, 400, 503 |
| GET | `/api/hosting/status` | required | `qq_live_digest/webapp.py:2764` | — | {ok,error,user_qq_path,user_qq_pids,user_qq_running,napcat_running,napcat_online,napcat_login_error,hosting_active,hosting_uin,hosting_since,groups_selected} | 200 |
| GET | `/api/inbox` | required | `qq_live_digest/webapp.py:2613` | query: group_id, limit, offset, q, verdict | {ok,error,items,counts,total} | 200, 400 |
| GET | `/api/inbox/classify/status` | required | `qq_live_digest/webapp.py:2609` | — | — | 200 |
| GET | `/api/inbox/image` | required | `qq_live_digest/webapp.py:2314` | query: msg_id | {ok,error} | 404 |
| GET | `/api/insights` | required | `qq_live_digest/webapp.py:2865` | query: days | {stats,insights} | 200 |
| GET | `/api/lab/replies` | required | `qq_live_digest/webapp.py:2240` | query: limit | — | 200, 400, 403, 503 |
| GET | `/api/lab/search` | required | `qq_live_digest/webapp.py:2206` | query: limit, q | — | 200, 400, 403, 503 |
| GET | `/api/lab/status` | required | `qq_live_digest/webapp.py:2198` | — | {ok,result,enabled} | 200 |
| GET | `/api/meta` | required | `qq_live_digest/webapp.py:2887` | — | {groups,channels,reminders,started_at,insights,push_budget,quiet_hours,channel,push,push_help,llm,providers,stats} | 200 |
| GET | `/api/napcat/doctor` | required | `qq_live_digest/webapp.py:2443` | — | {ok,steps,broken,hint,error} | 200 |
| GET | `/api/napcat/groups` | required | `qq_live_digest/webapp.py:2463` | — | {ok,groups,error} | 200 |
| GET | `/api/napcat/qrcode` | required | `qq_live_digest/webapp.py:2473` | — | {ok,error} | 200, 404 |
| GET | `/api/napcat/status` | required | `qq_live_digest/webapp.py:2402` | — | {ok,online,good,user_id,nickname,napcat_installed,napcat_root,qr_stamp,qr_age_seconds,qr_stale,napcat_running,stage,waiting_seconds,error} | 200 |
| GET | `/api/notices` | required | `qq_live_digest/webapp.py:2849` | — | {items} | 200 |
| GET | `/api/onboarding` | public | `qq_live_digest/webapp.py:2144` | — | {required,accepted_at} | 200 |
| GET | `/api/onboarding/next` | public | `qq_live_digest/webapp.py:2150` | query: skip | {ok,step,title,body,reason,state,action,action_label,blocked,skippable,done,detail,skipped,index,total} | 200 |
| GET | `/api/onboarding/status` | public | `qq_live_digest/webapp.py:2156` | — | {ok,state,action,message,error} | 200 |
| GET | `/api/overview` | required | `qq_live_digest/webapp.py:2882` | — | {ok,sources} | 200 |
| GET | `/api/pair/info` | required | `qq_live_digest/webapp.py:2775` | — | {ok,public_url,public_enabled,tailscale_ready,base,lan,candidates,qr_error,firewall,steps,no_public_hint} | 200 |
| GET | `/api/pair/pending` | required | `qq_live_digest/webapp.py:2811` | — | {ok,pending} | 200 |
| GET | `/api/pair/status` | public | `qq_live_digest/webapp.py:2161` | query: code, secret | {ok,status,token,error} | 200, 404 |
| GET | `/api/qq/groups/status` | required | `qq_live_digest/webapp.py:2298` | — | {ok,last_attempt_at,last_success_at,group_count,last_error,is_fetching,next_run_at} | 200 |
| GET | `/api/selfcheck` | required | `qq_live_digest/webapp.py:2407` | — | {ok,items,last_push_at} | 200 |
| GET | `/api/settings` | required | `qq_live_digest/webapp.py:2747` | — | — | 200 |
| GET | `/api/setup/status` | required | `qq_live_digest/webapp.py:2309` | — | {ok,platforms} | 200 |
| GET | `/api/sync/info` | required | `qq_live_digest/webapp.py:2490` | — | — | — |
| GET | `/api/sync/qr.png` | required | `qq_live_digest/webapp.py:2524` | query: text | {ok,error} | 200, 400, 500 |
| GET | `/api/sync/test` | required | `qq_live_digest/webapp.py:2496` | query: url | {ok,events,url,error} | 200, 400, 502 |
| GET | `/api/tasks` | required | `qq_live_digest/webapp.py:2816` | — | {headline,subline,progress,stats,pinned,candidates,today,week,later,done} | 200 |
| GET | `/api/tunnel/status` | required | `qq_live_digest/webapp.py:2806` | — | — | 200 |
| GET | `/api/update` | required | `qq_live_digest/webapp.py:2366` | query: check | — | 200 |
| GET | `/api/wechat/groups` | required | `qq_live_digest/webapp.py:2695` | — | {ok,groups,suggest_source,suggest_error,error} | 200 |
| GET | `/api/wechat/status` | required | `qq_live_digest/webapp.py:2690` | — | {ok,enabled,running,available,groups,interval_seconds,last_read_at,last_read_seconds,last_error,error_category,error_hint,manual_key,state,hint,scan_state,scan_stage,scan_started_at,scan_elapsed_seconds} | 200 |
| GET | `/calendar` | required | `qq_live_digest/webapp.py:2579` | — | — | 302 |
| GET | `/calendar.ics` | required | `qq_live_digest/webapp.py:2584` | — | — | 200 |
| GET | `/floatball` | required | `qq_live_digest/webapp.py:2373` | — | — | — |
| GET | `/floatball.html` | required | `qq_live_digest/webapp.py:2373` | — | — | — |
| GET | `/health` | public | `qq_live_digest/webapp.py:2111` | — | {ok,service,channels} | 200 |
| GET | `/icon.svg` | public | `qq_live_digest/webapp.py:2122` | — | — | 200 |
| GET | `/index.html` | required | `qq_live_digest/webapp.py:2380` | — | — | 200 |
| GET | `/manifest.webmanifest` | public | `qq_live_digest/webapp.py:2117` | — | — | 200 |
| GET | `/setup` | required | `qq_live_digest/webapp.py:2397` | — | — | 302 |
| GET | `/sw.js` | public | `qq_live_digest/webapp.py:2127` | — | — | 200 |
| POST | `/api/chaoxing/auto_cookie` | required | `qq_live_digest/webapp.py:3027` | — | {ok,saved,source,courses,attempts,category,error,detail,next_step} | 200 |
| POST | `/api/chaoxing/cookie/validate` | required | `qq_live_digest/webapp.py:3022` | — | {ok,has_cookie,last_checked_at,result,error} | 200 |
| POST | `/api/dingtalk/scan` | required | `qq_live_digest/webapp.py:3123` | — | {ok,started,source,scan_state,scan_stage,scan_elapsed_seconds,error,category,detail} | 200 |
| POST | `/api/firewall/allow` | required | `qq_live_digest/webapp.py:3206` | — | {ok,state,request_id,port,error,needs_confirm} | 200, 400, 403 |
| POST | `/api/history/fetch` | required | `qq_live_digest/webapp.py:3337` | —；body: groups, since, until | {ok,started,error} | 200, 400, 409, 503 |
| POST | `/api/history/fetch/cancel` | required | `qq_live_digest/webapp.py:3322` | — | {ok,cancelled,error} | 200, 409 |
| POST | `/api/hosting/start` | required | `qq_live_digest/webapp.py:3405` | —；body: uin | — | 200, 400 |
| POST | `/api/hosting/stop` | required | `qq_live_digest/webapp.py:3405` | —；body: uin | — | 200, 400 |
| POST | `/api/inbox/classify` | required | `qq_live_digest/webapp.py:3356` | —；body: groups, limit, since, until | {ok,started,error} | 200, 400, 409, 503 |
| POST | `/api/inbox/promote` | required | `qq_live_digest/webapp.py:3378` | —；body: due, msg_id, title | {ok,task_id,error} | 200, 400, 503 |
| POST | `/api/lab/events` | required | `qq_live_digest/webapp.py:2987` | —；body: msg_id, text | — | 200, 400, 403, 503 |
| POST | `/api/lab/replies/ack` | required | `qq_live_digest/webapp.py:2987` | —；body: msg_id, text | — | 200, 400, 403, 503 |
| POST | `/api/napcat/autosetup` | required | `qq_live_digest/webapp.py:3517` | — | {ok,steps,qrcode_path,uin,applied_via,restarted,waiting_for_login,restart_required,error} | 200 |
| POST | `/api/napcat/install` | required | `qq_live_digest/webapp.py:3462` | — | {ok,installed,already_installed,root,version,bytes,error} | 200 |
| POST | `/api/napcat/launch` | required | `qq_live_digest/webapp.py:3473` | —；body: uin | {ok,already_running,pid,command,profile_dir,boot,error,onebot_config} | 200, 400 |
| POST | `/api/napcat/restart` | required | `qq_live_digest/webapp.py:3495` | —；body: uin | {ok,stopped,already_stopped,pid,api_up,waiting_for_login,error} | 200, 400 |
| POST | `/api/onboarding/accept` | public | `qq_live_digest/webapp.py:2950` | — | {ok,accepted_at,error} | 200, 400 |
| POST | `/api/onboarding/run` | required | `qq_live_digest/webapp.py:3170` | — | — | 403 |
| POST | `/api/pair/approve` | required | `qq_live_digest/webapp.py:3160` | —；body: code | {ok,status,error} | 200, 400 |
| POST | `/api/pair/deny` | required | `qq_live_digest/webapp.py:3165` | —；body: code | {ok,status,error} | 200, 400 |
| POST | `/api/pair/public` | required | `qq_live_digest/webapp.py:3181` | — | — | 200, 400, 403 |
| POST | `/api/pair/start` | public | `qq_live_digest/webapp.py:2977` | — | — | 200, 400 |
| POST | `/api/settings` | required | `qq_live_digest/settings_api.py:291` | — | {ok,error} | 200, 400 |
| POST | `/api/settings/test-llm` | required | `qq_live_digest/settings_api.py:220` | — | {ok,model,reply,seconds,error,endpoint} | 200 |
| POST | `/api/settings/test-push` | required | `qq_live_digest/settings_api.py:251` | — | {ok,results,error} | 200 |
| POST | `/api/subscriptions` | required | `qq_live_digest/webapp.py:3426` | —；body: aliases, groups | {ok,groups,applied,error} | 200, 400 |
| POST | `/api/tasks/create` | required | `qq_live_digest/webapp.py:3275` | —；body: deadline, summary | {ok,task_id,summary,deadline,error} | 200, 400, 500 |
| POST | `/api/tasks/pin` | required | `qq_live_digest/webapp.py:3250` | —；body: pinned, task_id | — | 200, 400, 404, 500 |
| POST | `/api/tasks/urgent` | required | `qq_live_digest/webapp.py:3228` | —；body: task_id, urgent | — | 200, 400, 404, 500 |
| POST | `/api/tunnel/start` | required | `qq_live_digest/webapp.py:3181` | — | — | 200, 400, 403 |
| POST | `/api/update/apply` | required | `qq_live_digest/webapp.py:3457` | — | {ok,restarting,script,error} | 200 |
| POST | `/api/update/download` | required | `qq_live_digest/webapp.py:3452` | — | {ok,started,error} | 200 |
| POST | `/api/wechat/scan` | required | `qq_live_digest/webapp.py:3123` | — | {ok,started,source,scan_state,scan_stage,scan_elapsed_seconds,error,category,detail} | 200 |
| GET | `/js/*` | public | `qq_live_digest/webapp.py:2917` | — | — | 200 |
| POST | `/api/tasks/<id>` | required | `qq_live_digest/webapp.py:3530` | body: action, correction, done, value | {ok,id,action,done,status,error} | 200, 400, 404 |


## 通用错误响应

- 未授权（`Accept: text/html`）：`401` + 一段人话 HTML（`webapp.py:2936-2942`）
- 未授权（其他）：`401` + `{"ok": false, "error": "invalid token"}`
- 未命中：`404` + `{"ok": false, "error": "not found"}`（`webapp.py:2947`）
- 请求体超限：见 `_reject_oversized_body`（`MAX_BODY_BYTES`）
- 请求体不是 JSON 对象：`400` + `{"ok": false, "error": "bad json"}`

## 重写时最容易漏的三处

1. **`/api/settings` GET+POST 同名不同方法**，去重计数会少一个。
2. **`/js/*` 与 `/api/tasks/<id>` 两条前缀路由**不在字面量表里，纯遍历 dict 会漏。
3. **`/api/hosting/stop` 与 `/api/hosting/start` 共用一个 handler 名**，
   靠 body 区分 —— 这是现存契约瑕疵，重写要么改对，要么显式记录。
