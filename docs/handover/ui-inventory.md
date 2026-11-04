# 界面内容盘点：可交互内容 + 静态内容 + 逻辑关系

> 配合 [`ui-refactor.md`](./ui-refactor.md) 使用。那份讲怎么改、怎么验；这份讲**界面上一共有哪些东西、谁连谁**。
> 盘点是按运行时 DOM 逐块核对的（202 个带 `id` 的元素 + JS 生成的卡片），不是读代码猜的。

---

## 1. 骨架总览

```
<body>
├── 常驻外壳（四个页签共享）
│   ├── .masthead-row          吸顶栏：品牌 / 公告铃铛 / 本地运行标记 / 托管胶囊 / 开始托管
│   ├── .summary               今日概览卡（含轮播、清空已完成、进度条）
│   ├── #top-banner-row        横幅区：公告横幅 / 版本更新横幅 / 配置体检条 / 首次使用引导
│   └── .tabs                  四个页签
├── .workspace
│   ├── #tab-tasks             页签 A「待办」= 主列（分组列表 + 月历面板）
│   ├── #tab-notices           页签 B「最近推送」
│   ├── #tab-inbox             页签 C「收件箱」= 历史回溯 + 通知收件箱
│   ├── #tab-settings          页签 D「设置」（内容几乎全部由 settings.js 生成）
│   └── .side-rail             右栏 7 块（**不属于任何页签**，四个页签下都在）
└── 浮层（默认 hidden，按条件出现）
    ├── #announce-panel / #announce-modal   公告面板与公告详情
    ├── #onboarding                        首次使用风险提示
    ├── #platform-setup-wizard             首装配置向导（三步）
    ├── #star-prompt                       支持开发者提示
    ├── #token-hint / #action-feedback     令牌交换失败提示 / 操作结果条
```

**右栏不属于任何页签**（`.side-rail` 是 `.workspace` 的直接子元素），切页签时右栏不动——改布局时别把它当成 `#tab-tasks` 的一部分。

---

## 2. 页签 A：待办

### 2.1 静态骨架（写死在 `page.html`）

| 区块 | 内容 | 备注 |
|---|---|---|
| 今日概览 | eyebrow「今日概览」+ 进度百分比胶囊 + `h1#headline` + `p#subline` | 标题/副标题**由后端分组逻辑生成**，不是静态文案 |
| 轮播 | 「最近到期 / 正在进行」两个胶囊切换 + 日期标题 + 3 条清单 + 「N 件」+ ‹ › | 固定在 200px 高（窄屏 220px），空态收到内容高度 |
| 底部操作 | 「清空已完成」按钮 + 进度条 | |
| 分组 | 今天 / **已过期**（收起）/ 本周 / **待确认** / 以后 / **已完成**（收起） | 分组名为静态文案；**空分组整块不渲染** |
| 月历面板 `#calendar-panel` | 月历标题 + ‹ › + 7×N 格 + 「查看月历」折叠按钮（手机）+ 当天详情区 | 在**主列**里，插在第一个分组之后 |

### 2.2 可交互内容

| 元素 | 类型 | 触发的动作 | 影响范围 |
|---|---|---|---|
| `#tab-tasks-button` 等 4 个页签 | `role=tab` | 切页签 | 只切换 `aria-selected` 与面板显隐，**不重新拉数据** |
| `#upcoming-mode-due` / `#upcoming-mode-now` | 胶囊 | 轮播切「最近到期 / 正在进行」 | 记住在 `qq_digest_overview_mode` |
| `#upcoming-prev` / `#upcoming-next` | 图标按钮 | 轮播翻日期 | |
| `#tasks-clear-done` | 按钮 | `POST /api/tasks/clear-finished` | 只清 done/dismissed/expired，open 与 candidate 不动；幂等 |
| `#calendar-toggle` | 按钮（手机） | 展开/收起月历 | 纯前端 |
| `#cal-prev` / `#cal-next` | 图标按钮 | 翻月 | 纯前端 |
| 月历格 | `div.calendar-day[data-has-events]` | 点有事件的格子 → 展开当天详情 | 纯前端 |
| `#calendar-detail` 关闭按钮 | 按钮 | 收起当天详情 | |
| 每张待办卡的勾选框 `.check` | 按钮 | `done` / `reopen` | 卡片换分组 + FLIP 位移动画 |
| 卡片「查看完整原文」 | `<details><summary>` | 展开原文与纠错区 | 纯前端 |

### 2.3 卡片内的动作按钮（JS 生成，按状态出现）

| 按钮 | 后端 `action` | 出现条件 |
|---|---|---|
| 确认待办 | `confirm` | 仅候选（`status=candidate`） |
| 忽略 | `dismiss` | 候选 / 进行中 |
| 明天提醒 | `snooze` | 候选 / 进行中（时间不可自定义，固定次日早晨） |
| 处理完成 | `done` | 进行中 |
| 取消完成 | `reopen` | 已完成 |
| 删除这条待办 | `delete` | 仅手工新建（`manual:` 前缀）且未完成；其余状态不显示 |
| 紧急 / 跟随自动判断 | `urgent: true` / `urgent: null` | 全部（后者是清除覆盖） |
| 置顶 / 取消置顶 | `pin` | 全部，已完成的不给置顶 |

卡片上的**只读标签**（不可点）：分类 chip（紧急/待办/学业/通知）、截止 chip（今天/明天/日期 + 已过）、稍后提醒 chip、来源 chip（微信 · 群名）、群名 chip、`+N` 折叠 chip。

### 2.4 右栏 7 块

| 区块 | 可交互 | 静态内容 |
|---|---|---|
| `#pinned-panel` 置顶日程 | 每条「取消置顶」；底部 `#pinned-resize` 拖动改高度 | 标题、空态文案「还没有置顶的日程」 |
| `#connect` QQ 接入 | 5 个动作按钮（下载安装并启动 / 启动引擎并显示二维码 / 刷新二维码 / 重启引擎 / 帮我连上它）——**同一时刻只露一个主按钮 + 至多一个次按钮**，按「装没装 → 跑没跑 → 登录没有」的状态机切换；QQ 号输入框、折叠「已经自己装过 NapCat？」→ 目录输入 + 保存、二维码图 | 状态行 `#status`、下一步提示 `#qq-next-step`、引擎状态 `#napcat-engine`、二维码占位说明 |
| `#groupbox` 订阅群 | 「拉取我的群」「全选建议」「清空」「保存」、群搜索框、群行多选 | 「订阅群」标题、群来源徽标、空态引导链接 |
| `#rail-summary` 本周小结 | 无（纯展示：待办/已完成/逾期 3 个数字 + 7 天柱状图） | 说明文案（如「3 个逾期事项」） |
| `#rail-about` 关于与支持 | 「使用须知与风险提示」→ 打开 `#onboarding`；GitHub star 按钮 → `#star-prompt`；「反馈问题」链接 | 社区在线数（拿不到显示「暂不可用」） |
| `#rail-actions` 快捷操作 | 新建待办（展开内联表单：内容 + 截止时间 + 保存/取消）、复制今日清单、导出 `.ics`、跳到二维码、暂停动态效果、回到顶部 | 每个按钮下的小字说明 |
| `#rail-health` 服务自检 | 「重新自检」、每行「去设置」跳设置页 | 8 行固定标签：QQ 登录 / 引擎托管 / 钉钉 / 微信 / 学习通登录信息 / 附件解析 / 公网日历 / 消息推送，每行有状态点（ok/warn/bad）+ 值 + 提示 + 下一步 |

---

## 3. 页签 B：最近推送

内容**全部由 JS 渲染**（静态骨架是空的）。

| 元素 | 说明 |
|---|---|
| `.notice-feed` 时间轴 | 左侧竖线 + 圆点，每条是一个摘要 |
| 每条摘要 | 标题 chip（如「定时摘要」）+ 时间 + 正文（最多 72ch 宽） |
| 无交互 | 这里**没有任何按钮**：只看不改 |

---

## 4. 页签 C：收件箱（含历史回溯）

### 4.1 历史回溯 `#history-*`

| 元素 | 类型 | 动作 |
|---|---|---|
| `#history-all-groups` / `#history-clear-groups` | 按钮 | 全选 / 清空群多选 |
| `#history-groups` | 群多选列表（`label` + checkbox） | |
| `#history-probe` | 按钮 | `GET /api/history/floor`：探测本地缓存最早能追到哪天（最坏约 30 秒） |
| `#history-since` / `#history-until` | `input[type=date]` | |
| `#history-limit` | `input[type=number]` | |
| `#history-7` / `#history-30` / `#history-all-dates` | 按钮 | 快捷填日期区间 |
| `#history-fetch` | 按钮 | `POST /api/history/fetch` 起任务；已在跑回 409 |
| `#history-classify` | 按钮 | `POST /api/inbox/classify`：「提取通知」，**与回溯共用一把任务锁** |
| `#history-cancel` | 按钮 | 取消（任务已结束回 409，与收件箱的取消不同） |
| `#history-job` | 进度条 + 状态行 | 轮询 `/api/history/fetch/status` 或 `/api/inbox/classify/status` |

### 4.2 通知收件箱 `#inbox-*`

| 元素 | 类型 | 动作 |
|---|---|---|
| `.inbox-counts` 4 个按钮 | 筛选（`aria-pressed`） | 全部 30 / 确定通知 7 / 疑似通知 3 / 已转待办 3；**计数恒为全量** |
| `#inbox-group` | `select` | 按群筛选（选项由数据生成） |
| `#inbox-search` + `#inbox-search-button` | 搜索 | `q` 参数 |
| `#inbox-refresh` | 按钮 | 重新拉取 |
| 每条消息 | 「转成待办」（已转过的变「重试转待办」）、「删除」（两段式确认）、`<details>`「查看消息正文」、原图缩略图 | 删除会连原图一起删 |
| `#inbox-more` | 按钮 | 翻页（`limit`/`offset`） |

---

## 5. 页签 D：设置

内容**几乎全部由 `settings.js`（128 KB）生成**，`#tab-settings` 自己只有十来个容器壳子。13 个面板：

| 面板 | 关键控件（绑定 id） | 数据源 |
|---|---|---|
| 服务公告 | 无交互，纯展示当前公告 | `/api/announcements` |
| 首次配置 | 「重新运行配置向导」→ `#platform-setup-wizard` | `/api/setup/status` |
| 版本更新 | `update-channel` 单选、检查更新、下载、应用（会重启服务） | `/api/update*` |
| iPhone 日历订阅 | 地址框、二维码、「测试这个地址」 | `/api/sync/info`、`/api/sync/test`、`/api/sync/qr.png` |
| 数据备份 | `#backup-download` 下载 zip | `/api/backup` |
| 学习通 | `#cx-auto-cred` 读浏览器 Cookie、`#cx-load-courses`、`#cx-save`、Cookie 校验 | `/api/chaoxing/*` |
| 钉钉 | `#dt-save`、`#dt-scan-now`、`#dt-reconnect`、`#dt-select-suggested` | `/api/dingtalk/*`、`/api/groups/suggest` |
| 微信 | `#wx-save`、`#wx-scan-now`、`#wx-scan-retry`、`#wx-key-save`、`#wx-dir-save`、`#wx-select-suggested` | `/api/wechat/*` |
| 手机 App | 配对二维码、已配对设备、防火墙放行、公网入口 | `/api/pair/*`、`/api/app/qr.png`、`/api/firewall/*`、`/api/tunnel/*` |
| 外观 | 主题三态（跟随系统/浅/深）、动效开关 | `localStorage`：`nh_theme`、`nh_motion` |
| 托管设置 | `#hosting-start` / `#hosting-stop`、四组偏好开关、`#pref-catchup-save` | `/api/hosting/*` |
| 支持开发者 | 赞赏码、「使用须知」入口 | `/api/donate/qr.png` |
| 运行信息 | 只读：端口、令牌来源、各通道状态 | `/api/meta`、`/api/overview`、`/api/config/audit` |

> 设置页还有一组由 `lab-rules.js` 渲染的「自动整理规则」编辑器（增删规则、试跑一条消息），
> 以及「推送通道」五项（WxPusher / Server酱 / PushPlus / Webhook）——它们和上面 13 个面板并列，
> 都在 `#tab-settings` 内，改设置页布局时一并算进去。

---

## 6. 浮层与横幅（条件出现）

| 元素 | 出现条件 | 可交互 | 关闭方式 |
|---|---|---|---|
| `#announce-bell` + `#announce-bell-badge` | 常驻（有未读才显示数字） | 打开 `#announce-panel` | — |
| `#announce-panel` | 点铃铛 | 列表项展开、`#announce-panel-read-all` 全部已读、关闭 | 关闭按钮 / Esc |
| `#announce-banner` | 有未读公告 | 点开 `#announce-modal` | — |
| `#announce-modal` | 点横幅或铃铛里的某条 | `#announce-modal-prev/next` 翻条、`#announce-modal-ok` 确认、详情链接 | 确认 / Esc |
| `#update-banner` | 有新版本 | 点开更新面板 | — |
| `#platform-setup-panel` | 有平台没配好 | `#platform-setup-banner` 展开体检清单，每行一个「去配置」 | 收起 |
| `#coldstart-guide` | 待办为 0 且 QQ 未在线 | 「首次使用？先完成 QQ 接入 →」「×」 | 关闭后记 `nh_coldstart_guide_closed` |
| `#onboarding` | 首次使用（未接受） | `#onboarding-accept` 我知道了 / `#onboarding-close` | 接受后记 `onboarding_accepted_at`（服务端） |
| `#platform-setup-wizard` | 首访（`nh_setup_wizard_seen` 未设）或从设置重开 | 三步：选平台 → 逐项检查 → 进入主界面；每步都有「稍后配置」 | 稍后配置 / 完成后记 `nh_setup_wizard_seen` |
| `#star-prompt` | 点「支持开发者」 | `#star-prompt-go`（外链）、`#star-prompt-later`、`#star-prompt-never` | 选择后记 |
| `#token-hint` | 仅当 URL 令牌换 Cookie 失败 | 无（纯提示） | —— |
| `#action-feedback` | 任何写操作的结果 | `#feedback-retry` 重试 | 几秒后自收 |
| `#journey` 新手引导 | 首装未完成时 | `#journey-action` 主行动、`#journey-dismiss` 暂时收起 | 收起后记 `qq_digest_journey_hidden` |

---

## 7. 逻辑关系

### 7.1 数据源归属（谁读哪个接口）

| 区域 | 接口 | 刷新方式 |
|---|---|---|
| 概览卡 / 分组列表 / 右栏统计 / 置顶 | `GET /api/tasks` | **60 秒**定时 + 每次写操作后 |
| 轮播 | 同上（`today/week/candidates` 在同一个响应里） | 同上 |
| 收件箱 | `GET /api/inbox`（`verdict`/`group_id`/`q`/`limit`/`offset`） | 手动刷新 + 翻页 |
| 最近推送 | `GET /api/notices` | 切到该页签时 |
| 服务自检 | `GET /api/selfcheck`（单飞，最多等 30 秒） | 手动 + 页面加载 |
| 通道总览 | `GET /api/overview`（**通道判定的唯一来源**） | 各处复用，不重复探测 |
| 设置页 | `/api/settings`、`/api/meta`、`/api/config/audit` | 打开时 + 保存后 |
| QQ 接入 | `GET /api/napcat/status` | 约 **5 秒**轮询（安装中额外 1 秒轮询 `install/status`） |
| 托管胶囊 | `GET /api/hosting/status` | **10 秒** |
| 群列表拉取 | `GET /api/qq/groups/status` | **15 秒** |
| 公告 | `GET /api/announcements` | 约 15 分钟 + 手动刷新 |
| 配对/公网 | `/api/pair/*`、`/api/tunnel/status` | 配对 4 秒、隧道 2 秒 |

### 7.2 状态流转（写操作 → 界面变化）

```
候选 ──确认──► 进行中 ──完成──► 已完成 ──取消完成──► 进行中
  │              │
  ├─忽略──► 已忽略          进行中 ──截止过期──► 已过期（后端判定）
  └─稍后──► 仍是候选/进行中，但打上 snooze 标记
                             手工建的 ──删除──► 真删（其余状态不给删）
```

- 每次写动作 → `POST /api/tasks/<id>`（或 `/pin`、`/urgent`、`/create`）→ 成功后**重新拉 `/api/tasks`** → 重新渲染分组 → FLIP 动画把被挤动的卡片移过去。
- 「已完成」的卡片飞出分组（不做位移动画），「已完成」分组默认收起。

### 7.3 依赖与互斥（改界面时最容易碰坏的部分）

| 关系 | 说明 |
|---|---|
| 回溯 ↔ 提取通知 | **共用一把任务锁**：一个在跑，另一个返回 409；进度条与取消按钮共用 |
| 空分组不渲染 | `section()` 对空数组返回 `null`，整块消失。改布局时不要假设分组恒存在 |
| 「已过期」「已完成」是收起的 `<details>` | 里面的卡片有布局盒但**不绘制**；任何几何检测都要排除 |
| 首次使用这条链 | `#onboarding`（风险提示，服务端记录）→ `#platform-setup-wizard`（选平台）→ `#journey`（引导）→ `#coldstart-guide`（冷启动提示），彼此条件重叠但文案不同 |
| 页签不重拉数据 | 切页签只改显隐；数据靠定时器与写操作后刷新 |
| 右栏跨页签常驻 | 它不属于任何页签 |
| 月历面板在主列 | 由 JS 插到第一个分组之后，`#calendar-toggle` 必须在 `#calendar-panel` 里面（有测试钉住） |
| 写操作后必须让缓存失效 | `AGENTS.md` 的硬规矩：安装/启动/重启/登录之后缓存必须失效，不许让用户看到旧状态 |

### 7.4 持久化在浏览器里的东西

| 键 | 作用 |
|---|---|
| `nh_theme` | 明/暗（没设时按浅色） |
| `nh_motion` | 动效暂停 |
| `qq_digest_overview_mode` | 轮播显示「最近到期」还是「正在进行」 |
| `nh_setup_wizard_seen` | 首装向导是否已看 |
| `nh_coldstart_guide_closed` | 冷启动提示是否已关 |
| `qq_digest_journey_hidden` | 新手引导是否收起 |
| `nh_ann_read` / `nh_ann_seen` / `seen_announce_ids` | 公告已读 |
| `seen_update_tag` | 已提示过的版本号 |

> 令牌**不落 localStorage**（安全加固后改成 HttpOnly Cookie）。

---

## 8. 静态内容清单（不随交互变化）

这些是可以直接改文案/排版、不需要动逻辑的部分：

| 位置 | 内容 |
|---|---|
| 页头 | 「群务台」「群消息 · 待办与日历」、通道徽标（服务端注入） |
| 页签 | 待办 / 最近推送 / 收件箱 / 设置（各带图标） |
| 分组名 | 今天 / 已过期 / 本周 / 待确认 / 以后 / 已完成 |
| 分类文案 | 紧急 / 待办 / 学业 / 通知（`CATEGORY_LABELS`） |
| 概览卡 | 「今日概览」「最近到期」「正在进行」「清空已完成」 |
| 卡片 | 「查看完整原文」「纠错」「暂时收起」等按钮文案 |
| 右栏标题 | 置顶日程 / QQ 接入 / 订阅群 / 本周小结 / 关于与支持 / 快捷操作 / 服务自检 |
| 自检 8 行标签 | QQ 登录 / 引擎托管 / 钉钉 / 微信 / 学习通登录信息 / 附件解析 / 公网日历 / 消息推送 |
| 设置 13 面板标题 | 见第 5 节 |
| 风险与法务文案 | `#onboarding` 里的 NapCat 风控提示、微信只读说明、数据本地存储说明、许可声明 |
| 空态与提示 | 「还没有置顶的日程」「今天没有到期的事」「公告暂时不可用」等 |

**由数据驱动、不要写死的**：`#headline` / `#subline` / 进度百分比、卡片标题与截止 chip、
各计数（收件箱四类、右栏三数）、自检状态点与值、月历事件、公告内容、版本号。

---

## 9. 重构时的注意点

1. **别把 `.side-rail` 挪进某个页签**——现在它跨页签常驻，挪进去会让另外三个页签丢掉右栏。
2. **卡片状态决定按钮集合**，不是所有按钮都在所有卡片上（见 2.3）。做新卡片时先画状态矩阵。
3. **空分组不渲染**，所以布局要考虑「只有 2 个分组」的极端情况。
4. **两个收起分组（`<details>`）** 里的卡片不绘制，截图/几何检查要排除。
5. **一切写操作之后都要重拉 `/api/tasks`**，且 `AGENTS.md` 要求写动作后缓存失效。
6. **通道状态只有一个来源 `/api/overview`**，设置页、自检、向导都读它；不要各自探测。
7. **服务端会替换两个注释标记**（`<!--CHANNEL_BADGE-->`、`<!--CHANNEL_LOGO-->`），删了就没有版本徽标与品牌色。
8. **设置页的 DOM 由 JS 生成**：改它的外观要动 `settings.js`，而且要同步 `test_webapp` 里对设置页结构的断言。
9. **令牌只放 HttpOnly Cookie**，别为了省事塞回 URL 或 localStorage。
