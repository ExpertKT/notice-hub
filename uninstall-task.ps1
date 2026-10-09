[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
foreach ($taskName in 'NapCat-QQ', 'QQ-Live-Digest', 'NapCat-QQ-Watchdog') {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host ("Unregistered scheduled task: {0}" -f $taskName)
    } else {
        Write-Host ("Scheduled task not found: {0}" -f $taskName)
    }
}
