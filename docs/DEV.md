# 开发者说明

核对日期：2026-11-16。路径从仓库根目录算起。阅读顺序：`main.py` → `noticehub/bootstrap/cli.py` → `noticehub/delivery/service.py` → 本文第二节；查具体改动用第三、四节。

## 1. 跑起来

源码服务使用 Python；macOS 安装脚本明确检查 Python 3.10 以上。Windows 桌面托盘、微信本地读取、防火墙及计划任务依赖 Windows API，不能把服务可运行等同于这些功能跨平台可用。

`requirements.txt` 当前列出 qq-botpy==1.2.1、openpyxl>=3.1,<4、pillow>=10,<13、pystray==0.19.5、pypdf>=5,<7、pypdfium2>=5,<6。浏览器验证另外需要 Playwright、已安装的 Edge；JS 检查需要 Node。打包需要 PyInstaller。

在仓库目录准备环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

配置由 `noticehub/bootstrap/settings.py:build_settings` 装配；先读 `.env.example` 的配置说明，再准备自己的 `.env`。不要覆盖已有配置。以下命令中的 `python` 在本机应替换为 `.\.venv\Scripts\python.exe -X utf8`。

`main.py:_COMMANDS` 注册的完整子命令如下；全局 `--env <路径>` 放在子命令前。

| 命令 | 用途和副作用 |
| --- | --- |
| `python main.py run` | 启动接收器、网页及调度循环；`--poll` 改轮询间隔。会写库、日志，按配置联网和推送。 |
| `python main.py tick` | 执行一次调度；`--no-bot` 不启动官方 Bot。可能补采、建待办和推送。 |
| `python main.py preview` | 预览未处理消息的摘要；`--limit` 限数量。 |
| `python main.py stats` | 输出本地消息、摘要、投递等统计。 |
| `python main.py show` | 查看最近摘要；`--limit` 限数量。 |
| `python main.py tasks` | 查看待办；`--all`、`--limit` 控制显示；`--import-existing`、`--days` 导入历史摘要，`--reset` 清理后重建，后两类会写库。 |
| `python main.py catchup` | 从 NapCat 补采订阅群历史，写入消息库。 |
| `python main.py doctor` | 配置及本地状态诊断；`--online` 增加联网探测，`--bundle` 生成脱敏诊断包。 |
| `python main.py send-test` | 真发测试推送；`--wait` 控制等待。 |
| `python main.py attach-test <path>` | 用本地附件走提取/摘要链路；支持 `--group`、`--file-id`、`--busid`、`--name`。 |

入口实现都在 `noticehub/bootstrap/cli.py:command_*`；`main.py:build_parser` 负责参数，不在这里加业务。

测试从仓库根运行。先确认没有另一批 Python 验收占用临时目录；只跑本次改动对应模块，同一组运行两轮，各自保存退出码。下面是专项示例，不是要求每次全跑：

```powershell
$env:TEMP = Join-Path $PWD 'nh-tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_receiver tests.test_service
$r1 = $LASTEXITCODE
.\.venv\Scripts\python.exe -X utf8 -m unittest tests.test_receiver tests.test_service
$r2 = $LASTEXITCODE
"R1=$r1 R2=$r2"
```

浏览器专项必须先设 `$env:NH_REQUIRE_BROWSER='1'`，再用同一项目解释器运行 `-m unittest tests.test_ui_browser`。系统 Python 缺 Playwright 时可能整类跳过；全 skipped 不算通过。`tests/test_ui_browser.py` 使用已有 Edge，不需要下载 Chromium。不要在用户实例上跑冷装脚本。

## 2. 一条 QQ 消息的旅程

下面是 NapCat 的 HTTP OneBot 路径；官方 Bot 是另一入口 `noticehub/qq/bot.py:LiveBotClient`，最终也调用服务的消息回调。

| 顺序 | 文件:入口 | 输入 → 输出；失败行为 |
| --- | --- | --- |
| 1 | `noticehub/receivers/receiver.py:_Handler.do_POST` | HTTP body → JSON 事件。读取失败 400、鉴权失败 403、JSON 无效 400；通过后交给接收器回调。 |
| 2 | `noticehub/receivers/receiver.py:OneBotReceiver.handle_payload` | OneBot payload → 规范记录。先分派附件，再调用 QQ 适配器；附件异常记日志，不阻断文字。非消息、白名单外返回 204；有效消息交给 `on_message`，响应中报告 inserted。 |
| 3 | `noticehub/adapters/qq/onebot.py:parse_onebot_message`、`onebot_record` | 平台字段/消息段 → `MessageEvent` → service 使用的字典；不支持的事件返回 None。`flatten_message` 处理消息段。 |
| 4 | `noticehub/delivery/service.py:DigestService.on_message` → `noticehub/application/ingest.py:accept_message` | 字典 → `MessageEvent.from_mapping` → `Store.insert_message`。无消息 ID、白名单外、重复消息返回 False；入库异常记录后返回 False。只对首次插入消息执行规则。 |
| 5 | `noticehub/application/ingest.py:apply_rules` → `noticehub/domain/rules.py:match` | 消息 + 用户规则 → 候选待办；`Store.upsert_task` 写 status=candidate、source=rule。规则读取/匹配失败记日志；单条待办失败不撤销原消息。 |
| 6 | `noticehub/delivery/service.py:DigestService.tick` → `noticehub/application/schedule.py:tick` | `Store.unprocessed_messages` → `summarizer.analyse_records`。先做维护、补采、提醒和快照；紧急项单独发布，普通项受窗口、批量上限、静默时段及推送闸门控制。未到窗口继续留作未处理。 |
| 7 | `noticehub/ingest/summarizer.py:build_digest` → `DigestService._publish` | 分析项 → 摘要。LLM 临时错误在延后预算内保留未处理消息；预算耗尽用本地规则。没有重点的批次记 silent 摘要并标 processed。 |
| 8 | `noticehub/delivery/service.py:DigestService._publish` → `noticehub/application/schedule.py:sync_tasks` | 摘要先 `insert_digest`、再 `mark_processed`；合并事件、资格过滤、分类后 `Store.upsert_task`。晨报/晚报不建待办；同步异常记日志但继续推送。 |
| 9 | `noticehub/infrastructure/notify.py:PushManager.send_digest`、`_dispatch` | 摘要 → 各 `Pusher.send`。先 claim 投递去重；tier 0 全部失败才试 tier 1；成功记 sent、异常记 failed。无通道时摘要仍归档。 |
| 10 | `noticehub/delivery/service.py:DigestService._retry` → `PushManager.retry_pending` | failed 投递按次数及冷却间隔重试；摘要不存在或通道已移除时记失败，不凭空重建发送目标。异常记录后返回空结果。 |
| 11 | `noticehub/web/webapp.py:_Handler._route_get_api_tasks` → `noticehub/application/tasks.py:list_task_board` | 浏览器 GET `/api/tasks` → Store 待办 → 分组及统计 JSON。它读已经落库的待办，不重新调用模型。 |
| 12 | `noticehub/web/web_static/page.html:loadTasks`、`render` | JSON → 今日/本周/候选/已完成等页面。操作经 `/api/tasks/<id>` 等 POST 回到应用层和 Store；页面刷新读取新状态。 |

定位丢消息时先区分：HTTP 是否接收、messages 是否落库、processed 是否标记、tasks 是否生成、deliveries 是否成功。HTTP 200 不能证明后四步成功。

## 3. 模块清单

### 根入口与 noticehub 顶层文件

| 路径 | 干什么 | 关键入口（函数/类名） | 谁调它 | 动它之前要知道 |
| --- | --- | --- | --- | --- |
| `main.py` | CLI 参数和命令分派 | `build_parser` | 人/启动脚本 | 新命令同时补 bootstrap CLI 与测试。 |
| `qq_digest.py` | 消息解析、评分、截止时间、去重、导出文本及模型整理 | `Message`、`analyze_message`、`extract_deadline`、`run_codebuddy` | summarizer、离线调用 | 是现有分析实现，不另起一套同义规则。 |
| `noticehub/__init__.py` | 包入口 | 模块导入 | Python | 不放服务启动副作用。 |
| `noticehub/web/announce.py` | 拉取、缓存、按版本/通道筛公告 | `fetch`、`parse`、`active` | updates_api | 请求失败和真正无公告要区分。 |
| `noticehub/ingest/attachments.py` | 附件识别、格式提取、安全下载、文档/图片模型调用 | `Attachment`、`extract_text`、`download`、`summarize_document` | application/attachments | 有大小、ZIP、地址安全边界；保留失败路径。 |
| `noticehub/qq/bot.py` | 官方 QQ Bot 事件和生命周期 | `LiveBotClient`、`BotRunner` | service | 与 NapCat 不是同一接入；回调不能拖垮 Bot。 |
| `noticehub/receivers/browser_cookie.py` | 从浏览器快照提取学习通 cookie | `read_chaoxing_cookie` | webapp | 涉及 DPAPI/AES、锁库与隐私；不要记录 cookie。 |
| `noticehub/ingest/catchup.py` | NapCat HTTP 客户端 | `NapCatClient`、`NapCatError` | service、application/history | 处理响应错误，不把空结果当连通。 |
| `noticehub/receivers/chaoxing_receiver.py` | 学习通课程、通知和作业采集 | `ChaoxingClient`、`ChaoxingReader`、`chaoxing_status` | service、webapp | cookie 过期和网络失败有不同状态。 |
| `noticehub/remote/cloudflare.py` | cloudflared 下载与快速通道进程 | `ensure_binary`、`QuickTunnel` | webapp | 有 URL 不代表可访问；包含探测和退出状态。 |
| `noticehub/web/community.py` | 可关闭的社区计数 | `ping`、`fetch_counts` | webapp | 网络异常不能冒到主业务；遵守 telemetry 开关。 |
| `noticehub/platform/config.py` | Settings 数据、dotenv 读写、值校验 | `Settings`、`parse_env_file`、`update_env_file` | bootstrap、settings_api | 原子写配置；进程环境优先于 dotenv。 |
| `noticehub/platform/config_audit.py` | 报告配置值来源及脱敏状态 | `audit_config` | webapp | 只使用已算好的探测快照，不新增宿主写操作。 |
| `noticehub/receivers/dingtalk_receiver.py` | 钉钉本地库/WAL 快照、群过滤与增量读取 | `DingTalkLocalReader`、`parse_dingtalk_content` | service | 只读源库，盐/代际变化要重建快照。 |
| `noticehub/update/doctor.py` | 诊断资料脱敏及打包 | `sanitize`、`build_bundle` | CLI doctor | 不把 token、cookie、原始敏感信息装进包。 |
| `noticehub/ingest/event_extract.py` | 文本事件时间、地点及动作抽取 | `extract_events` | lab、service 合并链路 | 纯抽取结果仍受资格与置信度限制。 |
| `noticehub/floatball/floatball_host.py` | 浮球生命周期、动画、数据刷新和动作 | `FloatBallHost` | launcher | 含线程、全屏抑制、位置持久化及失败兜底。 |
| `noticehub/floatball/floatball_page.py` | 生成浮球网页内容 | `render` | webapp | 和原生浮球渲染文件不是一份实现。 |
| `noticehub/floatball/floatball_render.py` | Pillow 绘制球、面板及过渡帧 | `PanelView`、`render_ball`、`render_panel`、`hit_test` | floatball_host | 命中几何必须与绘制一致。 |
| `noticehub/floatball/floatball_win.py` | Win32 分层窗口、鼠标/滚轮及 GDI | `FloatBallWindow`、`anchor_xy`、`alpha_hit` | floatball_host | DPI、ctypes 签名和资源释放不可省。 |
| `noticehub/qq/group_suggest.py` | 群用途建议，模型失败时关键词回落 | `suggest`、`suggest_for_source` | webapp | 来源 namespace 与缓存不能串群。 |
| `noticehub/remote/host_actions.py` | 提权、防火墙命令与宿主动作审计 | `run_elevated`、`firewall_allows`、`audit_host_action` | hosting、webapp、launcher | 会改宿主；区分确认、执行和验证。 |
| `noticehub/remote/hosting.py` | QQ/NapCat 托管、恢复 QQ、自启偏好 | `start`、`stop`、`hosting_status` | webapp、launcher | 进程必须按安装目录识别，状态查询缓存写后失效。 |
| `noticehub/ingest/ics.py` | 待办转 ICS | `render_calendar` | application/calendar | 上海时间、转义与折行有专项测试。 |
| `noticehub/web/inbox.py` | 收件箱分类与提升为待办 | `classify_messages`、`promote` | webapp/application | 支持取消；判定与任务生成是不同动作。 |
| `noticehub/platform/lab.py` | 实验能力的开关/可用性入口 | `available`、`search`、`extract_events`、`call` | webapp | 关闭时不能偷偷执行实验能力。 |
| `noticehub/platform/llm_json.py` | 模型返回 JSON 解析 | `parse_json` | 模型调用方 | 错误输出不能当空成功。 |
| `noticehub/platform/logging_setup.py` | 控制台和轮转文件日志 | `setup_logging` | bootstrap/cli | 避免重复 handler 与 Windows 编码错误。 |
| `noticehub/qq/messages.py` | 用户可见共用错误/引导文案 | `QQ_NOT_FOUND_GUIDE`、`CHAOXING_COOKIE_EXPIRED` | 宿主及来源模块 | 页面和 docs 仍有副本；修改时核对重复文案。 |
| `noticehub/qq/napcat_admin.py` | NapCat 发现、安装、配置、启动、停止、二维码和诊断 | `install`、`launch`、`stop`、`auto_setup`、`diagnose` | webapp、hosting | 下载哈希、目录归属、空壳进程及真探活都是契约。 |
| `noticehub/qq/pairing.py` | 手机配对请求、批准及限流 | `start`、`status`、`pending`、`decide` | webapp | 配对码与 secret 配套，过期/限流不能绕过。 |
| `noticehub/delivery/push.py` | 推送协议实现及组装 | `Pusher`、`build_pushers`、`WebhookPusher` | service、PushManager | transport 负责发送，投递状态在 notify/Store。 |
| `noticehub/qq/qr.py` | 二维码矩阵及 PNG | `matrix`、`png_bytes` | webapp | 容错等级、掩模与边界有测试。 |
| `noticehub/receivers/receiver.py` | OneBot HTTP 鉴权、body 限制及回调 | `OneBotReceiver` | service | 平台字段转换在 adapters/qq。 |
| `noticehub/platform/retry.py` | LLM 错误分类及有限重试 | `call_with_retries`、`llm_should_retry` | 模型调用方 | 非临时错误不要无限重试。 |
| `noticehub/web/routes.py` | GET/POST 路由表、模式及鉴权标记 | `match_pattern` | webapp._Handler | 新接口须显式决定是否鉴权。 |
| `noticehub/ingest/rules.py` | 用户规则 JSON 存取、校验、试跑 | `load_rules`、`save_rules`、`test_rules` | ingest、lab | 纯匹配在 domain/rules，不混入落盘。 |
| `noticehub/web/search.py` | FTS5 trigram 索引及短查询 LIKE 回落 | `ensure_index`、`search`、`MessageSearch` | lab | 少于 3 字符走 LIKE；删消息后索引需重建才能缩小。 |
| `noticehub/update/selfcheck.py` | 功能状态转自检项 | `feature_items`、`check` | webapp | 输入快照不等于重新执行安装/连接。 |
| `noticehub/delivery/service.py` | 组件装配、生命周期、调度宿主、提醒与状态 | `DigestService` | bootstrap/cli | 部分业务已委托 application，别再复制回此文件。 |
| `noticehub/web/settings_api.py` | 设置写接口、校验、脱敏及 reader 重载 | `SettingsPostMixin` | webapp._Handler | 写 dotenv 后要更新运行态并失效相应缓存。 |
| `noticehub/infrastructure/sqlite/store.py` | Store 兼容导出 | `Store` | 旧导入者 | 实现位于 infrastructure/sqlite/store.py。 |
| `noticehub/ingest/summarizer.py` | 分析记录、摘要、待办分类与推送文本 | `Digest`、`analyse_records`、`build_digest`、`classify_task_item` | schedule、service | 本地回落也要保留来源和截止时间。 |
| `noticehub/platform/timeutil.py` | 当前本地时间及时间值归一化 | `now_local`、`to_naive_local`、`iso`、`parse_iso` | 多模块 | 当前仍含真实实现，不是纯再导出；另有 domain/time。 |
| `noticehub/platform/ttl_cache.py` | 线程锁保护的 TTL 读缓存 | `get`、`clear` | 探测调用方 | 写动作后必须 clear，空结果缓存有显式选项。 |
| `noticehub/remote/tunnel.py` | Tailscale Funnel 安装/登录/开关及公网探测 | `FunnelSetup`、`enable_funnel`、`probe_public` | webapp | 会启动进程/改 serve 配置；失败要回状态。 |
| `noticehub/update/update_signing.py` | RS256 清单验签 | `canonical_payload`、`verify_manifest_signature` | updater | 签名输入规范与失败关闭必须保持。 |
| `noticehub/update/update_sources.py` | 更新/公告源 URL | `sources`、`manifest_urls` | updater、announce | 镜像仓库和官方源的构造在这里。 |
| `noticehub/update/updater.py` | 检查、下载校验、pending、异步安装与回滚 | `check`、`download`、`apply_async`、`rollback_async` | updates_api、launcher | 会替换安装文件；保留数据与校验链不能省。 |
| `noticehub/web/updates_api.py` | 公告/更新缓存、下载任务和网页响应 | `_update_response`、`_start_update_download`、`_apply_update` | webapp | 下载完成、应用中与应用成功是不同状态。 |
| `noticehub/platform/version.py` | 应用版本常量 | 模块常量 | 更新器、状态接口 | 修改时用版本一致性检查，不能只改此处。 |
| `noticehub/web/webapp.py` | HTTP server、鉴权、路由适配、静态页面和后台任务 | `TaskWebServer`、`_Handler` | service | 大文件仍含旧页面、进程探测和全局任务状态。 |
| `noticehub/web/weburl.py` | 网页地址和局域网地址生成 | `build_web_url`、`detect_lan_ip` | service/调用方 | 不能把 0.0.0.0 直接给手机使用。 |
| `noticehub/receivers/wechat_receiver.py` | 微信存储发现、密钥解析、只读解密及增量扫描 | `WeChatLocalReader`、`resolve_key`、`fetch_messages`、`main` | service、webapp、CLI | 涉及用户进程/隐私；空白名单应拒绝采集。 |
| `noticehub/floatball/win_text.py` | Windows 子进程字节解码 | `decode_console_output` | 宿主探测代码 | 控制台编码不是固定 UTF-8。 |

### 子包逐文件

| 路径 | 干什么 | 关键入口（函数/类名） | 谁调它 | 动它之前要知道 |
| --- | --- | --- | --- | --- |
| `noticehub/bootstrap/__init__.py` | 装配包标识 | 模块导入 | Python | 不增加 import 时副作用。 |
| `noticehub/bootstrap/cli.py` | CLI 实际执行 | `command_run`、`command_tick`、`build_service` | main | 参数形状由 main 决定。 |
| `noticehub/bootstrap/settings.py` | 环境变量转 Settings | `build_settings`、`env_value`、`web_port_source` | config/CLI | 新前缀优先，空串也覆盖旧名。 |
| `noticehub/domain/__init__.py` | 领域包标识 | 模块导入 | Python | 保持无 IO。 |
| `noticehub/domain/message.py` | 统一消息数据 | `MessageEvent.from_mapping` | service/adapter | 字段变更要查所有入口。 |
| `noticehub/domain/events.py` | 抽取结果合并与等级拦截 | `merge_extracted_event`、`event_level_blocks` | service | 不能提升无证据字段。 |
| `noticehub/domain/eligibility.py` | 通知/待办资格及安静群过滤 | `is_task_eligible`、`is_notice_item` | schedule | 影响入任务边界，不只是 UI 过滤。 |
| `noticehub/domain/rules.py` | 规则解析、正则检查及纯匹配 | `Rule`、`parse_rules`、`match` | rules/ingest | 不读写规则文件。 |
| `noticehub/domain/time.py` | 不读取时钟的时间转换 | `to_naive_local`、`iso`、`parse_iso` | 领域调用者 | 与 timeutil 当前并存，先查实际导入。 |
| `noticehub/application/__init__.py` | 应用包标识 | 模块导入 | Python | 不启动任务。 |
| `noticehub/application/attachments.py` | 附件队列、后台处理、原图保存 | `AttachmentWorker`、`save_image`、`pick_image` | service、webapp | 队列失败/限速/清理不能漏反馈。 |
| `noticehub/application/calendar.py` | 日历地址排序、订阅描述和 Store→ICS | `assemble_calendars`、`render_calendar_ics` | webapp | 公网和虚拟网卡优先级有契约。 |
| `noticehub/application/history.py` | 历史消息转换、范围补采 | `backfill`、`backfill_range`、`run_catchup` | service/webapp | 重复消息不重复建待办。 |
| `noticehub/application/inbox_item.py` | 收件箱提升用例 | `promote_inbox_item` | webapp | 保留来源与输入校验。 |
| `noticehub/application/ingest.py` | 消息首次落库及规则候选 | `accept_message`、`apply_rules` | service | 插入成功才执行规则。 |
| `noticehub/application/jobs.py` | 历史/分类后台任务的参数和状态适配 | `start_history_backfill`、`start_inbox_classify`、`cancel_job` | webapp | 取消与完成状态不要混淆。 |
| `noticehub/application/notices.py` | 最近推送展示数据与旧正文清理 | `list_notices` | webapp | 正文限长、群名恢复和重复动作清理。 |
| `noticehub/application/push_test.py` | 推送通道测试用例 | `test_push_channels` | settings_api | 会真发送，不是配置语法检查。 |
| `noticehub/application/schedule.py` | 批次调度和摘要同步待办 | `tick`、`sync_tasks`、`import_existing_tasks` | service | 使用 service 注入的依赖；别自行创建第二个 Store。 |
| `noticehub/application/subscriptions.py` | 保存订阅群 | `save_subscriptions` | webapp | 配置与群名同步。 |
| `noticehub/application/tasks.py` | 待办输入验证、操作、分组和概览 | `create_task`、`apply_task_action`、`list_task_board` | webapp | 持久化字段在 SQLite 层。 |
| `noticehub/ports/__init__.py` | 端口包标识 | 模块导入 | Python | 不放具体平台实现。 |
| `noticehub/ports/clock.py` | 时钟协议 | `Clock` | 依赖注入 | 测试可替换时钟。 |
| `noticehub/ports/notifier.py` | 推送管理协议 | `Notifier` | service | 保持与 PushManager 调用形状一致。 |
| `noticehub/infrastructure/__init__.py` | 基础设施包标识 | 模块导入 | Python | 不隐式建库。 |
| `noticehub/infrastructure/clock.py` | 系统时钟实现 | `SystemClock.now` | service | 时间转换仍要遵守本地 naive 约定。 |
| `noticehub/infrastructure/notify.py` | 分层发送、去重和失败重试 | `PushManager` | service | 投递记录是重试依据。 |
| `noticehub/infrastructure/sqlite/__init__.py` | SQLite 包标识 | 模块导入 | Python | 具体实现看 store。 |
| `noticehub/infrastructure/sqlite/store.py` | schema、迁移、事务、消息/待办/投递存储 | `Store` | service、应用层 | 坏库隔离、并发 upsert 和兼容字段均有测试。 |
| `noticehub/infrastructure/sqlite/deliveries.py` | 投递仓储适配 | `DeliveryRepository` | PushManager | 委托 Store，不另建投递表。 |
| `noticehub/adapters/__init__.py` | 外部适配包标识 | 模块导入 | Python | 不增加平台依赖启动。 |
| `noticehub/adapters/qq/__init__.py` | QQ 适配包标识 | 模块导入 | Python | 转换实现在 onebot。 |
| `noticehub/adapters/qq/onebot.py` | OneBot/官方 QQ 事件转统一消息 | `parse_onebot_message`、`qqbot_group_event` | receiver、bot | 适配器只转字段，白名单/落库看 ingest。 |

### 网页静态文件

| 路径 | 干什么 | 关键入口（函数/类名） | 谁调它 | 动它之前要知道 |
| --- | --- | --- | --- | --- |
| `noticehub/web/web_static/page.html` | 主页面结构、CSS 与多数交互 | `loadTasks`、`render`、`loadInbox` | webapp.PAGE_HTML | 单文件巨页；改动需浏览器验证，服务导入时读入。 |
| `noticehub/web/web_static/js/settings.js` | 设置表单、更新、推送/来源保存 | `loadSettings`、`savePushSettings`、`refreshUpdate` | page script | 处理刷新竞争与未保存输入；修改要查 API payload。 |
| `noticehub/web/web_static/js/guide.js` | QQ 引导、二维码、群选择 | `loadGroups`、`refreshQr` | page script | 无引擎时不能显示旧二维码。 |
| `noticehub/web/web_static/js/pair.js` | 配对及防火墙状态 | `refreshPair` | page script | 配对是独立鉴权流程。 |
| `noticehub/web/web_static/js/announce.js` | 公告、铃铛、面板及公告弹窗 | `refreshAnnouncements`、`renderAnnouncePanel` | page script | 面板尺寸变化也要重定位，不能只在 render 时算。 |
| `noticehub/web/web_static/js/notice.js` | 通知展示相关脚本 | 脚本加载 | page script | 与 page 内通知渲染接线一起检查。 |
| `noticehub/web/web_static/js/lab-rules.js` | 规则表单映射和关键词控件 | 脚本加载 | settings | 真实 JS 由 test_lab_rules_form 执行。 |
| `noticehub/web/web_static/manifest.webmanifest` | PWA 名称、图标和启动属性 | JSON 文档 | webapp/PWA | 随 PyInstaller 整目录收集。 |
| `noticehub/web/web_static/icon.svg` | 网页/PWA 图标 | SVG 文档 | webapp/PWA | 修改后注意离线壳缓存。 |

### 工具、平台脚本和包装

当前 tools 顶层是 14 个脚本加 README，共 15 个文件；没有第 15 个脚本。目录内缓存不是脚本源。当前未发现 scripts 源目录，Windows 脚本按 windows 路径列出。

| 路径 | 干什么 | 关键入口（函数/类名） | 谁调它 | 动它之前要知道 |
| --- | --- | --- | --- | --- |
| `tools/announce.py` | 公告列表/添加/移除/校验 | `cmd_list`、`cmd_add`、`cmd_remove`、`cmd_validate` | 维护者 | 写 announcements.json，不能当只读检查全运行。 |
| `tools/cleanup-install.ps1` | 清理安装残留 | 参数 `-Apply`、`-WhatIf` | 维护者/包 | 默认先审清单；作用目录不可指错。 |
| `tools/e2e-install.ps1` | 解包、启动、探活及升级保留检查 | `-Zip`、`-InstallDir`、`-Overwrite` | gate/验收者 | 可删整个目标目录；只能一次性目录，退出后收尾进程。 |
| `tools/e2e-render.ps1` | Node 语法与 Edge 真渲染 | `-Url`、`-InstallDir` | gate/验收者 | 读实例 token；标题检查证明 JS 跑过。 |
| `tools/gate.ps1` | 分层验收、包时效、清单检查 | `Test-ArtifactFreshness` | 发布流程 | 输入列表要与 build 保持同步；现有差异见第六节。 |
| `tools/packcheck.py` | 检查 PyInstaller 包内代码 | `load_pyz`、`inspect`、`main` | 维护者 | 依赖实际构建产物，不能证明当前源码已被打包。 |
| `tools/run-hidden.vbs` | 无窗口拉起 PowerShell | WScript 参数 | 计划任务 | 异步 shell.Run，不是进程存活证明。 |
| `tools/syntaxcheck.py` | AST、PS parser、内联 JS 语法检查 | 顶层执行 | gate/维护者 | 会 import webapp，Node 不在时 JS 检查会跳过。 |
| `tools/tray-smoke.ps1` | 真实桌面托盘冒烟 | `-NoUi`、`-KeepTemp` | 本机验收 | 无交互桌面返回跳过；不要游戏期间运行。 |
| `tools/verify_manifest.py` | 更新清单格式/URL 校验 | `check`、`main` | gate | 测试用临时清单，不覆盖正式文件。 |
| `tools/version_bump.py` | 版本多落点计划、写入和检查 | `check`、`apply`、`main` | 发布者 | `--set` 会写文件，`--check` 检查一致性。 |
| `tools/watchdog-task.ps1` | 计划任务定期检查并拉服务 | `-InstallRoot` | Windows 计划任务 | 只认本目录进程；手动退出标志不能忽略。 |
| `tools/wechat_probe.py` | 微信密钥/页解密的独立诊断 | `main`、`decrypt_database` | 维护者 | 读取进程/用户库并输出诊断；输出可能敏感。 |
| `tools/wiki_sync.ps1` | docs/wiki 同步 GitHub Wiki | 参数 `-Message` | 发布者 | 会操作远程，需单独授权。 |
| `tools/README.md` | 装包与渲染脚本参数说明 | 文档 | 维护者 | 两个 ExpectTasks 参数语义不同。 |
| `windows/run.ps1` | 源码计划任务服务入口 | 脚本主体 | install-task | 挑 .venv Python，stdout 字节追加日志；搬迁后需核根路径。 |
| `windows/install-task.ps1` | 注册服务/NapCat/看门狗任务 | `-InstallDir`、`-WhatIf` | 维护者 | 优先启动器，其次源码 run；会改系统任务。 |
| `windows/uninstall-task.ps1` | 注销四个已知计划任务 | 脚本主体 | 维护者 | 不存在任务跳过，不删用户数据。 |
| `windows/status.ps1` | 查询任务、端口、接口与日志 | `Find-OneBotConfig`、`Read-ApiToken` | 检查状态.cmd | 只读；搬迁后需核根路径。 |
| `windows/检查状态.cmd` | UTF-8 控制台调用 status | cmd 主体 | 双击 | 同目录寻找 status.ps1。 |
| `packaging/build.ps1` | 两个 exe 打包、合并资源、ZIP | 脚本主体 | 发布者 | UTF-8 BOM；从 windows 复制四脚本到包根。 |
| `packaging/noticehub.spec` | 服务 PyInstaller 收集规则 | `Analysis`、`EXE`、`COLLECT` | build | main.py 入口、_internal、整个 web_static。 |
| `packaging/launcher.spec` | 桌面启动器收集规则 | `Analysis`、`EXE`、`COLLECT` | build | 动态 pystray/tkinter 后端显式收集。 |
| `packaging/launcher.py` | 单实例、托盘、服务守护、健康等待、退出 | `TrayHost`、`main`、`_launch_service`、`_wait_health` | QQ-Notice-Hub.exe | 与服务分进程；退出/更新/防火墙有副作用。 |
| `packaging/app.manifest` | PerMonitorV2 DPI 声明 | XML | 两个 spec | 影响浮球尺寸与坐标。 |
| `packaging/fix-napcat.ps1` | 找现有 NapCat 并写配置 | 脚本主体 | 配套 cmd | 会写 .env，搜索范围可用 NH_NAPCAT_SEARCH 限定。 |
| `packaging/如果你已经装好NapCat点这个.cmd` | 调 fix-napcat | cmd 主体 | 双击 | 与 PS 脚本同目录。 |
| `packaging/创建桌面快捷方式.cmd` | 创建桌面 lnk | WScript.Shell | 双击 | 目标是安装目录启动器。 |
| `packaging/先看我.txt` | 包内首次使用/升级说明 | 文档 | 用户 | 路径和实际包布局要一致。 |
| `packaging/linux/install.sh` | 建 venv、装依赖、生成 user unit | bash 主体 | Linux 维护者 | 不覆盖已有 .env；生成后给启动命令。 |
| `packaging/linux/notice-hub.service` | systemd 用户服务模板 | ExecStart | install.sh/systemd | 安装时替换 PROJECT_ROOT。 |
| `packaging/macos/install.sh` | venv 与 LaunchAgent 安装 | bash 主体 | macOS 维护者 | 会加载 agent 并设 loopback。 |
| `packaging/macos/com.qqnoticehub.delivery.service.plist` | launchd 服务模板 | ProgramArguments | install.sh/launchd | stdout/stderr 在 logs。 |
| `packaging/macos/uninstall.sh` | 卸载 agent，可选清数据 | `--purge` | macOS 维护者 | purge 需确认且拒绝符号链接数据目录。 |
| `packaging/build-last.log` | 最近构建输出 | 生成文件 | build 操作者 | 不是源输入，不凭旧日志认定新包成功。 |
| `packaging/QQ-Notice-Hub.zip` | 发布 ZIP | 生成文件 | 发布/冷装 | 实际存在与内容随构建变化；不手改。 |
| `packaging/build/`、`packaging/dist/` | PyInstaller 中间及输出树 | 生成文件 | build | 内含第三方依赖副本；不逐文件维护、不作源码。 |

### Android 关键文件

| 路径 | 干什么 | 关键入口（函数/类名） | 谁调它 | 动它之前要知道 |
| --- | --- | --- | --- | --- |
| `android/app/src/main/java/com/noticehub/app/MainActivity.java` | 扫码/手填配对、地址尝试、WebView、快照及 JS bridge | `MainActivity`、`showClient`、`pollPair`、`fetchSnapshot`、`Bridge` | Android Activity | token 在应用偏好；离线 snapshot.json 与在线状态要区分。 |
| `android/app/src/main/AndroidManifest.xml` | 网络、相机、Activity/扫码及深链声明 | Activity 声明 | Android | cleartext、singleTask、noticehub://connect 会影响配对。 |
| `android/app/build.gradle` | SDK、应用版本、签名/配对构建配置及依赖 | Gradle android/dependencies | Gradle | compile/target 35、min 23；ZXing 扫码与 AndroidX 依赖在此。 |

### tests 每文件范围

每行只概括该文件罩住的行为；具体断言以文件为准。模块名可直接用于 `-m unittest tests.test_x`。

| 路径 | 测什么 |
| --- | --- |
| `tests/test_acceptance_gate.py` | gate 脚本静态验收契约。 |
| `tests/test_announce.py` | 公告版本范围、解析、拉取失败和活动公告筛选。 |
| `tests/test_application_calendar.py` | 日历地址排序/主地址及 ICS 装配。 |
| `tests/test_application_notices.py` | 最近推送数据、正文上限和旧正文清理。 |
| `tests/test_application_tasks.py` | 待办 ID/输入、创建/操作/纠正、置顶/紧急和看板。 |
| `tests/test_attachments.py` | 附件解析、提取、安全下载、后台处理、去重和图片留存。 |
| `tests/test_bot.py` | 官方 Bot 记录、回调容错、启动停止。 |
| `tests/test_browser_cookie.py` | 浏览器 cookie 快照、回落、解密、跨平台防护和防泄漏。 |
| `tests/test_calendar_public.py` | 公网/局域网订阅地址与探测白名单。 |
| `tests/test_catchup.py` | NapCat 历史补采。 |
| `tests/test_chaoxing.py` | 学习通客户端、采集、状态及端点替换。 |
| `tests/test_cloudflare.py` | 快速通道地址、下载校验、进程状态机。 |
| `tests/test_codebuddy.py` | CodeBuddy JSON、空输出、退出码、子进程环境。 |
| `tests/test_community.py` | 计数键、网络容错、路由及 telemetry 开关。 |
| `tests/test_community_counts_ui.py` | 社区计数三态与设置开关。 |
| `tests/test_config.py` | 配置默认值、解析及环境变量行为。 |
| `tests/test_config_audit.py` | 来源判定、脱敏、快照和无宿主副作用。 |
| `tests/test_deadline_relative.py` | 相对截止时间回归。 |
| `tests/test_digest_screen.py` | 附件/图片占位过滤与真实通知截止时间。 |
| `tests/test_dingtalk.py` | 钉钉本地读取链路。 |
| `tests/test_dingtalk_receiver.py` | 群/个人/临时会话过滤。 |
| `tests/test_doctor.py` | 诊断包脱敏与打包。 |
| `tests/test_event_extract.py` | 时间、地点、置信度的正负抽取样本。 |
| `tests/test_floatball_host.py` | 浮球生命周期、动画、刷新、滚轮、动作、兜底及全屏。 |
| `tests/test_floatball_render.py` | 绘制几何、命中、滚动、过渡及透明兜底。 |
| `tests/test_floatball_win.py` | 窗口几何、alpha 命中、鼠标/滚轮、ctypes 签名。 |
| `tests/test_group_suggest.py` | 群建议及回落。 |
| `tests/test_host_actions.py` | 防火墙命令、平台防护、提权和审计。 |
| `tests/test_hosting.py` | QQ/NapCat 托管与进程查询缓存。 |
| `tests/test_ics.py` | 上海时区转换和日历输出。 |
| `tests/test_inbox.py` | 收件箱分类与提升。 |
| `tests/test_install_task.py` | 计划任务触发/验证、卸载/状态、脚本编码及平台跳过。 |
| `tests/test_lab.py` | 实验能力可用性、入口和纯度。 |
| `tests/test_lab_rules_form.py` | 真实规则表单 JS 映射及关键词控件。 |
| `tests/test_launcher_quit.py` | 退出顺序、失败继续、手动退出标志和收尾预算。 |
| `tests/test_launcher_watchdog.py` | 启动器导入、快捷方式、单实例、端口、守护、防火墙与退出。 |
| `tests/test_logging_setup.py` | 日志 handler 和输出配置。 |
| `tests/test_main.py` | CLI doctor bundle 参数接线。 |
| `tests/test_napcat_admin.py` | NapCat 管理及 OneBot 端口发现。 |
| `tests/test_napcat_admin_safety.py` | NapCat 安装安全边界。 |
| `tests/test_no_duplicates.py` | 防止同一职责出现派生实现。 |
| `tests/test_onboarding_api.py` | 引导下一步、执行动作及错误状态契约。 |
| `tests/test_overview_sources.py` | 微信/钉钉/推送/学习通来源概览判定。 |
| `tests/test_page_beta11.py` | 接入引导、二维码阶段、宿主确认及设置加载竞争。 |
| `tests/test_pairing.py` | 配对生命周期和状态。 |
| `tests/test_perf_baseline.py` | 性能回归护栏。 |
| `tests/test_push.py` | 文本分块、发送管理、各 webhook 和通道组装。 |
| `tests/test_qr.py` | 二维码编码。 |
| `tests/test_receiver.py` | OneBot 解析、HTTP 接收器及 token 契约。 |
| `tests/test_retry.py` | 重试分类、次数、间隔与最终异常。 |
| `tests/test_routes.py` | 路由表、鉴权、分派结构及别名。 |
| `tests/test_routes_uncovered.py` | 最近推送/洞察/配对拒绝和删除/清理/取消边界的真 HTTP。 |
| `tests/test_rules.py` | 规则解析、匹配、正则安全、排序去重及文件存取。 |
| `tests/test_search.py` | 消息全文检索和 trigram 可用性。 |
| `tests/test_selfcheck.py` | 自检状态。 |
| `tests/test_service.py` | 窗口/紧急/静默、任务同步、提醒、补采退避及推送闸门。 |
| `tests/test_store.py` | Store 消息/摘要/待办/投递与隔离契约。 |
| `tests/test_store_recovery.py` | 坏库隔离、schema 版本、快照及并发 upsert。 |
| `tests/test_summarizer.py` | 选材、去重、时间、推送正文及模型回落。 |
| `tests/test_tasks_latency.py` | tasks 接口群名解析延迟回归。 |
| `tests/test_temp_hygiene.py` | 测试临时目录清理与忽略规则。 |
| `tests/test_token_session.py` | URL token 换 HttpOnly cookie、不落 localStorage、清待配对。 |
| `tests/test_tunnel.py` | 公网探测、Funnel 状态机、关闭及日历 URL。 |
| `tests/test_ui_browser.py` | Edge 中真实响应式页面、控件、弹层与交互。 |
| `tests/test_ui_copy_terms.py` | 用户可见禁词与术语。 |
| `tests/test_ui_state_coverage.py` | QQ 接入后端状态在页面中的覆盖。 |
| `tests/test_update_signing.py` | 清单签名及 .NET 互操作。 |
| `tests/test_update_signing_adversarial.py` | 字段篡改、签名封装和失败关闭对抗。 |
| `tests/test_update_sources.py` | 更新来源 URL。 |
| `tests/test_updater.py` | 检查/镜像/通道、下载、pending、安装、补丁和回滚。 |
| `tests/test_updates_api.py` | applying 标记读失败、状态写重试及应用门槛。 |
| `tests/test_ux_fixes.py` | 多项前端修复和 NapCat 进度接线。 |
| `tests/test_verify_manifest.py` | 临时更新清单校验。 |
| `tests/test_version_bump.py` | 临时仓库版本多落点检查/写入。 |
| `tests/test_watchdog_task.py` | Windows 看门狗脚本契约。 |
| `tests/test_web_static_assets.py` | 外置 JS 整文件语法。 |
| `tests/test_webapp.py` | 页面结构/动效/主题、HTTP API、鉴权、任务及接入状态。 |
| `tests/test_wechat_integration.py` | 微信配置、白名单、重载、HTTP 及消息回调接线。 |
| `tests/test_wechat_probe.py` | 独立微信诊断探针。 |
| `tests/test_wechat_receiver.py` | 微信记录、存储/密钥发现、解密/WAL、增量及 CLI。 |
| `tests/test_wiki.py` | wiki 页面、链接和版本更新日志。 |
| `tests/test_win_text.py` | Windows 输出解码及探测调用者回归。 |

## 4. 我要改 X

以下命令前都套第一节的 TEMP/TMP；浏览器另设 NH_REQUIRE_BROWSER=1。同组两轮，分别取退出码。

| 要改什么 | 修改链路 | 连带测试与命令 |
| --- | --- | --- |
| 加一个推送通道 | `noticehub/delivery/push.py` 实现 Pusher 并接入 build_pushers；`noticehub/platform/config.py` 增字段/通道描述，`bootstrap/settings.py` 解析环境；`settings_api.py` 校验、保存及脱敏；`web_static/js/settings.js` 和 page.html 接表单。沿用 PushManager 的投递/重试。 | `-m unittest tests.test_push tests.test_config tests.test_webapp`；有表单时加 `tests.test_web_static_assets tests.test_ui_browser`。 |
| 加一个网页接口 | `noticehub/web/routes.py` 注册 path/handler/鉴权；`webapp.py:_Handler` 适配请求；业务放对应 application 文件，设置写入沿 SettingsPostMixin。定义输入错误与失败 JSON。 | `-m unittest tests.test_routes tests.test_routes_uncovered tests.test_webapp`；新增正/负 HTTP 断言。 |
| 给待办加一个字段 | `infrastructure/sqlite/store.py` schema/旧库补列/upsert/行转换；`application/tasks.py` 输入与看板；必要时 schedule.sync_tasks 与 summarizer；webapp payload 和 page.html 展示/提交。检查 ICS、快照、Android 是否消费它。 | `-m unittest tests.test_store tests.test_store_recovery tests.test_application_tasks tests.test_service tests.test_webapp`；展示再跑浏览器。 |
| 页面上加一张卡片 | `web_static/page.html` 结构/CSS/状态与交互；已有外置脚本按职责复用；新数据补 routes→webapp→application，不能只造静态占位。 | `-m unittest tests.test_web_static_assets tests.test_webapp tests.test_ui_browser`；检查 390 与桌面、加载/空/失败、键盘与关闭。 |
| 改更新检查流程 | `updater.py:check`、`update_sources.py`、`update_signing.py` 决定来源/校验；`updates_api.py` 管缓存/下载/应用状态；`js/settings.js` 与 launcher 更新线程分别消费。 | `-m unittest tests.test_updater tests.test_updates_api tests.test_update_sources tests.test_update_signing tests.test_update_signing_adversarial`；改 UI 加浏览器，改启动器加 launcher 测试。 |
| 改 QQ 接入 | 事件字段改 `adapters/qq/onebot.py`，HTTP/鉴权改 receiver，首次落库改 application/ingest；安装/进程/二维码改 napcat_admin，托管改 hosting；页面改 guide.js。官方 Bot 改 bot.py，别混成 OneBot。 | `-m unittest tests.test_receiver tests.test_bot tests.test_service tests.test_catchup tests.test_napcat_admin tests.test_napcat_admin_safety tests.test_hosting`；接入卡加 `tests.test_ui_state_coverage tests.test_ui_browser`。 |

新增配置、接口或主流程后，同时更新对应 wiki 配置/接口/使用页和 changelog。本文不代替这些对外契约文档。

## 5. 数据放在哪

`DigestService` 默认打开 `settings.data_dir / 'digest.sqlite3'`。Store 在 `noticehub/infrastructure/sqlite/store.py` 定义表和补列，不另维护一份外置 SQL 作为权威。

| SQLite 表 | 内容 |
| --- | --- |
| messages | 原消息、来源、群/发送者、时间及正文。 |
| processed | msg_id 与处理它的 digest_id，控制调度是否再取。 |
| digests | 摘要标题/正文、payload、窗口与 kind。 |
| deliveries | 通道/目标、去重键、状态、尝试次数与错误；唯一键是去重边界。 |
| meta | token、调度、快照等键值状态。 |
| tasks | 待办正文、时间、来源、候选/确认/完成/忽略/延后、置顶与紧急覆盖。 |
| task_events | 待办操作/纠正历史。 |
| inbox_marks | 收件箱分类判定。 |
| group_names | 群 ID 与显示名。 |
| dingtalk_sync | 钉钉增量读取游标。 |
| search_state | search.py 的全文索引追平游标；按需创建。 |
| messages_fts | FTS5 external-content 索引，不复制 messages 正文；按需创建，另有 SQLite 自动维护的内部表。 |

配置优先级：`bootstrap/settings.py:build_settings` 接收显式 env_file 时加载它；未显式提供环境映射时，从 NOTICEHUB_ENV/兼容旧名或 runtime_root/.env 找配置。`config.py:load_env_file` 用 setdefault，因此已有进程环境不会被 dotenv 覆盖。相同后缀 NOTICEHUB_ 优先 QQ_DIGEST_，即使新名值是空串。显式传入的 env mapping 用作解析源。

默认 data_dir/runtime_root/data、log_dir/runtime_root/logs；学习通 cookie 优先环境配置，其次 data/chaoxing.cookie。`logging_setup.py` 写轮转的 logs/noticehub.log；windows/run.ps1 另把 stdout/stderr 追加 logs/service-stdout.log。service 每日 SQLite 快照在 data/snapshots/digest-YYYYMMDD.sqlite3；附件图片在 data/files。测试临时目录用 nh-tmp；库快照/浏览器 cookie 等 tempfile 使用 TEMP/TMP，必须在测试前指定。

`.gitignore` 当前排除 .env*（保留 .env.example）、data、logs、video、SQLite/WAL/SHM、jsonl、.venv、__pycache__、.reports、.rollback、nh-tmp、备份和 packaging 构建/ZIP 产物。不要提交 token、cookie、用户聊天库、诊断原始输出、签名私钥或 Android 本地签名配置；新增生成目录先补忽略规则。检查包时确认 .env/data/logs 没被打进去。

## 6. 坑

- `noticehub/infrastructure/sqlite/store.py` 是兼容再导出，真正实现是 infrastructure/sqlite/store.py；不要把兼容层当作架构终点。`timeutil.py` 当前却仍含 now_local 和转换实现，domain/time.py 也存在纯转换；不能把它写成“已完成再导出”，迁移前先查每个 import。
- web_static 没有前端构建步骤，但“改完只刷新就生效”不完全成立：webapp.py 在模块导入时读 PAGE_HTML、manifest 和 icon，运行中的 Python 进程仍持旧值；外置 JS 经静态路由读取。源码页面更新应在自己的开发实例重载服务，再刷新；不要重启用户实例。HTTPS 的 service worker 网络优先、断网回离线壳，缓存也可能让你看到旧页面。
- page.html 是巨页，函数、选择器和共享状态跨多个区域；语法检查、HTTP 200 均不能证明页面能操作。布局或交互改动连带浏览器用例，检查移动端和错误/关闭路径。
- 浏览器只用项目 `.venv\Scripts\python.exe -X utf8`，且 NH_REQUIRE_BROWSER=1；否则缺依赖可能整类 skip。TEMP/TMP 指向 nh-tmp，一次只跑一批 Python，两轮分别取退出码，别在管道后拿别的程序退出码。
- packaging/build.ps1 必须保存 UTF-8 带 BOM。Windows PowerShell 5.1 会把无 BOM UTF-8 当本机 ANSI/GBK 解析，中文注释可能破坏语法。tools/gate.ps1 自身按测试契约保持 ASCII。
- tools/gate.ps1 的 freshness 输入清单必须与 packaging/build.ps1 的拷贝输入同步。核对时现有 gate 只看 docs 根 Markdown，build 已递归复制 docs/wiki；gate 还指旧 qq-live-digest.exe 产物路径，build 输出是 noticehub。未修差异前，不能把 freshness 通过当作所有输入都已覆盖。
- Windows 脚本正搬到 windows；核对时 run.ps1 第 3 行、status.ps1 第 4 行仍出现缺变量名的 ` = $PSScriptRoot`。这是观察到的迁移中状态，脚本执行能力未核实；迁移完成后重新检查，勿照本文快照直接执行。
- e2e-install 不带 Overwrite 会删目标目录，带 Overwrite 也会替换文件。InstallDir 只能临时冷装目录；脚本结束可能留进程，验完按目录归属收尾。
- 更新下载完成不等于安装完成；Popen 成功不等于服务健康；有二维码文件不等于二维码新鲜。状态接口必须依据实际探测，失败要返回原因和下一步。

## 维护这份文档

- 新增/删除顶层模块或改变职责：改第三节对应逐文件行；调用链变化同步第二节。
- 新增子命令/参数：从 main.py:_COMMANDS 与 build_parser 核对，改第一节；再补 CLI 测试。
- schema、配置优先级、数据路径变化：改第五节，并核对旧库迁移及忽略规则。
- 新增通道/API/页面流程：改第四节的关联文件与最窄验证命令；新增测试文件补第三节。
- 包布局/脚本输入变化：核对 build 与 gate 两边，改第三、六节；迁移中的观察消失后删除临时提示。
- 文档只记录亲自核对过的符号与路径。命令未执行就写清未执行；不要把源码静态核对写成运行验收。
