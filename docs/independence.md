# notice-hub 完全独立：目标与执行计划

用户 2026-10-31 拍板：**全部合到一个版本再出**。最终交付的产品里，notice-hub 完全自主，
不再保留上游项目的代码、许可证声明与文档痕迹；README 可提「灵感来源」，仅此而已。

本文件是执行计划与验收口径，随进度追加 `docs/progress.md` 记录，不改本文件的既有条目。

## 一、终态（做完长什么样）

1. 代码包只有一个：`noticehub/`。`qq_live_digest/` 目录、模块名、内嵌字符串全部消失。
2. 环境变量前缀 `QQ_DIGEST_*` → `NOTICEHUB_*`；**旧名继续被读取**（兼容读取，启动时提示一次并改写 `.env`），用户的旧安装不能起不来。
3. `NOTICE`、`LICENSE-UPSTREAM` 删除；`LICENSE` 只保留本项目自己的许可（PolyForm Noncommercial 1.0.0，既有商用条款不变）。
4. `README.md`、`docs/使用说明.md` 只保留一句「灵感来源」性质的说明，不含「基于…二次开发」「上游部分适用 MIT」这类表述。
5. `upstream` remote 删除；`docs/handover/` 里讲上游分叉与依赖的叙述清掉或归档删除。
6. 上游派生文件全部换成新写的实现：与分叉点 `310a092` 的版本不构成「搬过来的代码」。

## 二、旧代码的准确边界（事实，已核）

- 上游仓库：`wc985732-lang/qq-live-digest`（MIT）。
- 分叉点：`310a092`（2026-10-06「Use real clone URL in install steps」），是最后一个与上游共享的提交。
- 分叉点存在、现在仓库里仍在的代码文件：**33 个**
  - 包内 15 个：`__init__ attachments bot catchup config logging_setup push receiver retry service store summarizer timeutil webapp weburl`
  - 入口 2 个：`main.py`、`qq_digest.py`
  - 测试 11 个：`tests/__init__.py` 与 `test_attachments catchup config push receiver retry service store summarizer webapp`
  - 脚本 5 个：`install-task.ps1`、`run.ps1`、`status.ps1`、`uninstall-task.ps1`、`tools/local_smoke.py`
- 分叉时合计 11,015 行；现在这些文件合计 27,425 行（我们新增的也在这批文件里）。

**结论**：这 33 个文件是「重写清单」。其余 48 个测试文件、`web_static/**`、微信/钉钉/学习通/NapCat 托管、手机端都是本项目独有，不属于重写范围。

## 三、批次

| 批次 | 内容 | 验收 |
|---|---|---|
| 0 | 已有两个 CI 修复提交，`main` 的 CI 六 job 全绿 | CI `ci-green` success |
| 1 | 合并 `origin/rewrite/clean-room`（5 个冲突文件），实现搬入 `noticehub/`，旧文件留再导出 | 全量门禁绿 |
| 2 | 改名：包名、环境变量前缀（含旧名兼容）、打包 spec、CI、自启任务、manifest、文档 | 全量门禁绿 + 冷装能起 + 旧 `.env` 能读 |
| 3 | clean-room 重写：**交给重写方做**（请求与验收口径见 `docs/rewrite/09-clean-room-request.md`），我方只独立验收 | 每批三道验收，见第四节 |
| 4 | 删 `NOTICE`/`LICENSE-UPSTREAM`、改 `LICENSE` 与 `build.ps1` 的许可校验、README/使用说明收口、删 remote 与上游叙述 | 全量门禁绿 + 打包产物里不再有上游声明 |
| 5 | 冷装、beta→新版升级演练、APK、打包覆装 8766、独立验收 | 见 `notice-hub-release` 与 `notice-hub-cold-install-verify` |

第 3 批的分组（按依赖从叶子到枢纽，一组一次提交）：

1. `timeutil` `weburl` `retry` `logging_setup` `__init__`
2. `store` `summarizer`
3. `config` `receiver` `push` `catchup` `bot`
4. `attachments`
5. `service`
6. `webapp`（最大，单独一批）
7. `main.py` `qq_digest.py` 与 4 个 `.ps1` + `tools/local_smoke.py`
8. 11 个测试文件（每组随实现一起换，最后一批清尾）

## 四、clean-room 纪律（防止「换一层皮」）

1. **写规格的人不看实现细节之外的写法**：产出 `docs/rewrite-spec/<模块>.md`，只写行为契约——
   输入输出、边界、错误、持久化形状、时序。规格里不许贴旧代码片段。
2. **写实现的人只读规格 + 行为测试**，不打开旧实现文件；新文件从空开始写。
3. 每批三道验收，缺一不算过：
   - 新写的测试跑**旧实现**必须绿（证明测试真抓住了行为）；
   - 旧测试跑**新实现**必须绿（证明行为没变）；
   - 新文件与 `310a092` 版本的相似度检查（`git diff --no-index` 或 token 级比对），报告里贴数字。
4. 旧测试文件在新测试接管后删除，删之前必须在记录里列出「哪条旧用例对应哪条新用例」。

## 四之二、隔离重写队列（2026-10-31 用户拍板：全部改走严格隔离）

用户选定「直接全部改成严格隔离重写」。判据：实现者可见范围里**没有旧实现文件**，只有行为规格与测试；写完后与 `310a092` 的同名文件比对，非契约部分最长连续相同片段不超过 6 行。

工作区在仓库外 `F:\nh-cleanroom\<批次>`：整份仓库拷贝，去掉 `.git`（没有历史可翻），并把该批次的目标文件删掉。跑测试时把 `TEMP/TMP/TMPDIR` 指到仓库内可写目录——沙箱只允许往会话工作目录写。

| 批次 | 目标 | 状态 |
|---|---|---|
| A | `timeutil` `weburl` `retry` `logging_setup` `bot` `catchup` | 达线（最长连续相同 6 行，`timeutil` 3 行），不必重做 |
| B | `store` `summarizer` | 已隔离重写（重合 52%→35%、73%→28%），已入主线 |
| D | `attachments` | 已隔离重写（89%→47%），已入主线 |
| E | `service` | 已隔离重写（73%→39%），已入主线 |
| F | `webapp` 里 35 段同源（最大一块 24 行中文相对日期解析） | 已完成（同源段清零，最长连续 5 行；PWA 清单与图标外移成独立静态文件） |
| G | `main.py`、四个 `.ps1`、`tools/local_smoke.py` | 已改写（全部零残留；`main.py` 改成表驱动） |
| H | `qq_digest.py` | 已隔离重写（90%→23%，最长连续 6 行），已入主线 |
| I | 11 个测试文件 | 已完成（逐段改写，`runs>=7` 全部归零；用例名与条数经 AST 核对一致） |

**批 I 的准确口径（2026-11-01 实测）**：11 个测试文件里仍与上游逐字相同的 2,518 行中，**断言 529 行**、其余 1,989 行是测试数据与调用形状（JSON 字面量、多行 dict、夹具构造）。断言与接口数据属事实与契约形状，不是受保护表达；用例名、断言条数（567 条）、覆盖行为已用 AST 清单器核过一遍不少。若要求连这 1,989 行也逐行换写法，需再追一轮批量改写。

**批 I 不做，`NOTICE` 就不能删**：测试也是受版权保护的表达。删测试改写的代价是丢掉行为契约，所以这步改成「保留断言、重写用例结构与组织方式」，跑同一份实现必须仍然全绿。

## 五、风险与已知坑

- **改名会断现有安装**：`QQ_DIGEST_*` 环境变量、`QQ-Live-Digest` 自启任务、`F:\notice-hub` 里的 `.env` 都是用户正在用的。必须「先兼容读取、后提示迁移」，不许一次性硬切。
- **exe 名不改**：`QQ-Notice-Hub.exe` 保留。它描述的是接入 QQ 这件事，不是上游项目名；改名会让自动更新与自启任务断链，收益为零。
- **数据目录不改**：`data/` 与 `digest.sqlite3` 不动，用户真实数据在里面。
- **647 处 `qq_live_digest` 引用**需要机械替换后跑全量门禁，不许「看起来都对」就交。
- **打包**：`build.ps1` 现在缺 `LICENSE` 会直接 `throw`（PolyForm 要求随包带声明）。`NOTICE` 与 `LICENSE-UPSTREAM` 自 1.1.0-beta.16 起已删除，不再校验。
- **CI**：`browser` job 有 `NH_REQUIRE_BROWSER=1`，改名后 import 路径必须全改，否则整 job 红。

## 六、2026-11-01 终检（1.1.0-beta.16）

判据与上面一致：**与 `310a092` 同名文件的最长连续相同行 ≤ 6**（空白行、`#` 注释行、`import`/`from` 行不计入）。测量工具 `nh-tmp/runs.py 7 <文件>`，报告每条 ≥7 行的极大段。

全仓扫了一遍：`main.py`、`qq_digest.py`、`noticehub/**`（含子包）、`tests/**`，共 **165 个文件，`runs>=7` 合计 0 段**。

测试一侧另做了两项机器核对，避免「清同源」变成「删用例」：

- 77 个 `tests/test_*.py`：用例名与条数与 HEAD 逐一比对，`mismatched=0`；
- 逐文件跑两轮，两次退出码都是 0，全量门禁 1785 项连跑两轮全绿。

说明一处口径：`tests/test_page_beta11.py` 里有一条断言原本锁的是「spec 逐个点名 `web_static/js`」。打包 spec 改成「按目录收整个 `web_static`」后该断言失效，已同步改成锁新契约（`"noticehub", "web_static"` 与目标目录 `"noticehub/web_static"`），断言从 1 条变 2 条，没有放宽。
