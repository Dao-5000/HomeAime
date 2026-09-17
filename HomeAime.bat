@echo off
chcp 65001 >nul
title HomeAime
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0\tools\HomeHimeLauncher.ps1"
if errorlevel 1 pause
