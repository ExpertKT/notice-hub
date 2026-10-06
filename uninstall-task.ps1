# 取消自启任务
$ErrorActionPreference = 'Stop'
$taskName = 'QQ-Live-Digest'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "已删除计划任务：$taskName"
} else {
    Write-Host "计划任务不存在：$taskName"
}
