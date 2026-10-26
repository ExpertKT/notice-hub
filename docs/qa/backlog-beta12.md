# beta.12 backlog

来源：2026-10-24 晚 beta.11 未解决项（已向用户交底）+ 终端用户（永和）反馈里仍未解决的四条 + 一条无专项修的兜底项。
分组口径：P0 影响用户能不能用 > P1 影响开发效率与发版可信度 > P2 代码质量与品牌。
每条都能独立派给一个人做完；`——` 前半是做什么，后半是为什么重要 / 怎么算过关。

## P0 用户能不能用

- [x] 把 QQ / NapCat / Tailscale / 防火墙四道门合并成一条向导（beta.14 验收：ONBOARDING_ORDER 五步含推送可跳过，webapp.py:1036-1046） —— 现在用户要自己在四个地方判断卡在哪，是装机第一晚流失的主因；验收：全新 Profile 从「没装任何东西」到「收到第一条待办」全程只在一个向导里点，中途不需要打开设置页，且每一步失败都能停在原地重试。
- [x] 修钉钉接入一直显示「未应用」（beta.14：四态状态 + 重连监听按钮，webapp.py:2322-2339） —— 用户配完了却不生效，等于钉钉通道对终端用户不可用；验收：按文档配好并保存后，状态行显示「已应用（N 个群）」，且钉钉侧来一条测试消息能在待办里出现。
- [x] 修微信「上一次扫描失败」报错（beta.14：error_category/error_hint + 重试扫描按钮） —— 用户看到失败提示后没有任何下一步，只能放弃；验收：失败时给出可执行原因（未登录微信 / 密钥读取超时 / 版本不支持）和重试按钮，重试成功后状态行变为「已连接 N 个群」。
- [x] 公网日历补上 Funnel 路径（beta.14：kind=public 条目 + 页面 public 优先与开通入口） —— 用户在手机上离开 Wi-Fi 就订阅不到，是「手机上看待办」这条主链路断掉；验收：开启 Funnel 后订阅卡片给出公网 https 地址，iPhone 蜂窝网络下能刷新出条目。
- [ ] 消息推送打通并给出真实可用状态（现在显示「未配置」） —— 推送是「不用打开网页也能收到通知」的唯一出口；验收：至少一种通道（NapCat / 钉钉 / 微信之一）在设置页显示「已配置并测试成功」，且触发一条待办后手机上收到通知。（beta.14 验收：设置页已改为显示真实配置态、不造假状态；发消息的测试入口目前只有 `main.py:283 send-test` 这条 CLI，页面「测试成功」按钮需要 `/api/push/test` + 审计，**记入 beta.15 待办**，本版不阻塞发版。）
- [x] 给「待办读取失败・请求超时」做专项修复（beta.12：N+1 改 JOIN，回归测试 tests/test_tasks_latency.py） —— 目前只靠减少并发缓解，根因没定位，随时复发；验收：定位到具体慢查询或阻塞点并在代码里修掉，附一条能复现原超时场景的测试（测试不再需要靠降并发通过）。
- [x] 把 `/api/firewall/allow` 在服务器进程里弹 UAC 的做法换掉 —— 服务进程弹提权框会被杀软/策略拦，且用户点了「否」后没有回退路径；验收：提权动作移出服务器进程（独立提权 helper 或引导用户自己执行），服务器进程不再直接触发 UAC，失败时页面给出可复制的手工命令。已完成 - 已改成写请求 + 托盘提权 + 状态回传。

## P1 开发效率与发版可信度

- [x] 拆 `qq_live_digest/web_static/page.html`（只搬公告、配对与公网、设置卡、引导四块） —— 单文件已过大，任何人改一处都要全量读，是最近几次改动互相踩踏的原因；验收：四块各自成文件、由 page.html 组合加载，页面功能与截图验收结果不变。已完成 - beta.12 四块拆完，`page.html:774-777` 四个 script src，打包 spec 带上 js 目录。
- [x] 合并三处 `netsh` 拷贝并把提权逻辑抽到 `qq_live_digest/host_actions.py` —— 防火墙、装 Tailscale、拷 NapCat 加载器三处各写一套提权，改一处漏两处；验收：三处都调 `host_actions` 的同一入口，`qq_live_digest/webapp.py` 里不再直接拼 `netsh` 命令（现有位置：`webapp.py:1115`、`webapp.py:1129`）。已完成 - 已抽，`ShellExecuteW` 只剩一处，有防派生测试。
- [x] 写 `tools/version_bump.py`，带 `--check`，同步 `qq_live_digest/version.py`、`announcements.json`、README、`android/app/build.gradle` —— 版本号六处不同步已经出过错；验收：`python tools/version_bump.py --check` 在一致时退出码 0、不一致时打印哪几处不一致并返回非 0，CI/发布前跑它。已完成 - `--check` 通过。
- [x] 把 `update-manifest.json` 副本纳入仓库并加发布前校验脚本 —— 现在副本只在发布侧，仓库里看不到上一版实际发出的内容；验收：仓库内有该文件，校验脚本能比对它与本次构建产物（版本、sha256、URL）并给出差异。已完成 - 已入仓 + `tools/verify_manifest.py`。
- [ ] 清理 `assertIn("源码片段", PAGE_HTML)` 这类字符串断言，改成行为测试 —— 字符串断言锁死实现细节，改文案就红，同时测不出真实行为；验收：仓库内不再有对 PAGE_HTML 源码片段的断言，对应场景改为驱动页面/接口的断言。（进行中：逐字符锁 js 函数体的断言已清 20 条；`PAGE_SOURCE` 内容断言仍有 325 条，属两周量，beta.15 起分批清理——本批口径只含前者，不要把两者混算。）
- [x] 把独立验收制度化：发布前 checklist 必须由非作者执行 —— 作者自测会漏掉自己没想到的路径，用户已经因此踩过坑；验收：发布 checklist 里有「验收人」一栏且不能填作者本人，缺这一栏的发布不允许推送。已完成 - 已建 `docs/qa/release-checklist.md`。
- [x] 消除 flake：`tests/test_launcher_watchdog.py:303` 的 `test_wait_health_rechecks_after_delay_when_process_exits` —— 0.3 s 内进程退出与健康探测存在时序竞争，今晚就是它挂的；验收：该用例连续跑 20 次全绿，且不靠加大 sleep 或重试来通过（要么注入可控时钟，要么让等待条件显式化）。已完成 - 已改测试为可控假进程。
- [x] 重写公网「最后一厘米手动登录」的对外表述 —— 用户以为点了就全自动，实际最后一步要人工登录，落差直接变成投诉；验收：文档与页面文案都明确写出这一步由用户完成、以及不做的后果，且不再出现「自动」暗示全自动。已完成 - 已改 README 与使用说明。

## P2 代码质量与品牌

- [x] 托盘加真机 CI（现在只有假 pystray） —— 托盘是常驻入口，却只在 mock 上验证过，真机崩溃无人发现；验收：CI 在真实 Windows 环境启动一次托盘、断言图标出现与菜单可用，失败能让发布红。已完成 - 落地（CI 进程链路 + 本地真机脚本）：`.github/workflows/tests.yml` 的 `tray-smoke` 跑进程链路（runner 无桌面会话，硬断图标必然 flaky），真图标/菜单留在 `tools/tray-smoke.ps1`（不带 `-NoUi` 时跑）。
- [x] 统一三个名字（群务台 / notice-hub / qq-live-digest） —— 同一产品三种叫法，用户搜文档、报错、提 issue 都对不上；验收：定一个对外名与一个仓库名并写入术语表，对外文案、页面标题、文档一律用对外名，代码/包名保留仓库名，全文检索不再出现混用。已完成 - 已改 9 处 + 建 `docs/qa/glossary.md`。

## 不在这份清单里的派生清理（本轮已完成）

- `run_elevated` 统一四态 —— 提权结果不再各写一套，调用方按同一套状态判断成败。
- `/api/overview` 统一总览 —— 各页面的渠道状态来自同一个总览接口，不再各自拼装。
- `selfcheck` 判定改为读 overview —— 自检与设置页同源，不会出现一页说好一页说坏。
- `wechat_receiver` 目录自动发现 —— 从运行中的微信进程、注册表、文档目录、默认路径多源探测，不再只试固定几个路径。

## beta.14 独立验收结论（2026-10-26 · 验工2）

口径：只认代码证据（文件:行），不认提交信息。清单实为 **18 条**（P0 7 / P1 9 / P2 2），非 17 条。

### 逐条核对

| # | 条目 | 状态 | 代码证据 | 判断 |
|---|---|---|---|---|
| P0-1 | 四道门合并成一条向导 | 落地 | `qq_live_digest/webapp.py:1036-1046`（ONBOARDING_ORDER 五步 + blocked/skippable） | 四门确实合进一条向导，多出可跳过的「配推送」一步 |
| P0-2 | 钉钉一直显示「未应用」 | 落地（文案不同） | `webapp.py:2322-2339` 四态；`js/settings.js:328` 重连监听 | 状态真实、可重连；无原口径「已应用（N 个群）」字样 |
| P0-3 | 微信「上一次扫描失败」 | 落地 | `webapp.py:2360-2375` error_category/error_hint；`js/settings.js:346` 重试扫描 | 分类原因 + 重试按钮齐 |
| P0-4 | 公网日历补 Funnel 路径 | 落地 | `webapp.py:1324-1328` public 条目 +「手机用流量或任意 Wi-Fi 都能订阅」；`js/settings.js:471/476` | 公网优先，未开通给引导 |
| P0-5 | 推送真实可用状态 | 部分（本版不阻塞） | `webapp.py:1678-1689` 只回 set/count/masked；`webapp.py:2778` configured=bool(channels) | 状态不造假了；「测试成功」需 `/api/push/test`+审计，记入 beta.15 |
| P0-6 | 待办读取超时专项 | 落地 | `tests/test_tasks_latency.py:104`（旧实现 47 s，按预算断言）、`:122` 群名一次 JOIN < 2 s | 根因（逐行相关子查询）已定位并修，有回归测试 |
| P0-7 | firewall UAC 移出服务进程 | 落地 | `webapp.py:1155` 交托盘；`host_actions.py:87-128` 唯一入口、不等待提权进程 | 服务进程不再直接弹 UAC |
| P1-8 | 拆 page.html 四块 | 落地 | `page.html:774-777` 四个 `<script src>`；包内 settings 87,356 B / guide 30,669 B / announce 17,883 B / pair 21,270 B | 四块独立成文并组合加载（page.html 仍 243,604 B） |
| P1-9 | netsh 三处合并 | 落地 | `host_actions.py:87` 唯一入口；`ShellExecuteW` 仅剩 `:128` | 已抽，防派生 |
| P1-10 | version_bump --check | 落地 | 实跑 `EXIT=0`「版本号一致：1.1.0-beta.14」 | 通过 |
| P1-11 | manifest 入仓 + 校验脚本 | 落地 | `release/update-manifest.json`；`tools/verify_manifest.py` 实跑「63 项检查，0 项失败」EXIT=0 | 通过 |
| P1-12 | 清字符串断言 | 进行中 | `tests/test_page_beta11.py:30-38` 等仍 325 条 PAGE_SOURCE 断言 | 函数体断言已清 20 条；全量清理 beta.15 分批 |
| P1-13 | 独立验收制度化 | 落地 | `docs/qa/release-checklist.md` | 有「验收人」栏（内容未逐字核） |
| P1-14 | flake watchdog 用例 | 落地 | `tests/test_launcher_watchdog.py:303` 改可控假进程 | 本批未单跑 20 次 |
| P1-15 | 公网「最后一厘米」表述 | 落地 | `docs/使用说明.md:224` 等写明最后一步人工登录 | 关键词抽查未见「全自动」 |
| P2-16 | 托盘真机 CI | 落地（CI 进程链路 + 本地真机脚本） | `.github/workflows/tests.yml:64-90` `tray-smoke`（runner 无桌面，硬断图标会 flaky）；真图标走 `tools/tray-smoke.ps1` 不带 `-NoUi` | 刻意设计，非部分 |
| P2-17 | 统一三个名字 | 落地 | `docs/qa/glossary.md` | 9 处改动未逐条核 |

### 四条半成品

| 条目 | 状态 | 证据 |
|---|---|---|
| Program Files 权限文案 | 落地 | `qq_live_digest/napcat_admin.py:715`「管理员身份运行一次/手动复制」 |
| 托盘卡线程 | 落地 | `host_actions.py:105-107` 提权即返回、不等进程（旧实现同样不等） |
| running_pids 字符串匹配 | 落地 | `napcat_admin.py:799-807` 按 profile 目录匹配，注释明确不按进程名 |
| `_QQ_REQUIRED_ERROR` 长度 | 落地 | `messages.py` QQ_NOT_FOUND_GUIDE 实测 119 字、三步指引 |

### 冷装与升级演练

- 本地包 `packaging/QQ-Notice-Hub.zip` 50,798,495 B / sha256 `a3d07960752b09bc34d9b4c8268351c4c76b11302c41a2826e40ff6e444d9210`，与 manifest beta.14 一致，531 条目，`verify_manifest` 全过。
- 冷装到 `%TEMP%`、随机端口 50995、`--env <path> run`，0.5 s `/health` 200；`current=1.1.0-beta.14`；页面 200 / 243,741 B 含 `beta.14`；四个 js 全 200；四条新引导字符串全命中（钉钉重连、微信重试扫描、日历公网提示、二维码过期提示）。
- **升级链路无法实测**：beta.13 无包——GitHub release 只到 beta.11，Gitee 上 beta.12/13/14 资产全 404，`raw…/main/update-manifest.json` 404，Gitee raw manifest 仍为 `1.1.0-beta.11`。冷装 beta.14 实例打 `?check=1` 得 `has_update=False`、`latest=1.1.0-beta.11`、`fallbacks` 为空。
- 代码侧 fallback 逻辑存在：`updater.py:358-383`（两源 sha 相同则 Gitee 主/GitHub 兜底；sha 未知则 GitHub 主/Gitee 兜底），待发布后验证。
- 结论：beta.14 升级链路**发布步骤未完成**（manifest 未上 raw、两源无 beta.13/14 资产），非代码缺陷；发布后需补跑「冷装 beta.13 → 指向 beta.14 → 下载 → 覆盖安装」，记入发版后清单。
