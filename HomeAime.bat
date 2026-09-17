@echo off
chcp 65001 >nul
title HomeAime
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\AI聊天项目桌面端\AI聊天项目\tools\HomeHimeLauncher.ps1"
if errorlevel 1 pause
