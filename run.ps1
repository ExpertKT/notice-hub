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

& $python (Join-Path $root 'main.py') run 2>&1 | Out-File -FilePath (Join-Path $root 'logs\service-stdout.log') -Append -Encoding utf8
