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

# python 侧日志按 UTF-8 写 stdout（见 qq_live_digest/logging_setup.py），
# 这里同步 PowerShell 的解码方式，否则重定向到日志文件时中文会变成乱码。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

& $python (Join-Path $root 'main.py') run 2>&1 | Out-File -FilePath (Join-Path $root 'logs\service-stdout.log') -Append -Encoding utf8
