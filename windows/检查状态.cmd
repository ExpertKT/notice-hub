@echo off
chcp 65001 >nul
title QQ 群通知摘要 - 状态检查
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0status.ps1"
