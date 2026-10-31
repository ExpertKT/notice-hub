# 计划任务用这个脚本拉服务：切到安装目录，挑解释器，把 stdout/stderr 追加进日志。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir 'service-stdout.log'

# 重定向必须交给 cmd：python 写的是 UTF-8 字节，走 PowerShell 管道会被按系统 GBK
# 解码再编码，隐藏任务里的中文日志就成了乱码；cmd 的 >> 是字节级追加。
cmd.exe /d /c "`"$python`" `"$root\main.py`" run >> `"$logFile`" 2>&1"
