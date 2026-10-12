@echo off
chcp 65001 >nul
rem 创建桌面快捷方式。
rem
rem 旧版本靠拷贝一个「预先存在的」start-notice-hub.lnk 来建快捷方式，但那个文件从来不
rem 随包发布、还被 .gitignore 忽略，所以这个脚本必然 exit 1（C-07）。
rem 现在改成用 PowerShell 直接生成 .lnk，目标写绝对路径，所以解压到哪都成立；
rem 名字与启动器首启自动创建的保持一致（notice-hub.lnk），避免桌面上出现两个图标。
setlocal
set "ROOT=%~dp0"
set "NH_TARGET=%ROOT%QQ-Notice-Hub.exe"
set "NH_ROOT=%ROOT:~0,-1%"
if not exist "%NH_TARGET%" (
  echo 找不到 "%NH_TARGET%"
  echo 请确认解压完整：QQ-Notice-Hub.exe 应该在解压出来的这个文件夹里。
  exit /b 1
)
powershell.exe -NoProfile -NonInteractive -Command "$ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'notice-hub.lnk')); $lnk.TargetPath = $env:NH_TARGET; $lnk.WorkingDirectory = $env:NH_ROOT; $lnk.IconLocation = $env:NH_TARGET + ',0'; $lnk.Description = 'notice-hub'; $lnk.Save()"
if errorlevel 1 (
  echo 创建桌面快捷方式失败。
  exit /b 1
)
echo 桌面快捷方式已创建：notice-hub.lnk
endlocal
