# history.md — 重大决策与踩坑史

口径：从 `git log`（152 个提交）+ 代码注释 + `docs/qa/backlog-beta12.md` 提取。
**只写事实**；我的推测单独放最后一节标「待确认」。行号以 HEAD `ad67e3e` + 工作区改动为准
（工作区有 19 条未提交改动，见 api-contract.md 与 risks.md 的说法）。

上游分叉点：`310a092`（2026-10-06「Use real clone URL in install steps」）是最后一个
与上游共享的提交；其后的提交都是本仓库独有。上游 = `wc985732-lang/qq-live-digest`。

---

## 打包与分发

### 1. 只读位文件导致装出来的目录解压失败
- 时间：`1.1.0-beta.11` 前后（`packaging/build.ps1:81-91`）
- 问题：zip 条目带文件属性位，只读源文件解出来是 ReadOnly，用户「解压覆盖旧目录」直接
  PermissionError [WinError 5]。
- 解法：打包前递归清 `IsReadOnly`，并打印清了几个（`build.ps1:84-91`）。
- 现在还在用：**是**。

### 2. zip 顶层目录结构错位 → 生产环境跑的是旧 exe
- 时间：`2026-10-11 16:22` 那次构建（`build.ps1:92-100` 注释）
- 问题：`Compress-Archive -Path $appRoot` 会把内容包进顶层 `qq-live-digest\`，
  而 `install-task.ps1` 注册的是 `<install root>\QQ-Notice-Hub.exe`。
  结果生产跑根目录那份**旧的** `qq-live-digest.exe`，新的那份嵌套目录没人用。
- 解法：改手写 `ZipArchive`，条目不带顶层目录，`CreateEntry` 用去掉根前缀的相对路径。
- 现在还在用：**是**。

### 3. zip 条目用 `\` 分隔符，Linux/macOS 解压全烂
- 时间：同上（`build.ps1:96-100`）
- 问题：Windows PowerShell 5.1 跑在 .NET Framework 上，`ZipFile.CreateFromDirectory` /
  `Compress-Archive` 写 `\` 分隔符。实测 2026-10-11 16:22 构建 293 个条目里 280 个如此。
  Linux/macOS 的 unzip 把反斜杠当文件名一部分，全部解成一层扁平的坏文件。
- 解法：手写条目名时 `.Replace('\', '/')`（`build.ps1:108`）。
- 现在还在用：**是**。**这是重写打包侧最容易复发的坑。**

### 4. license 文件缺失被静默跳过
- 时间：`C-05`（`build.ps1:64-74`）
- 问题：原来 `if (Test-Path) { Copy }`，新 clone 里 `LICENSE-UPSTREAM`/`NOTICE` 还没进 git，
  构建「成功」但产出一个**没有任何许可声明**的包 —— MIT 和 PolyForm 都要求带声明。
- 解法：改成缺文件直接 `throw`，构建失败。
- 现在还在用：**是**。

### 5. 打包带上 `web_static/js`
- 时间：`3d03d06`（2026-10-25）
- 问题：PyInstaller 只按 spec 的 datas 收集，`web_static/js` 漏了，
  **装出来的 exe 里前端脚本 404**，页面功能全废但服务本身正常。
- 解法：spec 补 datas。
- 现在还在用：**是**。教训：打包产物内容必须逐项核对，不能只看构建成功。

### 6. 打包时不算 sha256
- 现状（HEAD）：`packaging/build.ps1` 无任何 hash 计算（grep `Hash|sha` 零命中）。
- 发布前登记：`tools/gate.ps1:62` `Get-FileHash`，但只用于 mtime 新鲜度打印，
  `:55-57` 有 ponytail 注释承认这个天花板。
- 现在还在用：**是**。

---

## 自动更新

### 7. 没有 sha256 的版本一律拒装
- 位置：`updater.py:497-500,544-549`
- 根因：GitHub API 的 `assets[].digest` 老版本没有这个字段；没有校验值就无法保证完整性。
- 解法：缺 sha256 直接 `ok=False` 并给「请手动下载」；下载阶段边下边算
  （`:513,534`），不符就删掉半截文件。
- 现在还在用：**是**。

### 8. 覆盖安装必须等进程全部退出 → 外置 PowerShell 脚本
- 位置：`updater.py:667-855`（`_script_text` 生成、`apply_async` 执行）
- 根因：正在运行的进程**锁着自己的 exe**，没法给自己换文件。
- 解法：生成一个临时 `.ps1`，先 `Start-Process launcher --quit`（`:702`）请求优雅退出，
  等最多 60 秒（`:708-712`），超时再按安装目录兜底结束进程（`:713`），
  然后 `Expand-Archive -Force`（`:773`），装完探 `/health` 3 次 × 2 秒，失败给人话兜底（`:849`）。
- **装完不复算 hash** —— 信任下载阶段的结果（`:667-855` 无任何 hash 校验）。
- 现在还在用：**是**。

### 9. 升级重启后旧公告/旧更新提示不消失
- 时间：`08f583b`、`84c10f4`（2026-10-21）
- 问题：升级后缓存里还是旧版本的公告与更新提示，用户反复看到「有新版本」。
- 解法：启动时按版本作废缓存。
- 现在还在用：**是**（`updates_api.py` / `announce.py`）。

### 10. 版本号比较防降级
- 时间：`1d64ad1`（2026-10-18）
- 问题：beta 号来回跳（当时出现 beta.2 → beta.8 的倒退），更新器会把旧版当「更新」。
- 解法：版本比较加降级保护。
- 现在还在用：**是**。另外 `af09c8c` 把 beta.12/13/14 三个从未发布的本地版本合并成一个
  beta.12 —— 说明**版本号曾失控过**，重写时版本号必须单一来源。

---

## QQ 接入与 NapCat 托管

### 11. 扫码登录后无反应
- 时间：`d73b46e`（2026-10-12）
- 根因：**启动前不写 OneBot 配置**。扫码成功了，但 NapCat 起来后连不到后端。
  而且自检查不出这个断点，界面上看就是「没反应」。
- 解法：先写配置再启动；自检补这一项。
- 现在还在用：**是**。这是「静默失效」的典型案例：前端显示成功，链路是断的。

### 12. 端口探针把刚拉起来的服务杀掉
- 时间：`d1af371`、`1564dc5`（2026-10-13）
- 根因：探针访问 `/api/health` 拿 200，判定「端口被占用」，于是**杀掉自己刚起的服务**。
- 解法：占用判定要区分「是自己」还是「别人」。
- 现在还在用：**是**。

### 13. 连不上 NapCat 的报错分四类
- 时间：`04a052e`（2026-10-26）
- 问题：所有连接失败都报同一句话，用户无从下手。
- 解法：按真实原因分四类（没装 / 没起 / 端口不通 / 版本不兼容），并给出下一步。
- 现在还在用：**是**。

### 14. NapCat 目录输入归一化
- 时间：`6968b60`（2026-10-26）
- 问题：用户粘贴带引号、反斜杠的路径，保存后输入框被清空。
- 解法：输入归一化；保存成功不清空。
- 现在还在用：**是**。

### 15. 二维码过期判定
- 时间：`52607c0`、`5ba888f`（2026-10-26）
- 根因：`/api/napcat/status` 只有 `qr_stamp`，页面无法判断二维码是否还新鲜，
  用户拿着一张早就过期的图在扫。
- 解法：加 `QR_STALE_SECONDS=120`，导出 `qr_age_seconds`/`qr_stale`，页面换提示文案。
- 现在还在用：**是**。

---

## 数据库与迁移

### 16. 无 schema 版本号，靠列差集迁移
- 位置：`store.py:184-202`
- 现状：11 张表全 `CREATE TABLE IF NOT EXISTS`；**没有 `PRAGMA user_version`、
  没有 `schema_version` 表**。升级靠 `PRAGMA table_info(...)` 取差集 +
  `ALTER TABLE ADD COLUMN`，字典在 `store.py:143`（messages 1 列）、`:145-159`（tasks 13 列）。
- 限制：只能加列，不能改列类型、不能删列、不能重命名。重写若需要这些，得先引入版本号。
- 现在还在用：**是**。

### 17. 数据库损坏无恢复路径
- 实测（只读调查）：写随机字节进 `digest.sqlite3` 后 `Store(p)` →
  `DatabaseError: file is not a database`，文件原样保留，无 `.bak`、无 rename、无重建。
- `doctor.py:196-197` 只把异常打成一行「数据库打不开或已损坏」。
- 用户手动备份：`GET /api/backup`（`webapp.py:2259-2295`，`mode=ro` → `source.backup()` → zip）。
- 现在还在用：**是**。

### 18. 测试用 `data/tasks.sqlite3` 是遗留产物
- 代码零引用（仅 `tests/test_webapp.py:1354` 用同名临时文件）。真实库只有
  `service.py:51-54` 那一份 `digest.sqlite3`。
- 现在还在用：**否**（遗留，清理候选）。

---

## 前端与界面

### 19. 「看着绿其实是假绿」——五处
- 时间：`bd15248`（2026-10-25）
- 内容：页面拆成四块脚本时，修掉五处「断言绿了但实际不工作」的地方。
  同一类问题在 2026-10-27 的社区在线数 bug 又出现一次（见第 20 条）。
- 现在还在用：**是**。

### 20. 社区在线数永远显示「暂不可用」
- 时间：2026-10-27（工作区未提交，`community.py` / `guide.js` / `webapp.py`）
- 根因三层叠加：
  1. `guide.js` 直连 `fetch('/api/community/counts')` **不带 token** → 一直 401；
  2. ping 挂在 `GET /api/onboarding` 上，而首屏早就不调这个接口 → **从来没上报过**；
  3. `guide.js` 在 `api()` 定义之前就同步执行。
  外加：ping 的 timeout 只给 1 秒，而境外计数端点实测要 0.9–1.3 秒 → 大概率超时。
- 解法：改走 `api()`；ping 挂到页面每次都拉的 `/api/community/counts`；等 DOMContentLoaded；
  timeout 提到 3.0；`online is None` 时不写缓存（否则 null 被锁 60 秒）。
- 现在还在用：**是**（工作区）。

### 21. 服务自检 32.7 秒 → 10.2 秒
- 时间：2026-10-27（工作区）
- 根因：同一个请求里 napcat/微信/钉钉/学习通的状态**各算两遍**，
  外加 `tailscale serve status` 子进程跑两次（单次约 2 秒）。
- 解法：`_route_get_api_selfcheck` 先算一次挂 `self._precomputed_status`（`webapp.py:2417-2423`），
  `_overview_payload` 复用（`:1745`），`finally` 置 None；`serve_config` 加 5 秒 TTL 缓存
  （`tunnel.py:51-52,67-70`），`enable_funnel` 后清（`:202`）。
- 注意：`_overview_payload` **保持无参签名**，因为测试替身 `fake_overview(_handler)`
  多传参数会 TypeError。
- 现在还在用：**是**（工作区）。

### 22. 学习通判活误判过期
- 时间：`8434e2f`（2026-10-26）
- 根因：原来靠 HTTP 状态码，校级泛雅平台会 302，被当成 cookie 过期。
- 解法：改用 SSO `userLogin4Uname.do` 的 `result` 字段。
- 现在还在用：**是**。

### 23. 微信密钥四分类
- 时间：`4e70ce7`（2026-10-26）
- 内容：支持 `.env` 手填 raw key，失败按四类（找不到库 / 无密钥 / 库损坏 / 读不出）分开提示。
- 现在还在用：**是**。

### 24. `#platform-setup-wizard` 新浏览器必弹一次
- 判定（2026-10-27 前端排查）：纯前端 localStorage 控制（`page.html:2658` 读
  `nh_setup_wizard_seen`），后端不参与。**设计如此，不是 bug**。
- 已知理论风险：弹前不查 `/api/setup/status`，全配好的安装换个浏览器仍被弹一次。
  URL 加 `&nowiz=1` 可跳过（`page.html:2577`）。
- 现在还在用：**是**。

---

## 路由与拆分（工作区，未提交）

### 25. 路由表化（R1）
- `webapp.py` 里 90+ 条 `if path == ...` 分支抽到 `qq_live_digest/routes.py` 两张 dict
  + 两条前缀模式。`do_GET`/`do_POST` 只做查表 + `getattr`（`webapp.py:2925-2947`、`:3588-3605`）。
- 好处：`tests/test_routes.py` 能用一份契约锁死路由集合，拆分层时不会漏。
- 现在还在用：**是**。

### 26. 设置接口拆分（R2）
- `_route_post_api_settings` 从 318 行拆成 66 行派发 + 9 个域方法 + 1 个 hosting 兜底
  （`settings_api.py`）。
- 过程中踩过一次：`_save_push_settings` 用了拆出后越界的局部名 `push_keys`，
  NameError 被兜底 `except` 吞成 200 `ok:false`（本该 400）。已修为内联等价表达式。
- 教训：**兜底 `except` 会把 NameError 变成「接口返回正常但什么都没干」**，
  这是本项目最容易出现的一类静默失效。
- 现在还在用：**是**。

### 27. `settings_api.py:20` 的 `from . import lab` 未使用
- 已删（工作区）。`lab` 只剩 `:402` 的字典键字符串。
- 现在还在用：**是**（已删）。

---

## CI 与门禁

### 28. CI 五个 job + 汇总门禁
- `.github/workflows/tests.yml`：`unittest`（windows/py3.12）、`linux`、`macos`、
  `tray-smoke`（`-NoUi`）、`browser`（playwright），汇总 `ci-green`（`:154-171`）。
- `ci-green` 规则：任一 job 非 success 就红。**这是推 main 的硬标准（用户 2026-10-27 拍板）**，
  不许用 skip / 降级 / 改断言绕过。
- tray-smoke 只跑 `-NoUi` 的原因写在 `:56-61`：GitHub runner 跑在服务会话，
  没有可读任务栏，UIA 三段只会 exit 3 或假红。
- 现在还在用：**是**。

### 29. 测试在 CI 上被用户名 / 代码页 / 短路径搞崩过多次
- `812d554`、`38e53e1`、`40c18d7`、`9a1a40f`（2026-10-12 ~ 10-13）
- 具体：GBK 夹具脱离 ambient 代码页、GitHub runner 的 8.3 短路径、
  用户名/短名相关断言。逐个改成与这些无关。
- 教训：测试里任何依赖本机用户名、路径、代码页的断言都会在 CI 上炸。
- 现在还在用：**是**。

### 30. netsh 打桩打错了模块
- `597ffd8`（2026-10-27）：netsh 实际在 `host_actions.subprocess` 里跑，
  打桩打在别处 → linux/macos 上 `FileNotFoundError`。
- 教训：打桩要打在**真正发起子进程的那个模块**。
- 现在还在用：**是**。

---

## 待确认（推测，未取证）

1. `qq_digest.py` 这个顶层模块与 `qq_live_digest/` 包的关系：`main.py:34` 是
   `from qq_digest import Message`，仓库根有 `qq_digest.py`。它是否还有独立演进、
   是否该并入包内，git 历史里看不出明确决策。
2. `data/tasks.sqlite3` 是哪一版引入的、为什么废弃，无 commit 记录。
3. `docs/qa/backlog-beta12.md` 自称「`PAGE_SOURCE` 类断言仍有约 325 条，属两周量」，
   这个估算的依据和口径未见记录。
4. 浮球（`floatball_*.py`，`FLOATBALL_ENABLED=False`）暂停不重做是用户 2026-10-22 拍板，
   但代码仍在仓库里并参与 CI（`33720d5` 跳过其用例）。

---

## 5. 重写视角的三条总结

1. **打包/更新的坑都集中在「Windows 特有行为」上**：只读属性位、`\` vs `/` 分隔符、
   exe 自锁、8.3 短路径、GBK 代码页。这些在 clean-room 重写里**必须重新踩一遍**，
   照抄本文的解法比重新试错快得多。
2. **「静默失效」是本项目最高频的 bug 类型**：第 6、11、20、26 条全是同一模式 ——
   前端显示正常/绿灯，链路实际是断的。共同特征是**有一个兜底 `except` 把异常吞掉**。
   重写时对每个「看起来成功」的路径都要问一句：失败了会怎样？
3. **测试债比代码债硬**：`tests/test_webapp.py` 6187 行，`PAGE_SOURCE`/`PAGE_HTML`
   类断言 255 条（全 tests 目录 617 处引用），锁的是页面模板源码片段而非行为。
   `docs/qa/backlog-beta12.md` 自己记了「仍有约 325 条，属两周量」。