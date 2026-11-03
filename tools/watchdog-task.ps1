[CmdletBinding()]
param(
    [string]$InstallRoot = '',
    [int]$HealthTimeoutSeconds = 5,
    [int]$WaitSeconds = 20
)
# 服务整个没了就拉起来的看门狗。计划任务每 5 分钟调一次这个脚本。
# 只认本安装目录的启动器：绝不按进程名杀东西，绝不碰用户的 QQ。
$ErrorActionPreference = 'Stop'

if (-not $InstallRoot) { $InstallRoot = Split-Path $PSScriptRoot -Parent }
$InstallRoot = (Resolve-Path -LiteralPath $InstallRoot).Path

$logRoot = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'NoticeHub' } else { Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'NoticeHub' }
$logFile = Join-Path $logRoot 'watchdog.log'
$manualFlag = if ($env:NOTICEHUB_MANUAL_EXIT_FLAG) { $env:NOTICEHUB_MANUAL_EXIT_FLAG } else { Join-Path $logRoot 'manual-exit.flag' }

function Write-WatchdogLog([string]$Message) {
    try {
        if (-not (Test-Path -LiteralPath $logRoot)) { New-Item -ItemType Directory -Path $logRoot -Force | Out-Null }
        $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
        Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    } catch {
        # 日志写不进去也不能挡住看门狗干活
    }
}

# 端口：读 <根>\.env 的 QQ_DIGEST_WEB_PORT（新名 NOTICEHUB_WEB_PORT 优先），读不到用 8766
$port = 8766
$envFile = Join-Path $InstallRoot '.env'
if (Test-Path -LiteralPath $envFile) {
    foreach ($key in @('NOTICEHUB_WEB_PORT', 'QQ_DIGEST_WEB_PORT')) {
        $match = Select-String -LiteralPath $envFile -Pattern ('^\s*' + $key + '\s*=\s*"?(\d+)"?\s*$') -Encoding UTF8 | Select-Object -First 1
        if ($match) {
            $candidate = [int]$match.Matches[0].Groups[1].Value
            if ($candidate -ge 1 -and $candidate -le 65535) { $port = $candidate; break }
        }
    }
}

function Test-ServiceAlive([int]$Port, [int]$TimeoutSeconds) {
    try {
        $response = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $Port + '/api/health') -TimeoutSec $TimeoutSeconds -UseBasicParsing
        return ([int]$response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

if (Test-ServiceAlive -Port $port -TimeoutSeconds $HealthTimeoutSeconds) {
    exit 0
}

if (Test-Path -LiteralPath $manualFlag) {
    Write-WatchdogLog ('用户主动退出，跳过（端口 ' + $port + '）')
    exit 0
}

$exe = Join-Path $InstallRoot 'QQ-Notice-Hub.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    Write-WatchdogLog ('没找到启动器，无法拉起：' + $exe)
    exit 2
}

Write-WatchdogLog ('服务没有应答（端口 ' + $port + '），正在拉起')
try {
    Start-Process -FilePath $exe -ArgumentList '--no-browser','--from-watchdog' -WorkingDirectory $InstallRoot -WindowStyle Hidden | Out-Null
} catch {
    Write-WatchdogLog ('拉起启动器失败：' + $_.Exception.Message)
    exit 2
}

$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (Test-ServiceAlive -Port $port -TimeoutSeconds $HealthTimeoutSeconds) {
        Write-WatchdogLog ('服务已恢复（端口 ' + $port + '）')
        exit 0
    }
}

Write-WatchdogLog ('拉起后 ' + $WaitSeconds + ' 秒内端口 ' + $port + ' 仍无应答')
exit 2