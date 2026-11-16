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
set PYTHONUTF8=1

rem 模型：demo\llm.key 里放一把 OpenAI 兼容接口的钥匙就走云端（一句 3~5 秒）；
rem 没有这个文件就退回本机 Ollama（先双击桌面「Ollama 本地模型」），再没有就回固定说法。
set LLMKEY=
if exist "%~dp0llm.key" for /f "usebackq delims=" %%i in ("%~dp0llm.key") do if not defined LLMKEY set LLMKEY=%%i
if defined LLMKEY (
  set NOTICEHUB_LLM_BACKEND=openai
  set NOTICEHUB_LLM_API_KEY=%LLMKEY%
  if not defined NOTICEHUB_LLM_ENDPOINT set NOTICEHUB_LLM_ENDPOINT=https://api.deepseek.com/chat/completions
  if not defined NOTICEHUB_LLM_MODEL set NOTICEHUB_LLM_MODEL=deepseek-flash
  if not defined NOTICEHUB_VL_MODEL set NOTICEHUB_VL_MODEL=deepseek-flash
) else (
  set NOTICEHUB_LLM_BACKEND=ollama
  set NOTICEHUB_LLM_MODEL=qwen3.6:35b-a3b-q4_k_m-gpu20
  set NOTICEHUB_LLM_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
)

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

if not exist "%~dp0data" mkdir "%~dp0data"
"%PY%" demo\seed.py
if errorlevel 1 goto :failed

start "" "http://127.0.0.1:8899/?token=demo&demo=1&nowiz=1"
echo.
echo 演示版已开：http://127.0.0.1:8899/?token=demo^&demo=1^&nowiz=1
if defined LLMKEY (
  echo 模型：云端接口，一句三到五秒。
) else (
  echo 模型：没找到 demo\llm.key，用本机 Ollama；没开就回固定说法，行程和地图不受影响。
)
echo 关掉这个窗口就是停止。
echo.
"%PY%" main.py run
goto :eof

:failed
echo 演示数据没写成功，检查上面那行报错。
pause
