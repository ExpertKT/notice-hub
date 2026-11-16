@echo off
rem 群务台 · 比赛演示版：起一个只读演示实例，端口 8899，不碰已经装好的 8766。
setlocal
cd /d "%~dp0"
set NOTICEHUB_DATA_DIR=%~dp0demo-data
set NOTICEHUB_LOG_DIR=%~dp0demo-data\logs
set NOTICEHUB_AGENT_SHELL=1
set NOTICEHUB_WEB=1
set NOTICEHUB_WEB_PORT=8899
set NOTICEHUB_WEB_TOKEN=demo
set PYTHONUTF8=1
if not exist "%~dp0demo-data\digest.sqlite3" (
  echo 正在铺演示数据……
  robocopy "%~dp0demo-seed" "%~dp0demo-data" /E /NFL /NDL /NJH /NJS /NP >nul
)
rem 有 llm.key 就走云端接口，没有就退本机 Ollama，两个都没有会回固定说法。
if exist "%~dp0llm.key" for /f "usebackq delims=" %%i in ("%~dp0llm.key") do if not defined LLMKEY set LLMKEY=%%i
if defined LLMKEY (
  if not defined NOTICEHUB_LLM_BACKEND set NOTICEHUB_LLM_BACKEND=openai
  if not defined NOTICEHUB_LLM_ENDPOINT set NOTICEHUB_LLM_ENDPOINT=https://api.deepseek.com/chat/completions
  if not defined NOTICEHUB_LLM_MODEL set NOTICEHUB_LLM_MODEL=deepseek-flash
  if not defined NOTICEHUB_VL_MODEL set NOTICEHUB_VL_MODEL=deepseek-flash
  if not defined NOTICEHUB_LLM_API_KEY set NOTICEHUB_LLM_API_KEY=%LLMKEY%
  echo 模型：云端接口，一句三到五秒。
) else (
  if not defined NOTICEHUB_LLM_BACKEND set NOTICEHUB_LLM_BACKEND=ollama
  if not defined NOTICEHUB_LLM_ENDPOINT set NOTICEHUB_LLM_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
  if not defined NOTICEHUB_LLM_MODEL set NOTICEHUB_LLM_MODEL=qwen3.6:35b-a3b-q4_k_m-gpu20
  if not defined NOTICEHUB_VL_MODEL set NOTICEHUB_VL_MODEL=qwen3.6:35b-a3b-q4_k_m-gpu20
  echo 没找到 llm.key：优先用本机 Ollama；Ollama 没开就回固定说法。
)
start "" "%~dp0noticehub.exe" run
echo 正在等页面起来……
curl -s -o nul --retry 20 --retry-connrefused --retry-delay 1 "http://127.0.0.1:8899/api/health?token=demo"
start "" "http://127.0.0.1:8899/?token=demo&demo=1&nowiz=1"
echo.
echo 浏览器没自己开就打这个地址：http://127.0.0.1:8899/?token=demo^&demo=1^&nowiz=1
echo 想要真的对话：把一把 key 写进同目录的 llm.key（一行），再双击一次本文件。
echo 想回到初始状态：先退出托盘上的群务台，再删掉 demo-data 文件夹。
pause
endlocal
