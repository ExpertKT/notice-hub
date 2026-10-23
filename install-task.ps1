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

# C-01: report stale duplicate service binaries instead of silently registering a task that
# supervises the wrong one. The launcher always starts <root>\qq-live-digest.exe, so the root
# copy is authoritative; the nested qq-live-digest\qq-live-digest.exe is a leftover from the old
# zip layout. We do NOT delete anything here (the install dir may be the live production one) --
# we print what we found so the user can delete it themselves.
$rootService = Join-Path $root 'qq-live-digest.exe'
$nestedService = Join-Path $root 'qq-live-digest\qq-live-digest.exe'
if ((Test-Path -LiteralPath $rootService -PathType Leaf) -and (Test-Path -LiteralPath $nestedService -PathType Leaf)) {
    Write-Warning '发现两份 qq-live-digest.exe，将使用根目录的那份：'
    Write-Warning ("  [使用] {0}" -f $rootService)
    Write-Warning ("  [陈旧] {0}" -f $nestedService)
    Write-Warning '陈旧副本不会被自动删除（该目录可能就是正在运行的安装目录）。确认无用后可以手动删掉整个 qq-live-digest\ 文件夹。'
}

# The shipped zip contains the launcher (which supervises the service itself);
# a source checkout contains run.ps1 (python from .venv). Pick whichever exists.
if (Test-Path -LiteralPath $launcher) {
    $serviceExe = $launcher
    $serviceArgs = ''
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

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Days 0)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = 'PT30S'
$startup = New-ScheduledTaskTrigger -AtStartup
$napcatArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -FilePath ''{0}'' -WindowStyle Hidden"' -f $NapCatExecutable
$watchdogArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $watchdogScript

# New-ScheduledTaskAction rejects an empty -Argument, so omit it entirely for the launcher.
if ($serviceArgs) {
    $serviceAction = New-ScheduledTaskAction -Execute $serviceExe -Argument $serviceArgs -WorkingDirectory $root
} else {
    $serviceAction = New-ScheduledTaskAction -Execute $serviceExe -WorkingDirectory $root
}

$tasks = @()
if (Test-Path -LiteralPath $NapCatExecutable -PathType Leaf) {
    $tasks += @{ Name = 'NapCat-QQ'; Action = (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $napcatArgs -WorkingDirectory $root); Trigger = $logon }
} else {
    Write-Host ("Skipping NapCat-QQ: executable not found at '{0}'. If needed, pass -NapCatExecutable '<full path to NapCatWinBootMain.exe>'. The QQ-Notice-Hub launcher starts NapCat itself." -f $NapCatExecutable)
}
$tasks += @{ Name = 'QQ-Live-Digest'; Action = $serviceAction; Trigger = $logon }
if (Test-Path -LiteralPath $watchdogScript) {
    $tasks += @{ Name = 'NapCat-QQ-Watchdog'; Action = (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $watchdogArgs -WorkingDirectory $root); Trigger = $startup }
} else {
    Write-Warning ("Watchdog script not found, skipping that task: {0}" -f $watchdogScript)
}

if ($WhatIf) {
    Write-Host ("WhatIf: root={0} service_kind={1}" -f $root, $serviceKind)
    foreach ($task in $tasks) {
        Write-Host ("WhatIf: task={0} execute={1} arguments={2} workingdir={3}" -f $task.Name, $task.Action.Execute, $task.Action.Arguments, $task.Action.WorkingDirectory)
    }
    return
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
        if ($task.Name -eq 'QQ-Live-Digest' -and ($message -match 'Access is denied|access denied|拒绝访问|权限')) {
            Write-Host 'QQ-Live-Digest 注册需要以管理员身份运行 install-task.ps1。'
            continue
        }
        throw
    }
}
if ($registered.Count -gt 0) {
    Get-ScheduledTask -TaskName $registered -ErrorAction SilentlyContinue | Select-Object TaskName, State
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
