# tools/

装包 / 渲染的端到端验收脚本。都用 Windows PowerShell 5.1（Desktop）跑，脚本本身只用 ASCII。

| 脚本 | 干什么 |
| --- | --- |
| `e2e-install.ps1` | 把发布 zip 当普通用户那样解压、首启、探活，验「包能不能装、装完能不能用」 |
| `e2e-render.ps1` | 用无头 Edge 打开控制台页面，验「页面里的 JS 真的跑起来了、真的渲染了数据」 |

`e2e-render.ps1` 补的是状态码看不出来的那一类故障：`webapp.PAGE_HTML` 里一个 JS 语法错误就能让整个仪表盘脚本死掉，页面永远停在静态占位符，而所有 `/api/*` 照样返回 200。

## 前置

- Windows + PowerShell 5.1（Desktop）。
- 已构建好的发布 zip（默认 `packaging\QQ-Notice-Hub.zip`）。
- `e2e-render.ps1` 额外需要：`node`（用来 `--check` 内联脚本）和 Edge（`msedge.exe`，不传 `-Edge` 时自动找两个默认安装路径）。
- 不需要任何第三方 PowerShell 模块。

## 一条铁律：`-InstallDir` 只能指向一次性目录

**绝对不要**把 `-InstallDir` 指向用户正在使用的那一份（当前是 8766 端口的安装）。`e2e-install.ps1` 不加 `-Overwrite` 时会 `Remove-Item -Recurse -Force` 掉整个目录，加 `-Overwrite` 时也会覆盖里面的文件——指错了就是删用户的数据。

统一用一次性目录，例如 `D:\nh-tools` 或 `%TEMP%` 下的某个新目录，验完就删。`e2e-render.ps1` 可以读 `-InstallDir\\.env` 拿 token，除此之外不改任何东西。

另外 `e2e-install.ps1` 跑完**不会**自动关闭启动的进程，跑完请自己收尾（见下）。

## e2e-install.ps1

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\e2e-install.ps1 `
  -Zip packaging\QQ-Notice-Hub.zip `
  -InstallDir D:\nh-tools `
  -Port 8877
```

参数：

- `-Zip <路径>`（必填）：发布 zip。
- `-InstallDir <路径>`（必填）：一次性安装目录。脚本会自己建；不加 `-Overwrite` 时会先整目录删掉再重建。
- `-Port <int>`，默认 `8766`：探活/API 用的端口。**别用 8766 跑临时实例**（那是用户在用的那份所在的端口）；一次性实例用 8877 之类的空端口。非 8766 端口上，脚本会先给该目录种一份 `.env`（否则启动器会去抢 8766）。
- `-Token <字符串>`：访问 token。省略时脚本从该目录的 `.env` 自己读，读不到才需要显式传。
- `-HealthTimeoutSeconds <int>`，默认 `180`：等 `/api/health` 返回 200 的上限。
- `-ExpectTasks <int>`：断言 `/api/tasks` 的 `stats.total`（**不是**今天的数量）。省略（`-1`）就不断言条数。全新建的实例传 `0`。
- `-Overwrite`：**升级**路径，把新包解压覆盖到已存在的安装目录上，并断言 `data\digest.sqlite3` 与 `.env` 原样存活。注意：它要求目标目录里**已经有**这两样东西；在一个空目录上加 `-Overwrite`，C-05/C-06 两条会因「没有东西可保留」而 FAIL——**冷装不要加 `-Overwrite`**。
- `-VerifyTrayQuit`：最后再用 `QQ-Notice-Hub.exe --quit` 驱动一次「托盘退出」，断言托盘进程、子进程、端口都没了。会把应用停掉，只建议一轮的最后一次跑时加。

退出码：有 FAIL 就是 `1`，否则 `0`。

## e2e-render.ps1

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\e2e-render.ps1 `
  -Url http://127.0.0.1:8877/ `
  -InstallDir D:\nh-tools
```

参数：

- `-Url <url>`，默认 `http://127.0.0.1:8766`：要检查的实例地址。请指向自己那份一次性实例。
- `-Token <字符串>`：访问 token。省略时若给了 `-InstallDir`，就从该目录的 `.env` 里读。
- `-InstallDir <路径>`：用来取 `.env` 里的 token，脚本不会写这个目录。
- `-Edge <exe 路径>`：`msedge.exe` 的显式路径，不传就自动找默认安装位置。
- `-Node <exe>`，默认 `node`：用来逐个 `node --check` 页面里的内联 `<script>`。
- `-RenderBudgetMs <int>`，默认 `20000`：无头渲染的虚拟时间预算。
- `-ExpectTasks <int>`：断言仪表盘标题里「今天 N 件」的 **N（今天到期的条数）**。省略（`-1`）时接受任何 overview() 能合法产出的标题；传 `0` 时要求空状态标题。注意这个参数和 `e2e-install.ps1` 的同名参数不是一个东西（那是 `stats.total`）。

退出码：全过 `0`，有 FAIL `1`，既没给 `-Token` 也没在 `.env` 里找到 `2`。

### 为什么它在空数据的库上也能过

标题 `<h1 id="headline">` 是 `render()` 拿到 `/api/tasks` 之后才写进去的，而 `overview()` 对「一条待办都没有」的库也有自己的合法标题。所以渲染检查断言的是：

> 这个元素已经**不再是**页面里那个静态占位符，而是 `overview()` 会产出的某一条标题。

这样既能在空库上成立，又确实能抓住「脚本没解析 / JS 报错 / 请求失败 / 仪表盘没渲染」——那些情况下这个元素会一直停在占位符或「加载失败」。想收紧到具体条数就用 `-ExpectTasks N`，不要靠把断言放宽成「有点字就算过」。

## 一次典型的自证流程

```powershell
# 1. 冷装到一次性目录（不要 -Overwrite，目录里本来就没东西可保留）
New-Item -ItemType Directory D:\nh-tools -Force | Out-Null
powershell -NoProfile -ExecutionPolicy Bypass -File tools\e2e-install.ps1 `
  -Zip packaging\QQ-Notice-Hub.zip -InstallDir D:\nh-tools -Port 8877

# 2. 在空库状态下验渲染
powershell -NoProfile -ExecutionPolicy Bypass -File tools\e2e-render.ps1 `
  -Url http://127.0.0.1:8877/ -InstallDir D:\nh-tools
```

## 收尾

`e2e-install.ps1` 会把实例留在运行状态，验完自己关：

```powershell
D:\nh-tools\QQ-Notice-Hub.exe --quit
Start-Sleep -Seconds 3
if (Test-Path D:\nh-tools) { Remove-Item -LiteralPath D:\nh-tools -Recurse -Force }
```

删之前最好 `Test-Path D:\nh-tools` 确认路径真是那份一次性目录。`e2e-render.ps1` 在 `%TEMP%` 下留的 `nh-uicheck-*` 工作目录（含 DOM 与 console 日志）通常不用管，排查时按它打印的 `workdir:` 去看。
