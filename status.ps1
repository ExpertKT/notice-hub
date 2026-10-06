# qq-live-digest 状态自检（双击 检查状态.cmd 运行）
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ''
Write-Host '==== QQ 群通知摘要 · 状态检查 ====' -ForegroundColor Cyan
Write-Host ''

Write-Host '[1] 计划任务'
foreach ($name in 'NapCat-QQ', 'QQ-Live-Digest', 'NapCat-QQ-Watchdog') {
    $state = (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue).State
    if (-not $state) { $state = '未注册' }
    Write-Host ("    {0,-22} {1}" -f $name, $state)
}

Write-Host ''
Write-Host '[2] 接口端口'
$p3000 = [bool](Get-NetTCPConnection -LocalPort 3000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
$p8765 = [bool](Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
if ($p3000) { Write-Host '    NapCat 接口 (3000)   在线' -ForegroundColor Green } else { Write-Host '    NapCat 接口 (3000)   离线' -ForegroundColor Red }
if ($p8765) { Write-Host '    摘要服务   (8765)   在线' -ForegroundColor Green } else { Write-Host '    摘要服务   (8765)   离线' -ForegroundColor Red }

Write-Host ''
Write-Host '[3] QQ 登录状态'
$apiToken = ''
$napcatHome = $env:NAPCAT_HOME
if (-not $napcatHome) {
    $napcatTask = Get-ScheduledTask -TaskName 'NapCat-QQ' -ErrorAction SilentlyContinue
    $launchPath = [string](@($napcatTask.Actions)[0].Arguments)
    if ($launchPath) {
        $launchPath = $launchPath.Trim('"')
        if (Test-Path $launchPath) { $napcatHome = Split-Path -Parent $launchPath }
    }
}
$napcatConfig = $env:NAPCAT_ONEBOT_CONFIG
if (-not $napcatConfig -and $napcatHome) {
    $configDir = Join-Path $napcatHome 'shell\config'
    $candidate = Get-ChildItem -Path $configDir -Filter 'onebot11_*.json' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { $napcatConfig = $candidate.FullName }
}
try {
    if ($napcatConfig -and (Test-Path $napcatConfig)) {
        $cfg = Get-Content $napcatConfig -Raw | ConvertFrom-Json
        $apiToken = [string](@($cfg.network.httpServers)[0].token)
    }
} catch { }
$nick = ''
$uid = ''
$online = $false
$good = $false
try {
    $login = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:3000/get_login_info' -Headers @{ Authorization = "Bearer $apiToken" } -TimeoutSec 8
    $nick = [string]$login.data.nickname
    $uid = [string]$login.data.user_id
    $status = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:3000/get_status' -Headers @{ Authorization = "Bearer $apiToken" } -TimeoutSec 8
    $online = [bool]$status.data.online
    $good = [bool]$status.data.good
} catch { }
if ($uid) {
    $healthText = if ($online -and $good) { '正常' } else { '异常' }
    Write-Host ("    已登录：{0}（{1}）  NapCat 状态：{2}" -f $nick, $uid, $healthText) -ForegroundColor Green
} else {
    Write-Host '    未登录（可能需要重新扫码）' -ForegroundColor Red
}

Write-Host ''
Write-Host '[4] 运行统计'
$health = $null
try {
    $health = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 8
} catch { }
$stats = $null
try {
    $stats = & "$root\.venv\Scripts\python.exe" "$root\main.py" stats | ConvertFrom-Json
    Write-Host ("    累计收到群消息 {0} 条 / 生成摘要 {1} 条 / 成功推送 {2} 次" -f $stats.counts.messages, $stats.counts.digests, $stats.counts.deliveries_sent)
} catch {
    Write-Host '    读取统计失败' -ForegroundColor Yellow
}
if ($stats) {
    Write-Host ("    推送渠道：" + ($stats.channels -join ', '))
}
if ($health) {
    $catchupText = if ($health.catchup_enabled) { '已开启' } else { '已关闭' }
    $lastCatchup = '尚未成功'
    if ($health.last_catchup_at) {
        try { $lastCatchup = ([datetime]$health.last_catchup_at).ToString('MM-dd HH:mm') } catch { $lastCatchup = [string]$health.last_catchup_at }
    }
    $lastAttempt = '尚未执行'
    if ($health.last_catchup_attempt_at) {
        try { $lastAttempt = ([datetime]$health.last_catchup_attempt_at).ToString('MM-dd HH:mm') } catch { $lastAttempt = [string]$health.last_catchup_attempt_at }
    }
    Write-Host ("    历史补采：{0} / 最近成功：{1} / 最近尝试：{2}" -f $catchupText, $lastCatchup, $lastAttempt)
} else {
    Write-Host '    历史补采：读取失败（服务未响应 /health）' -ForegroundColor Yellow
}

Write-Host ''
Write-Host '[5] 最近日志'
$log = "$root\logs\qq-live-digest.log"
if (Test-Path $log) {
    Get-Content $log -Tail 5 -Encoding UTF8 | ForEach-Object { Write-Host ('    ' + $_.Substring(0, [Math]::Min(110, $_.Length))) }
} else {
    Write-Host '    暂无日志'
}
Write-Host ''
Write-Host '提示：以上都正常就不用管它；有“离线/未登录”再找 Codex 处理。' -ForegroundColor DarkGray
Write-Host ''
Read-Host '按回车键关闭'
