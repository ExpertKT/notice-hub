@echo off
setlocal
set "ROOT=%~dp0"
set "LINK=%ROOT%start-notice-hub.lnk"
set "DESKTOP=%USERPROFILE%\Desktop"
copy /Y "%LINK%" "%DESKTOP%\start-notice-hub.lnk" >nul
if errorlevel 1 (
  echo Shortcut creation failed.
  exit /b 1
)
echo Desktop shortcut created.
endlocal
