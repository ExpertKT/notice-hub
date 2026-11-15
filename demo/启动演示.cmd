@echo off
chcp 65001 >nul
setlocal
rem 比赛演示版：独立数据目录、独立端口，不碰装好的那一份（8766）。
cd /d "%~dp0.."

set NOTICEHUB_DATA_DIR=%~dp0data
set NOTICEHUB_LOG_DIR=%~dp0data\logs
set NOTICEHUB_AGENT_SHELL=1
set NOTICEHUB_WEB=1
set NOTICEHUB_WEB_PORT=8899
set NOTICEHUB_WEB_TOKEN=demo
set NOTICEHUB_LLM_BACKEND=ollama
set NOTICEHUB_LLM_MODEL=qwen3.6:35b-a3b-q4_k_m-gpu20
set NOTICEHUB_LLM_ENDPOINT=http://127.0.0.1:11434/v1
set PYTHONUTF8=1

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

if not exist "%~dp0data" mkdir "%~dp0data"
"%PY%" demo\seed.py
if errorlevel 1 goto :failed

start "" "http://127.0.0.1:8899/?token=demo&demo=1&nowiz=1"
echo.
echo 演示版已开：http://127.0.0.1:8899/?token=demo^&demo=1^&nowiz=1
echo 说话要用本机模型：先双击桌面「Ollama 本地模型」，没开也能用（话术会退回固定说法）。
echo 关掉这个窗口就是停止。
echo.
"%PY%" main.py run
goto :eof

:failed
echo 演示数据没写成功，检查上面那行报错。
pause
