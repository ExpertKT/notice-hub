[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'

# 四个任务按固定顺序逐个注销；查不到就只打一行跳过，不算失败。
$taskNames = 'NapCat-QQ', 'NoticeHub', 'QQ-Live-Digest', 'NapCat-QQ-Watchdog'
foreach ($taskName in $taskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host ('Scheduled task not found: {0}' -f $taskName)
        continue
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host ('Unregistered scheduled task: {0}' -f $taskName)
}
