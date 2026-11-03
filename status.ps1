# 群通知摘要 · 本机状态自检（双击「检查状态.cmd」运行）
# 全程只读：只查询任务、端口、接口与日志，不改任何状态。
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 统一的分节打印：先空一行再打标题，和下面的内容贴在一起。
function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host $Title
}

# 接口给的时间戳按 MM-dd HH:mm 显示；解析不了就原样贴。
function Format-Moment {
    param($Value, [string]$Fallback)
    if (-not $Value) { return $Fallback }
    try { return ([datetime]$Value).ToString('MM-dd HH:mm') } catch { return [string]$Value }
}

# NapCat 的 onebot 配置：先看环境变量，再从任务参数推目录，最后按文件名找。
function Find-OneBotConfig {
    if ($env:NAPCAT_ONEBOT_CONFIG) { return $env:NAPCAT_ONEBOT_CONFIG }
    $home = $env:NAPCAT_HOME
    if (-not $home) {
        $task = Get-ScheduledTask -TaskName 'NapCat-QQ' -ErrorAction SilentlyContinue
        $launch = [string](@($task.Actions)[0].Arguments)
        if ($launch) {
            $launch = $launch.Trim('"')
            if (Test-Path $launch) { $home = Split-Path -Parent $launch }
        }
    }
    if (-not $home) { return '' }
    $configDir = Join-Path $home 'shell\config'
    $hit = Get-ChildItem -Path $configDir -Filter 'onebot11_*.json' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { return $hit.FullName }
    return ''
}

# 配置里的 HTTP 服务令牌；读不到就返回空串（接口仍然会被试一次）。
function Read-ApiToken {
    $config = Find-OneBotConfig
    if (-not $config -or -not (Test-Path $config)) { return '' }
    try {
        $cfg = Get-Content $config -Raw | ConvertFrom-Json
        return [string](@($cfg.network.httpServers)[0].token)
    } catch {
        return ''
    }
}

Write-Host ''
Write-Host '==== QQ 群通知摘要 · 状态检查 ====' -ForegroundColor Cyan

Write-Section '[1] 计划任务'
$taskNames = 'NapCat-QQ', 'NoticeHub', 'QQ-Live-Digest', 'NapCat-QQ-Watchdog'
foreach ($name in $taskNames) {
    $state = (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue).State
    if (-not $state) { $state = '未注册' }
    Write-Host ('    {0,-22} {1}' -f $name, $state)

}

# 看门狗光有「已注册」不够：要能看出它是按周期跑的，否则服务退出后没人拉起。
$watchdog = Get-ScheduledTask -TaskName 'NapCat-QQ-Watchdog' -ErrorAction SilentlyContinue
if ($watchdog) {
    $repeat = @($watchdog.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Interval } | Select-Object -First 1)
    if ($repeat) {
        Write-Host ('    {0,-22} {1}' -f '  看门狗周期', ('每 ' + $repeat[0].Repetition.Interval))
    } else {
        Write-Host ('    {0,-22} {1}' -f '  看门狗周期', '没有重复触发：服务退出后不会自动拉起，需重跑 install-task.ps1') -ForegroundColor Yellow
    }
}


Write-Section '[2] 接口端口'
$portRows = @(
    @{ Port = 3001; Label = '    NapCat 接口 (3001)' },
    @{ Port = 8765; Label = '    摘要服务   (8765)' }
)
foreach ($row in $portRows) {
    $listening = [bool](Get-NetTCPConnection -LocalPort $row.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
    $word = if ($listening) { '在线' } else { '离线' }
    $color = if ($listening) { 'Green' } else { 'Red' }
    Write-Host ('{0}   {1}' -f $row.Label, $word) -ForegroundColor $color
}

Write-Section '[3] QQ 登录状态'
$apiToken = Read-ApiToken
$nickname = ''
$account = ''
$online = $false
$healthy = $false
try {
    $login = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:3001/get_login_info' -Headers @{ Authorization = "Bearer $apiToken" } -TimeoutSec 8
    $nickname = [string]$login.data.nickname
    $account = [string]$login.data.user_id
    $status = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:3001/get_status' -Headers @{ Authorization = "Bearer $apiToken" } -TimeoutSec 8
    $online = [bool]$status.data.online
    $healthy = [bool]$status.data.good
} catch { }
if ($account) {
    $healthText = if ($online -and $healthy) { '正常' } else { '异常' }
    Write-Host ('    已登录：{0}（{1}）  NapCat 状态：{2}' -f $nickname, $account, $healthText) -ForegroundColor Green
} else {
    Write-Host '    未登录（可能需要重新扫码）' -ForegroundColor Red
}

Write-Section '[4] 运行统计'
$health = $null
try {
    $health = Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 8
} catch { }

# 统计来自本地进程：冻结包用同目录的 noticehub.exe，源码检出才回退到 .venv。
$stats = $null
$frozenExe = Join-Path $root 'noticehub.exe'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
try {
    if (Test-Path $frozenExe) {
        # 冻结包会把「旧环境变量名」之类的 warning 打到 stderr；在 5.1 里 2>$null 不一定吃掉，
        # 它会变成 ErrorRecord 让整个检查脚本以退出码 1 收场。这里合并后按前缀滤掉，只留 JSON。
        $raw = & $frozenExe --env (Join-Path $root '.env') stats 2>&1 |
            Where-Object { "$_" -notmatch 'WARNING|检测到旧环境变量|^\s*$' }
        $stats = ($raw -join "`n") | ConvertFrom-Json
    } elseif (Test-Path $venvPython) {
        $stats = & $venvPython (Join-Path $root 'main.py') stats | ConvertFrom-Json
    } else {
        Write-Host '    这是冻结包（没有 .venv），已改用 noticehub.exe 读取统计。' -ForegroundColor Yellow
    }
    if ($stats) {
        Write-Host ('    累计收到群消息 {0} 条 / 生成摘要 {1} 条 / 成功推送 {2} 次' -f $stats.counts.messages, $stats.counts.digests, $stats.counts.deliveries_sent)
    }
} catch {
    Write-Host '    读取统计失败（服务可能没在运行）' -ForegroundColor Yellow
}
if ($stats) {
    Write-Host ('    推送渠道：' + ($stats.channels -join ', '))
}
if ($health) {
    $catchupText = if ($health.catchup_enabled) { '已开启' } else { '已关闭' }
    $lastOk = Format-Moment $health.last_catchup_at '尚未成功'
    $lastTry = Format-Moment $health.last_catchup_attempt_at '尚未执行'
    Write-Host ('    历史补采：{0} / 最近成功：{1} / 最近尝试：{2}' -f $catchupText, $lastOk, $lastTry)
} else {
    Write-Host '    历史补采：读取失败（服务未响应 /health）' -ForegroundColor Yellow
}

Write-Section '[5] 最近日志'
$log = Join-Path $root 'logs\noticehub.log'
if (Test-Path $log) {
    Get-Content $log -Tail 5 -Encoding UTF8 | ForEach-Object { Write-Host ('    ' + $_.Substring(0, [Math]::Min(110, $_.Length))) }
} else {
    Write-Host '    暂无日志'
}

Write-Host ''
Write-Host '提示：以上都正常就不用管它；有“离线/未登录”再找 Codex 处理。' -ForegroundColor DarkGray
Write-Host ''
Read-Host '按回车键关闭'
