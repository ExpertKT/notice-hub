# release.md — 打包与自动更新流程

基准：HEAD `ad67e3e` + 工作区未提交改动。**本文不含任何发布操作指引中的密钥/账号细节**，
需要人工的部分集中在最后一节。

---

## 1. 打包入口

```powershell
cd F:\qq-notice-hub
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build.ps1
```

注意：`build.ps1:7` 写死 `$root\.venv\Scripts\python.exe`。在别的机器上必须先
`python -m venv .venv && .\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller`。

内部步骤（`packaging/build.ps1`）：

| 行 | 动作 |
|---|---|
| `:10` | 清掉旧 `packaging\dist`（含只读资产，否则 PyInstaller 清理 WinError 5） |
| `:13` | 单独删旧的 `dist\qq-live-digest.exe` |
| `:14` | PyInstaller 跑 `qq_live_digest.spec` → service 本体 |
| `:16` | PyInstaller 跑 `launcher.spec` → 托盘/launcher |
| `:19-25` | 校验四个产物目录/文件必须存在 |
| `:26-27` | 把 `QQ-Notice-Hub.exe` 和 `_launcher_internal` 拷进 service 目录 |
| `:28-30` | 拷教程 txt |
| `:33-37` | 拷**所有** `.cmd`（旧写法 `Select -First 1` 会静默丢一个，`:31-32` 注释） |
| `:39-41` | 拷 `fix-napcat.ps1` |
| `:42-44` | 拷 `docs\*.md` |
| `:46-48` | 拷 `assets\*` |
| `:50-63` | 拷 `run.ps1`/`install-task.ps1`/`uninstall-task.ps1`/`status.ps1` 与两个 tools 脚本 |
| `:68-74` | `LICENSE`/`LICENSE-UPSTREAM`/`NOTICE` 缺失直接 `throw`（MIT + PolyForm 都要求带声明） |
| `:77-80` | 清旧 zip 的只读位再删 |
| `:84-91` | 递归清 `appRoot` 下所有文件的只读位 |
| `:101-118` | **手写** `ZipArchive`：条目名 `.Replace('\', '/')`，不带顶层目录 |

三个必须记住的坑（都在注释里，见 history.md 第 1–4 条）：
1. zip 顶层不能包进 `qq-live-digest\` 目录 —— 否则生产跑旧 exe。
2. 条目必须用 `/` —— 否则 Linux/macOS 解压全烂。
3. 必须清只读位 —— 否则用户解压覆盖失败。

## 2. 产物位置

- `packaging\dist\qq-live-digest\`：完整安装根（含 `QQ-Notice-Hub.exe`、`_launcher_internal`、
  `docs\`、`assets\`、`tools\`、`LICENSE*`、`NOTICE`）
- `packaging\dist\QQ-Notice-Hub\`：launcher 单独产物（中间态，构建时被拷走）
- **`packaging\QQ-Notice-Hub.zip`**：最终分发包，顶层即安装根

## 3. 版本号在哪定义

**唯一来源：`qq_live_digest\version.py`**

```python
APP_VERSION = "1.1.0-beta.12"   # :12
APP_CHANNEL = "beta"            # :13  "stable" | "beta"
```

`:7` 注释明确「只许往前推。发布时由维护者改这里的值，别在别处硬编码版本字符串」。
`launcher.py` 与网页端都 import 它；`updater.check()` 用它和 GitHub release tag 比较。

另外 `release/update-manifest.json` 里要登记同一版本的 `size` 与 `sha256`
（`8d9618d` 之后 `tools/gate.ps1` 会校验这个清单）。

## 4. SHA-256 校验链路

| 环节 | 位置 | 事实 |
|---|---|---|
| 打包时算 hash | — | **不算**。`build.ps1` 无任何 hash 调用 |
| 发布前登记 | `tools/gate.ps1:62` | `Get-FileHash -Algorithm SHA256`，但只用于 mtime 新鲜度打印（`:55-57` 有 ponytail 注释承认天花板） |
| 更新检查 | `updater.py:301 check()` | 从 GitHub API + Gitee 清单取 `sha256`；GitHub 优先，Gitee 兜底（`:375-379`） |
| 下载 | `updater.py:465 download()` | **边下边算**：`:513 digest = hashlib.sha256()`，`:534 digest.update(chunk)` |
| **比对点** | `updater.py:544-549` | `actual != expected` → 删文件 + `ok=False` |
| 无 sha256 拒装 | `updater.py:497-500` | 「这个版本没有提供 sha256 校验值，为安全起见不自动安装；请手动下载」 |
| 安装脚本复算 | — | **没有**。`:667-855` 无任何 hash 校验，信任下载阶段结果 |
| 签名 | — | **完全没有任何签名环节**。全仓 `sigstore\|authenticode\|Set-AuthenticodeSignature\|codesign\|gpg` 零命中 |

**无签名的实际后果**：Windows SmartScreen 会对未签名 exe 弹「Windows 已保护你的电脑」，
用户需点「更多信息 → 仍要运行」。这是当前已知的用户摩擦点，没有代码层缓解手段。

## 5. 更新源与通道

`update_sources.py`：

- GitHub 主源 + Gitee 镜像（`ExpertKT/notice-hub-mirror`，可用
  `QQ_DIGEST_UPDATE_MIRROR` 覆盖，`:15,24-30`）
- 清单路径 `MANIFEST_PATHS = ("release/update-manifest.json", "update-manifest.json")`（`:54`），
  `release/` 优先、根目录兼容兜底。`:52-53` 注释记着：以前按根目录拼 URL，
  **beta.11 及更早从来没拉到过 Gitee 清单**，只是 GitHub 能兜住。
- 下载 URL 排序（`updater.py:381-410`）：**只有两边 sha256 都非空且相等时**才把 Gitee 提到首位；
  否则 GitHub 优先，Gitee 仅在 sha 未知时作 fallback（`:393-396` 注释：避免拿到错包）。
- 通道（`version.py:13`）：`stable` → 查 `/releases/latest`；`beta` → 查 `/releases` 列表。
  用户要求：正式版用户不会被推测试版（tag 含 `-` 过滤）。

## 6. 安装/应用更新

`updater.py:856 apply_async()`：

1. 写 `applying.flag` + 生成临时 PowerShell 脚本（`_script_text`，`:667`）
2. 脚本用 `Start-Process launcher --quit -Wait` 请求优雅退出（`:702`）
3. 等目标进程最多 60 秒（`:708-712`），超时按安装目录兜底结束进程（`:713`）
4. 再等 2 秒让文件句柄释放（`:715`）
5. `Expand-Archive -Force`（`:773`）
6. 探 `/health` 3 次 × 2 秒；失败给人话兜底（`:849`）
7. `os._exit(0)` 让位（`webapp.py:3457` 对应路由 `POST /api/update/apply`）

`pending.json`（`updater.py:569-631`）：下载完成后 `mark_pending` 写，
`pending_update` 读、`clear_pending` 清；损坏或缺失视为没有待安装更新（`:614`）。

## 7. 重写后必须重跑的步骤

按顺序，`- [ ] 步骤 — 命令 — 通过判据`：

```
- [ ] 全量测试        — .\.venv\Scripts\python.exe -m unittest discover -s tests -t .
                        — 1325 项，OK (skipped=1)，exit 0（当前基线）
- [ ] 版本号单一来源  — 确认 version.py 是唯一硬编码处
                        — grep -r "1\.1\.0" --include=*.py 只命中 version.py 与测试
- [ ] 打包            — powershell -File packaging\build.ps1
                        — 结尾打印 "Packed N files into ..."；dist\qq-live-digest\QQ-Notice-Hub.exe 存在
- [ ] zip 内容核对    — 解压看顶层无嵌套目录、条目用 /、有 LICENSE/NOTICE、有 web_static/js
                        — 逐项在，无 ReadOnly 属性
- [ ] 冷装起实例      — 临时目录 + 随机端口 + 独立 data/log 目录
                        — curl /api/health 返回 200
- [ ] 更新检查        — 设 QQ_DIGEST_UPDATE_CHANNEL=beta，GET /api/update
                        — 返回 latest 与 has_update，无 error
- [ ] 下载+校验       — POST /api/update/download
                        — ok=true 且 sha256 与清单一致；故意给错 sha 应被拒装
- [ ] 应用更新        — POST /api/update/apply
                        — 新版本起来，/health 200，日志无 traceback
- [ ] 安卓端          — 需人工真机（见下）
```

「可本地自动验」：除最后一项全部可自动。「必须人工/真机」：安卓 App 冒烟、
托盘图标与菜单（GitHub runner 无可读任务栏，`tests.yml:56-61`）。

---

## 必须由人补的项

1. **GitHub Release 创建**：`gh release create` / 网页操作，含上传 `QQ-Notice-Hub.zip`。
2. **Gitee 镜像仓库同步**：`ExpertKT/notice-hub-mirror` 的推送与 `release/update-manifest.json` 更新。
3. **设为 Latest / Pre-release**：每版都要显式设，测试版标 Pre-release。
4. **Android APK 构建与签名**：签名密钥只能人工持有。
5. **SmartScreen 弹窗**：无签名导致，只能人工点「仍要运行」，无代码解。
6. **`tools/gate.ps1` 里的 GitHub/Gitee token**（如果校验私有清单需要）。
