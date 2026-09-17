@echo off
chcp 65001 >nul
title 家姬百恋HomeHime
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0\tools\HomeHimeLauncher.ps1"
if errorlevel 1 pause
