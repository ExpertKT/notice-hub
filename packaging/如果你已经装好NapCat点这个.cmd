@echo off
rem Already installed NapCat yourself? Double-click this file.
rem The real work is in fix-napcat.ps1 next to this file (Chinese text lives there so this
rem batch file stays pure ASCII and immune to console code pages).
rem Advanced: set NH_NAPCAT_SEARCH=path1;path2 before running to limit where we look.
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix-napcat.ps1"
exit /b %ERRORLEVEL%
