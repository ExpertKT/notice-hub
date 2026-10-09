[CmdletBinding()]
param(
    [string]$InstallDir = $PSScriptRoot,
    [string]$NapCatExecutable = (Join-Path $InstallDir 'NapCat.Shell.exe'),
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $InstallDir).Path
$launcher = Join-Path $root 'QQ-Notice-Hub.exe'
$runScript = Join-Path $root 'run.ps1'
$watchdogScript = Join-Path $root 'tools\watchdog-task.ps1'

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
    throw ("Neither QQ-Notice-Hub.exe nor run.ps1 exists in {0}; cannot register the service." -f $root)
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

$tasks = @(
    @{ Name = 'NapCat-QQ'; Action = (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $napcatArgs -WorkingDirectory $root); Trigger = $logon },
    @{ Name = 'QQ-Live-Digest'; Action = $serviceAction; Trigger = $logon }
)
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

foreach ($task in $tasks) {
    Register-ScheduledTask -TaskName $task.Name -Action $task.Action -Trigger $task.Trigger -Settings $settings -Force | Out-Null
    Write-Host ("Registered scheduled task: {0}" -f $task.Name)
}
Get-ScheduledTask -TaskName ($tasks | ForEach-Object { $_.Name }) | Select-Object TaskName, State
