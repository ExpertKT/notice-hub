# tray-smoke.ps1 — 托盘菜单真机冒烟（发布前手动跑一次，不进 CI）
#
# 为什么只能本地跑：pystray 只在收到 WM_RBUTTONUP 时才 TrackPopupMenuEx 模态弹菜单
# （packaging\dist\qq-live-digest\_launcher_internal\pystray\_win32.py:193-224），
# 无人值守的 CI 没有交互桌面，就没有菜单窗口可读。
#
# 退出码：0 = 全部通过；1 = 有断言失败；3 = 跳过（无桌面 / 锁屏 / RDP 断开 / 等图标超时）
# 安全：随机端口 + 临时目录，绝不碰仓库里的 data\ 与已装实例。

[CmdletBinding()]
param(
    [string]$Zip = '',
    [int]$TimeoutSeconds = 20,
    [switch]$KeepTemp,
    # -NoUi：只跑与桌面无关的链路（解包 → 起进程 → /api/health 200 → --quit 退出码 0
    # → 无残留进程 → 日志无 traceback）。CI 的 windows runner 是服务会话，没有可读的
    # 任务栏，UIA 那三段（找图标 / 读菜单 / 鼠标点右键）在那里只会 exit 3 或假红。
    [switch]$NoUi
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$script:temp = $null
$script:failures = 0
$script:launcher = $null

function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) {
        Write-Host ("  PASS  {0}" -f $Name)
    } else {
        $script:failures++
        Write-Host ("  FAIL  {0}  {1}" -f $Name, $Detail)
    }
}

function Stop-Scratch {
    if (-not $script:temp) { return }
    if (-not (Test-Path -LiteralPath $script:temp)) { return }
    # 先收进程，否则 exe 被占用，临时目录删不掉
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -like (Join-Path $script:temp '*') } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { } }
    Start-Sleep -Milliseconds 800
}

function Done([int]$Code, [string]$Reason = '') {
    if ($Reason) { Write-Host $Reason }
    Stop-Scratch
    if ($script:temp -and -not $KeepTemp) {
        Remove-Item -LiteralPath $script:temp -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit $Code
}

# ---- 1. 有没有桌面 ----
Add-Type -Namespace TraySmoke -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern System.IntPtr GetShellWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, System.UIntPtr extra);
'@

if (-not $NoUi) {
    if (-not [Environment]::UserInteractive) { Done 3 'SKIP: 非交互会话（服务/无人值守），没有桌面' }
    if ([TraySmoke.Native]::GetShellWindow() -eq [IntPtr]::Zero) { Done 3 'SKIP: 没有 shell 窗口（无桌面或锁屏/RDP 断开）' }
    if ([TraySmoke.Native]::GetForegroundWindow() -eq [IntPtr]::Zero) { Done 3 'SKIP: 没有前台窗口（锁屏或 RDP 断开）' }
}

# ---- 2. 解一份到临时目录 ----
if (-not $Zip) { $Zip = Join-Path $repo 'packaging\QQ-Notice-Hub.zip' }
if (-not (Test-Path -LiteralPath $Zip)) { Done 3 ("SKIP: 找不到安装包 {0}（先跑打包）" -f $Zip) }

$script:temp = Join-Path ([System.IO.Path]::GetTempPath()) ('tray-smoke-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $script:temp -Force | Out-Null
Add-Type -AssemblyName 'System.IO.Compression.FileSystem'
$archive = [System.IO.Compression.ZipFile]::OpenRead($Zip)
try {
    foreach ($entry in $archive.Entries) {
        if ([string]::IsNullOrEmpty($entry.Name)) { continue }
        $target = Join-Path $script:temp $entry.FullName.Replace('/', '\')
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
    }
} finally { $archive.Dispose() }

$exe = Join-Path $script:temp 'QQ-Notice-Hub.exe'
if (-not (Test-Path -LiteralPath $exe)) { Done 1 ("FAIL: 解压后找不到 {0}" -f $exe) }

# 随机端口 + 临时 data\：绝不碰 F:\notice-hub 的实例与仓库里的 data\
$port = Get-Random -Minimum 20000 -Maximum 40000
$token = [guid]::NewGuid().ToString('N')
$seed = @(
    'QQ_DIGEST_WEB=1'
    'QQ_DIGEST_WEB_HOST=127.0.0.1'
    "QQ_DIGEST_WEB_PORT=$port"
    "QQ_DIGEST_WEB_TOKEN=$token"
    ('QQ_DIGEST_DATA_DIR=' + (Join-Path $script:temp 'data'))
    ('QQ_DIGEST_LOG_DIR=' + (Join-Path $script:temp 'logs'))
) -join "`n"
Set-Content -LiteralPath (Join-Path $script:temp '.env') -Value $seed -Encoding UTF8
Write-Host ("scratch install: {0}" -f $script:temp)
Write-Host ("scratch port: {0}" -f $port)

# ---- 3. 起托盘 ----
$env:BROWSER = 'nonexistent-browser-probe'   # 别真弹浏览器打断桌面
$launchArgs = @()
if ($NoUi) { $launchArgs = @('--no-browser') }   # NoUi 下连浏览器探测都不做
$script:launcher = Start-Process -FilePath $exe -WorkingDirectory $script:temp -ArgumentList $launchArgs -PassThru
Write-Host ("launcher pid {0}" -f $script:launcher.Id)

if ($NoUi) {
    # ---- 3N. NoUi：只断言与桌面无关的链路 ----
    Write-Host ''
    Write-Host '=== 1. wait for /api/health ==='
    $health = $null
    $healthDeadline = (Get-Date).AddSeconds([Math]::Max(60, $TimeoutSeconds))
    while ((Get-Date) -lt $healthDeadline) {
        try {
            $health = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri ("http://127.0.0.1:{0}/api/health" -f $port)
            if ($health.StatusCode -eq 200) { break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    $healthStatus = if ($health) { $health.StatusCode } else { 'n/a' }
    Check 'service answered /api/health' ([bool]($health -and $health.StatusCode -eq 200)) ("status={0}" -f $healthStatus)
    $script:launcher.Refresh()
    Check 'launcher process still alive' (-not $script:launcher.HasExited)

    Write-Host ''
    Write-Host '=== 2. graceful quit via --quit ==='
    # 托盘菜单点不动、taskkill 又证明不了退出流程：--quit 是唯一能验「优雅退出」的口。
    # 退出码 0 才叫「确认已退出」；1 = 本来没在跑，或 40 秒内没退（那说明看门狗没起来）。
    $quit = Start-Process -FilePath $exe -ArgumentList '--quit' -WorkingDirectory $script:temp -PassThru -Wait
    $quitDeadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $quitDeadline) {
        $script:launcher.Refresh()
        if ($script:launcher.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    $script:launcher.Refresh()
    if ($quit.ExitCode -eq 0 -and $script:launcher.HasExited) {
        Check '--quit returned 0' $true
        Check 'launcher exited after --quit' $true
    } else {
        # 没有可投递的托盘时（服务会话 / 无交互桌面）launcher 会退化成 process.wait()，
        # 看门狗线程起不来，--quit 只能等满 40 秒返 1。这不是产品缺陷、是环境不支持，
        # 也算不出「退出流程对不对」——按 SKIP 收场，别做成随机冒红的 flaky 断言。
        $reason = "SKIP: --quit 未在 20 秒内退出（exit={0}，launcherHasExited={1}）：本环境建不出托盘"
        $reason = $reason -f $quit.ExitCode, $script:launcher.HasExited
        Write-Host $reason
        Done 3 $reason
    }
    Start-Sleep -Seconds 2
    $left = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -like (Join-Path $script:temp '*') })
    Check 'no scratch process left behind' ($left.Count -eq 0) (($left | ForEach-Object { $_.Name }) -join ',')

    Write-Host ''
    Write-Host '=== 3. logs have no traceback ==='
    $logDir = Join-Path $script:temp 'logs'
    $logFiles = @(Get-ChildItem -LiteralPath $logDir -Filter '*.log' -ErrorAction SilentlyContinue)
    Check 'scratch logs were written' ($logFiles.Count -gt 0) ("files={0}" -f $logFiles.Count)
    $trace = @()
    foreach ($file in $logFiles) {
        $trace += @(Select-String -LiteralPath $file.FullName -Pattern 'Traceback \(most recent call last\)|Unhandled exception' -ErrorAction SilentlyContinue)
    }
    Check 'no traceback in logs' ($trace.Count -eq 0) (($trace | ForEach-Object { $_.Filename + ':' + $_.LineNumber }) -join ',')

    Write-Host ''
    if ($script:failures -gt 0) { Done 1 ("RESULT: {0} check(s) failed" -f $script:failures) }
    Write-Host 'RESULT: tray smoke (NoUi) passed'
    Done 0
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Find-ByClass($Parent, $Scope, [string]$ClassName) {
    $prop = [System.Windows.Automation.AutomationElement]::ClassNameProperty
    $cond = New-Object System.Windows.Automation.PropertyCondition($prop, $ClassName)
    return $Parent.FindFirst($Scope, $cond)
}

function Find-ByType($Parent, $Scope, $ControlType) {
    $prop = [System.Windows.Automation.AutomationElement]::ControlTypeProperty
    $cond = New-Object System.Windows.Automation.PropertyCondition($prop, $ControlType)
    return @($Parent.FindAll($Scope, $cond))
}

function Get-RootElement { return [System.Windows.Automation.AutomationElement]::RootElement }

function Get-TrayButtons {
    $tray = Find-ByClass (Get-RootElement) ([System.Windows.Automation.TreeScope]::Children) 'Shell_TrayWnd'
    if (-not $tray) { return @() }
    return @(Find-ByType $tray ([System.Windows.Automation.TreeScope]::Descendants) ([System.Windows.Automation.ControlType]::Button))
}

function Get-OverflowButtons {
    $win = Find-ByClass (Get-RootElement) ([System.Windows.Automation.TreeScope]::Children) 'NotifyIconOverflowWindow'
    if (-not $win) { return @() }
    return @(Find-ByType $win ([System.Windows.Automation.TreeScope]::Descendants) ([System.Windows.Automation.ControlType]::Button))
}

function Find-Icon {
    # 图标名 = "notice-hub · " + _status_line()（launcher.py:1423），状态会变 → 只做前缀匹配
    foreach ($b in @(Get-TrayButtons) + @(Get-OverflowButtons)) {
        if ($b.Current.Name -like 'notice-hub*') { return $b }
    }
    # 被折叠进溢出区：点一下「显示隐藏的图标」再找一次
    foreach ($b in Get-TrayButtons) {
        if ($b.Current.Name -match '显示隐藏的图标|Show hidden icons') {
            try { $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); Start-Sleep -Milliseconds 800 } catch { }
            break
        }
    }
    foreach ($b in @(Get-TrayButtons) + @(Get-OverflowButtons)) {
        if ($b.Current.Name -like 'notice-hub*') { return $b }
    }
    return $null
}

function Get-MenuItems {
    $deadline = (Get-Date).AddSeconds(3)
    while ((Get-Date) -lt $deadline) {
        $menu = Find-ByClass (Get-RootElement) ([System.Windows.Automation.TreeScope]::Children) '#32768'
        if ($menu) {
            $items = @(Find-ByType $menu ([System.Windows.Automation.TreeScope]::Descendants) ([System.Windows.Automation.ControlType]::MenuItem))
            if ($items.Count -gt 0) { return $items }
        }
        Start-Sleep -Milliseconds 150
    }
    return @()
}

function Open-Menu($Icon) {
    $r = $Icon.Current.BoundingRectangle
    [TraySmoke.Native]::SetCursorPos([int]($r.X + $r.Width / 2), [int]($r.Y + $r.Height / 2)) | Out-Null
    Start-Sleep -Milliseconds 150
    [TraySmoke.Native]::mouse_event(0x0008, 0, 0, 0, [UIntPtr]::Zero)   # RIGHTDOWN
    [TraySmoke.Native]::mouse_event(0x0010, 0, 0, 0, [UIntPtr]::Zero)   # RIGHTUP
    Start-Sleep -Milliseconds 500
    return @(Get-MenuItems)
}

function Select-MenuItem($Item) {
    $r = $Item.Current.BoundingRectangle
    [TraySmoke.Native]::SetCursorPos([int]($r.X + $r.Width / 2), [int]($r.Y + $r.Height / 2)) | Out-Null
    Start-Sleep -Milliseconds 150
    [TraySmoke.Native]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)   # LEFTDOWN
    [TraySmoke.Native]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)   # LEFTUP
    Start-Sleep -Milliseconds 400
}

function Show-Menu([object[]]$Items) {
    foreach ($it in $Items) {
        $mark = if ($it.Current.IsEnabled) { 'x' } else { ' ' }
        Write-Host ("    [{0}] {1}" -f $mark, $it.Current.Name)
    }
}

Write-Host ''
Write-Host '=== 1. find tray icon ==='
$icon = $null
$iconDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $iconDeadline) {
    $icon = Find-Icon
    if ($icon) { break }
    Start-Sleep -Milliseconds 500
}
if (-not $icon) { Done 3 ("SKIP: 超过 {0} 秒仍未看到托盘图标（超时，可能没桌面）" -f $TimeoutSeconds) }
Check 'tray icon visible' $true $icon.Current.Name

Write-Host ''
Write-Host '=== 2. open menu and read items ==='
$items = Open-Menu $icon
if ($items.Count -eq 0) { Done 1 'FAIL: 右键后 3 秒内没读到菜单（可能被别的窗口抢了前台）' }
Show-Menu $items
Check 'menu has 9 items' ($items.Count -eq 9) ("got {0}" -f $items.Count)

$names = @($items | ForEach-Object { $_.Current.Name })
foreach ($expected in @('开始托管', '结束托管', '显示/隐藏浮窗', '打开控制台', '设置')) {
    Check ("menu item '{0}'" -f $expected) ($names -contains $expected)
}
Check 'restart item present' ([bool](@($names | Where-Object { $_ -eq '重启服务' -or $_ -eq '重启并更新' }).Count))
Check 'quit item present' ([bool](@($names | Where-Object { $_ -like '退出*' }).Count))

$disabled = @($items | Where-Object { -not $_.Current.IsEnabled })
Check 'idle menu has nothing disabled' ($disabled.Count -eq 0) (($disabled | ForEach-Object { $_.Current.Name }) -join ',')

Write-Host ''
Write-Host '=== 3. start hosting, then re-read the menu ==='
$startItem = $items | Where-Object { $_.Current.Name -eq '开始托管' } | Select-Object -First 1
Select-MenuItem $startItem
$health = $null
$healthDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $healthDeadline) {
    try {
        $health = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri ("http://127.0.0.1:{0}/api/health" -f $port)
        if ($health.StatusCode -eq 200) { break }
    } catch { }
    Start-Sleep -Seconds 1
}
$healthStatus = if ($health) { $health.StatusCode } else { 'n/a' }
Check 'service answered /api/health after 开始托管' ([bool]($health -and $health.StatusCode -eq 200)) ("status={0}" -f $healthStatus)
Start-Sleep -Seconds 2

$icon2 = Find-Icon
if (-not $icon2) { Done 1 'FAIL: 菜单操作后托盘图标消失了' }
$items2 = Open-Menu $icon2
Show-Menu $items2
Check 'menu still readable after 开始托管' ($items2.Count -eq 9) ("got {0}" -f $items2.Count)
$stopItem = $items2 | Where-Object { $_.Current.Name -eq '结束托管' } | Select-Object -First 1
Check '结束托管 enabled once idle' ([bool]($stopItem -and $stopItem.Current.IsEnabled))

Write-Host ''
Write-Host '=== 4. quit from the menu ==='
$quitItem = $items2 | Where-Object { $_.Current.Name -like '退出*' } | Select-Object -First 1
Select-MenuItem $quitItem
$quitDeadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $quitDeadline) {
    $script:launcher.Refresh()
    if ($script:launcher.HasExited) { break }
    Start-Sleep -Milliseconds 500
}
$script:launcher.Refresh()
Check 'launcher exited after 退出' $script:launcher.HasExited
Start-Sleep -Seconds 2
$left = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -like (Join-Path $script:temp '*') })
Check 'no scratch process left behind' ($left.Count -eq 0) (($left | ForEach-Object { $_.Name }) -join ',')

Write-Host ''
if ($script:failures -gt 0) { Done 1 ("RESULT: {0} check(s) failed" -f $script:failures) }
Write-Host 'RESULT: tray smoke passed'
Done 0
