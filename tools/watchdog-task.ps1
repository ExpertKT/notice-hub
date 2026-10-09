[CmdletBinding()]
param(
    [string]$ServiceTaskName = 'QQ-Live-Digest'
)
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $ServiceTaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host ("Service task not found: {0}" -f $ServiceTaskName)
    exit 1
}
if ($task.State -ne 'Running') {
    Start-ScheduledTask -TaskName $ServiceTaskName
    Write-Host ("Started service task: {0}" -f $ServiceTaskName)
} else {
    Write-Host ("Service task is running: {0}" -f $ServiceTaskName)
}
