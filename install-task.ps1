[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$NapCatExecutable,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
if (-not $InstallDir) {
    $InstallDir = $PSScriptRoot
}
if (-not $InstallDir) {
    throw 'InstallDir is empty; pass -InstallDir <path> or run this script from a file location.'
}
try {
    $root = (Resolve-Path -LiteralPath $InstallDir -ErrorAction Stop).Path
} catch {
    throw ("InstallDir does not exist or cannot be resolved: {0}" -f $InstallDir)
}
if (-not $NapCatExecutable) {
    $NapCatExecutable = Join-Path $root 'NapCat.Shell.exe'
}
$launcher = Join-Path $root 'QQ-Notice-Hub.exe'
$runScript = Join-Path $root 'run.ps1'
$watchdogScript = Join-Path $root 'tools\watchdog-task.ps1'
# powershell.exe -WindowStyle Hidden 仍会先建一个控制台再隐藏，每 5 分钟闪一下黑框；
# 经 wscript 无窗口启动才真的不闪（VBS 里 shell.Run 的窗口样式 0 = 隐藏）。
$hiddenRunner = Join-Path $root 'tools\run-hidden.vbs'

# C-01: report leftover service binaries instead of silently registering a task that supervises
# the wrong one. The launcher always starts <root>\noticehub.exe, so the root copy is authoritative;
# a leftover legacy directory from the old zip layout may still hold an old exe. We do NOT delete
# anything here (the install dir may be the live production one) -- we print what we found.
$rootService = Join-Path $root 'noticehub.exe'
$legacyDir = Join-Path $root 'qq-live-digest'
if ((Test-Path -LiteralPath $legacyDir -PathType Container) -and (Test-Path -LiteralPath $rootService -PathType Leaf)) {
    Write-Warning ("发现旧布局残留目录：{0}" -f $legacyDir)
    Write-Warning '里面的旧 exe 不会被自动删除（该目录可能就是正在运行的安装目录）。确认无用后可以手动删掉它。'
}

# The shipped zip contains the launcher (which supervises the service itself);
# a source checkout contains run.ps1 (python from .venv). Pick whichever exists.
if (Test-Path -LiteralPath $launcher) {
    $serviceExe = $launcher
    # Auto-start (logon / boot) must not hijack the screen: no browser tab for it.
    $serviceArgs = '--no-browser'
    $serviceKind = 'launcher'
} elseif (Test-Path -LiteralPath $runScript) {
    $serviceExe = 'powershell.exe'
    $serviceArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $runScript
    $serviceKind = 'source'
} else {
    # C-01: fail with readable Chinese and a clean exit code instead of a PowerShell red stack
    # trace, so a user (or the automated check) running this gets an actionable message.
    Write-Host ("没有在 {0} 里找到 QQ-Notice-Hub.exe，也没有 run.ps1，无法注册开机自启。" -f $root)
    Write-Host '请确认解压完整（顶层应该直接有 QQ-Notice-Hub.exe），或用 -InstallDir 指定正确的安装目录。'
    exit 0
}

$napcatArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -FilePath ''{0}'' -WindowStyle Hidden"' -f $NapCatExecutable

# 任务计划先写成纯数据：名字、动作、触发器种类。
# 真正的 ScheduledTask 对象留到注册前才构造——构造它们要走 CIM，普通/低权限进程会被拒（拒绝访问），
# 而「预览」不该依赖注册权限。
$tasks = @()
if (Test-Path -LiteralPath $NapCatExecutable -PathType Leaf) {
    $tasks += @{ Name = 'NapCat-QQ'; Execute = 'powershell.exe'; Arguments = $napcatArgs; WorkDirectory = $root; Triggers = @(@{ Kind = 'Logon' }) }
} else {
    Write-Host ("Skipping NapCat-QQ: executable not found at '{0}'. If needed, pass -NapCatExecutable '<full path to NapCatWinBootMain.exe>'. The QQ-Notice-Hub launcher starts NapCat itself." -f $NapCatExecutable)
}
$tasks += @{ Name = 'NoticeHub'; Execute = $serviceExe; Arguments = $serviceArgs; WorkDirectory = $root; Triggers = @(@{ Kind = 'Logon' }) }
if ((Test-Path -LiteralPath $watchdogScript) -and (Test-Path -LiteralPath $hiddenRunner)) {
    $tasks += @{ Name = 'NapCat-QQ-Watchdog'; Execute = 'wscript.exe'; Arguments = '//B //Nologo "{0}" "{1}"' -f $hiddenRunner, $watchdogScript; WorkDirectory = $root; Triggers = @(@{ Kind = 'Boot' }, @{ Kind = 'Repeat'; Interval = 'PT5M' }) }
} elseif (-not (Test-Path -LiteralPath $watchdogScript)) {
    Write-Warning ("Watchdog script not found, skipping that task: {0}" -f $watchdogScript)
} else {
    Write-Warning ("没有无窗口启动器就跳过看门狗任务，否则每 5 分钟会弹黑窗：{0}" -f $hiddenRunner)
}

if ($WhatIf) {
    Write-Host ("WhatIf: root={0} service_kind={1}" -f $root, $serviceKind)
    foreach ($task in $tasks) {
        Write-Host ("WhatIf: task={0} execute={1} arguments={2} workingdir={3}" -f $task.Name, $task.Execute, $task.Arguments, $task.WorkDirectory)
        # 光看 action 看不出「多久跑一次」：触发器才是看门狗能不能自动拉起的关键，这里一并打印。
        foreach ($trigger in @($task.Triggers)) {
            $triggerName = switch ($trigger.Kind) {
                'Logon' { 'MSFT_TaskLogonTrigger' }
                'Boot' { 'MSFT_TaskBootTrigger' }
                'Repeat' { 'MSFT_TaskTimeTrigger' }
                default { $trigger.Kind }
            }
            $interval = if ($trigger.Interval) { $trigger.Interval } else { '' }
            Write-Host ("WhatIf: task={0} trigger={1} repeat_interval={2} repeat_duration={3}" -f $task.Name, $triggerName, $interval, '')
        }
    }
    return
}

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Days 0)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = 'PT30S'
$startup = New-ScheduledTaskTrigger -AtStartup
# 看门狗不能只在开机时跑一次：服务中途退出后没人管。再挂一个「从现在起每 5 分钟」的重复触发器。
# 开机那一次由 AtStartup 负责，其余时间由它负责。
# 只写 -RepetitionInterval、不写 -RepetitionDuration：不写 Duration 就是无限期重复。
# 别写 -RepetitionDuration 的最大值：任务 XML 不接受那个 Duration，Register-ScheduledTask 会抛
# CimException，任务根本建不出来（而且异常在提权窗口里被吞掉，日志一行都没有）。
$watchdogRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)

# 纯数据换成真的 ScheduledTask 对象（这一步才需要 CIM 权限）。
$triggerObjects = @{ Logon = $logon; Boot = $startup; Repeat = $watchdogRepeat }
foreach ($task in $tasks) {
    # New-ScheduledTaskAction rejects an empty -Argument; omit it when there is none.
    if ($task.Arguments) {
        $task.Action = New-ScheduledTaskAction -Execute $task.Execute -Argument $task.Arguments -WorkingDirectory $task.WorkDirectory
    } else {
        $task.Action = New-ScheduledTaskAction -Execute $task.Execute -WorkingDirectory $task.WorkDirectory
    }
    $task.Trigger = @($task.Triggers | ForEach-Object { $triggerObjects[$_.Kind] })
}

$registered = @()
foreach ($task in $tasks) {
    try {
        Register-ScheduledTask -TaskName $task.Name -Action $task.Action -Trigger $task.Trigger -Settings $settings -Force -ErrorAction Stop | Out-Null
        $registered += $task.Name
        Write-Host ("Registered scheduled task: {0}" -f $task.Name)
    } catch {
        $message = $_.Exception.Message
        if ($task.Name -eq 'NapCat-QQ-Watchdog' -and ($message -match 'Access is denied|access denied|拒绝访问|权限')) {
            Write-Host 'NapCat-QQ-Watchdog skipped: 看门狗需要以管理员身份运行 install-task.ps1。'
            continue
        }
        if ($task.Name -eq 'NoticeHub' -and ($message -match 'Access is denied|access denied|拒绝访问|权限')) {
            Write-Host 'NoticeHub 注册需要以管理员身份运行 install-task.ps1。'
            continue
        }
        throw
    }
}
if ($registered.Count -gt 0) {
    Get-ScheduledTask -TaskName $registered -ErrorAction SilentlyContinue | Select-Object TaskName, State
}

# 注册返回成功不算装好：必须再查一次任务在不在、看门狗的重复触发器有没有真挂上。
if ($registered -contains 'NapCat-QQ-Watchdog') {
    $watchdogTask = Get-ScheduledTask -TaskName 'NapCat-QQ-Watchdog' -ErrorAction SilentlyContinue
    $repeatTriggers = @()
    if ($watchdogTask) {
        $repeatTriggers = @($watchdogTask.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Interval })
    }
    if (-not $watchdogTask) {
        Write-Host '看门狗没装上：查不到 NapCat-QQ-Watchdog 任务。以管理员身份重跑 install-task.ps1；还是不行就看看杀毒软件是不是拦了计划任务。'
    } elseif ($repeatTriggers.Count -eq 0) {
        Write-Host '看门狗装上了但没有重复触发：服务退出后不会被自动拉起。以管理员身份重跑本脚本；仍不对就删掉 NapCat-QQ-Watchdog 再重新注册。'
    } else {
        Write-Host ("Watchdog verified: NapCat-QQ-Watchdog repeats every {0}" -f $repeatTriggers[0].Repetition.Interval)
    }
}

# 品牌切换：旧任务名跑的是同一个服务，留着会让开机起两条（两个实例抢同一个端口）。
if ($registered -contains 'NoticeHub') {
    if (Get-ScheduledTask -TaskName 'QQ-Live-Digest' -ErrorAction SilentlyContinue) {
        try {
            schtasks /Delete /TN "QQ-Live-Digest" /F | Out-Null
            Write-Host 'Removed legacy scheduled task: QQ-Live-Digest'
        } catch {
            Write-Warning ("旧任务 QQ-Live-Digest 删除失败，请手动删除，否则可能与 NoticeHub 同时启动：{0}" -f $_.Exception.Message)
        }
    }
}

if ($registered.Count -gt 0) {
    # 手机走局域网访问待办台时，需要 Windows 防火墙放行该入站端口。
    $webPort = 8766
    $envFile = Join-Path $root '.env'
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8 -ErrorAction SilentlyContinue) {
            if ($line -match '^\s*QQ_DIGEST_WEB_PORT\s*=\s*"?(\d+)"?\s*$') {
                $parsedPort = [int]$Matches[1]
                if ($parsedPort -ge 1 -and $parsedPort -le 65535) { $webPort = $parsedPort }
            }
        }
    }
    $firewallRuleName = "群务台 $webPort"
    $firewallRuleExists = $false
    try {
        $firewallRuleExists = @(Get-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue).Count -gt 0
    } catch {
        $firewallRuleExists = $false
    }
    if (-not $firewallRuleExists) {
        netsh advfirewall firewall add rule name="$firewallRuleName" dir=in action=allow protocol=TCP localport=$webPort 2>&1 | Out-Null
        try {
            $firewallRuleExists = @(Get-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue).Count -gt 0
        } catch {
            $firewallRuleExists = $false
        }
    }
    if ($firewallRuleExists) {
        Write-Host ("Firewall allows TCP {0} (rule: {1})" -f $webPort, $firewallRuleName)
    } else {
        Write-Host ("防火墙未放行 TCP {0}：以管理员身份重跑本脚本可自动放行。" -f $webPort)
    }
}
