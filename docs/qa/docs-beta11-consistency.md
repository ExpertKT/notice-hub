# 文档与 1.1.0-beta.11 行为一致性核查

范围：`docs/使用说明.md`、`docs/napcat-setup.md` 里与 beta.11 实际行为冲突的说法。README 顶部 beta.11 段落由 Lead 改，未在本次范围内（其 5 条声明逐条核过，与代码一致）。

判定口径：文档写的做法/路径/判断条件，会让用户按文档操作后得到与程序实际行为不同的结果，才算冲突。单纯「没写到新功能」不算冲突，单独列在文末「建议补写」。

**本文只列不改。**

行号基于 2026-10-24 工作区（beta.11 未提交改动已在位）。

## A. `docs/napcat-setup.md`

| 位置 | 现在写的 | 应该改成什么 |
|---|---|---|
| `docs/napcat-setup.md:33` | 「它会自己确认官方 Release 的最新版本、下载 `NapCat.Shell.Windows.OneKey.zip`…」 | 程序下载的是 `NapCat.Shell.zip`（`qq_live_digest/napcat_admin.py:23` 的 `NAPCAT_SHELL_ZIP_URL`，地址里钉死 `v4.18.33`，不查「最新版本」）。OneKey 只是 994 KB 安装桩，**不是**程序用的那个包 |
| `docs/napcat-setup.md:35` | 「程序只从官方 GitHub Release 下载**约 1 MB** 的 `NapCat.Shell.Windows.OneKey.zip`」 | 「下载官方 `NapCat.Shell.zip`（完整 Shell 包，不是 1 MB 的 OneKey 安装桩），解压到 `<数据目录>\napcat`」 |
| `docs/napcat-setup.md:37` | 「程序默认只在 `F:\NapCat`、`F:\napcat-dl` 和 `<数据目录>\napcat` 三处找引擎」 | 实际基线是 `qq_live_digest/napcat_admin.py:71-88` 的 `find_roots()`：`APPDATA`、`LOCALAPPDATA`、`ProgramFiles`、`ProgramFiles(x86)` 四个目录本身及其下的 `napcat`/`NapCat*` 包、`QQ_DIGEST_NAPCAT_DIR`、`QQ_DIGEST_DATA_DIR\napcat`，外加注册表里 QQ 安装目录及其下的 `NapCat.*` 包。**没有**硬编码 `F:\NapCat` 或 `F:\napcat-dl` |
| `docs/napcat-setup.md:39` | 「写 OneKey 解压出来的那一层，里面有 `QQ.exe`、`NapCatWinBootMain.exe`、`NapCatWinBootHook.dll`」 | NapCat Shell 包里**不含** `QQ.exe`（它就是挂载到官方 QQ 上的外挂引擎）。引擎目录的判定是 `NapCatWinBootMain.exe` + `NapCatWinBootHook.dll`（`qq_live_digest/napcat_admin.py:116`、`:662`）；写成「里面有 QQ.exe」会让用户去填官方 QQ 的安装目录 |
| `docs/napcat-setup.md:44` | 「装好解压出 `NapCatWinBootMain.exe` + `QQ.exe` + `NapCatWinBootHook.dll` 之后…」 | 同上，去掉 `QQ.exe`：「装好解压出 `NapCatWinBootMain.exe` + `NapCatWinBootHook.dll` 之后…」 |
| `docs/napcat-setup.md:76` | 「`detect_boot()` 只识别同时具备 `QQ.exe` + `NapCatWinBootHook.dll` 且非纯壳的同级目录」 | `detect_boot()`（`qq_live_digest/napcat_admin.py:618-670`）认的是 `NapCatWinBootMain.exe` + `NapCatWinBootHook.dll`；`QQ.exe` 不是判定条件。「绝不返回你日常 QQ 的 QQ.exe」这句保留 |
| `docs/napcat-setup.md:76` | 「程序不替你安装 QQ」 | 语义仍成立（程序不自动装 QQ），但要补 beta.11 的实际处理：检测不到 QQ 时页面给出「下载官方 QQ」按钮 + 三步引导（下载 → 装完不用打开 → 重新检测），已装仍检测不到可在 `.env` 加 `QQ_DIGEST_QQ_EXE=<QQ.exe 完整路径>`；QQ 装在 `C:\Program Files` 时写加载器要管理员权限，这条现在是单独提示 |

## B. `docs/使用说明.md`

| 位置 | 现在写的 | 应该改成什么 |
|---|---|---|
| `docs/使用说明.md:44` | 「程序不替你安装 QQ（**你本来就有**）」 | 后半句删掉或改成「程序不替你装 QQ；没检测到 QQ 时会给出下载入口和三步引导」——beta.11 专门处理「没装 QQ / 装了检测不到」这两种情况 |
| `docs/使用说明.md:44` | 「程序会从官方 GitHub 下载**约 1 MB**」 | 「程序会从官方 GitHub 下载官方 `NapCat.Shell.zip` 并解压到 `<数据目录>\napcat`」。「约 1 MB」是 OneKey 安装桩的体积，不是实际下载包 |
| `docs/使用说明.md:219` | 现象：「接入向导显示**「QQ 未连接」**」 | beta.11 页面不再用这句。现在会显示「等待 QQ 登录（已等 N 秒）…」或「NapCat 引擎装了但没在运行：点上面的「启动并登录」」。排错表的现象名要跟着改，否则用户按字面找不到这句话 |
| `docs/使用说明.md:220` | 「二维码约 2 分钟刷新一次，**等它自己换一张**再扫」 | 「二维码换新会自动刷新；一直不变就点「重启引擎」再扫。页面会显示已等多少秒（`waiting_seconds`），别靠掐表」 |
| `docs/使用说明.md:44` | （无） | 建议补一句装机排错出口：「装了 QQ 还是提示未找到：`.env` 里加 `QQ_DIGEST_QQ_EXE=<QQ.exe 完整路径>` 重启再检测；QQ 在 `Program Files` 下需要管理员权限运行」 |

## 合计

- 冲突：**12 条**（A 组 7 条、B 组 5 条）。
- 最高优先级（用户装机第一步就会撞上）：`docs/napcat-setup.md:35`、`docs/napcat-setup.md:39`、`docs/使用说明.md:44`。
- 两类共性问题：①「NapCat 包里有 QQ.exe」这个错误认知在 `napcat-setup.md` 出现 **3 处**（`:39`、`:44`、`:76`），是 beta.11 已在 UI 里纠正过的说法，文档没跟着改；②引擎搜索路径、下载包名（OneKey vs Shell）都是历史状态，代码早改了文档没改。

## 建议补写（不算冲突，但用户会去找）

1. `docs/使用说明.md` 第八节「出问题了」缺一行「**提示未找到电脑版 QQ**」——这是 beta.11 的修复主线，用户第一晚就会撞上。
2. `docs/使用说明.md` 未提「接入状态」的四个阶段（未安装 / 没启动 / 等你扫码 / 已登录，即 `/api/napcat/status` 的 `stage`）与「已等 N 秒」，而这正是 beta.11 用来解决「扫了没反应」的抓手。
3. `docs/napcat-setup.md:44` 说「页面上的「托管状态」会显示 NapCat 是否在跑、是否已登录」——beta.11 已细分为四个阶段，可顺手把这句换成阶段名。
