# 启动 qq-live-digest（供任务计划程序调用）
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = 'python'
}

$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir 'service-stdout.log'

# python 侧日志按 UTF-8 写 stdout（见 qq_live_digest/logging_setup.py）。
# 这里用 cmd 直接重定向，让字节原样落盘；若走 PowerShell 管道，隐藏任务里会按
# 系统 GBK 解码 UTF-8，中文日志会变成乱码。

cmd.exe /d /c "`"$python`" `"$root\main.py`" run >> `"$logFile`" 2>&1"
