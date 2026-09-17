@echo off
chcp 65001 >nul
setlocal
rem ============================================================
rem  AI Companion - Local Voice One-Click Launcher
rem  1) CosyVoice2 local TTS service  (port 9881)
rem  2) Main FastAPI backend run.py    (port 3000)
rem  Then: npm start for desktop app, or browser http://localhost:3000
rem ============================================================

set "ROOT=%~dp0"
set "COSY_DIR=%COSYVOICE_HOME%"
set "PY311=python"

echo [1/3] Check CosyVoice TTS service (9881)...
netstat -ano | findstr ":9881" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo       CosyVoice already running.
) else (
  echo       Starting CosyVoice2 service (model first load ~1 min, watch its window)...
  start "CosyVoice-TTS" /min cmd /c "cd /d %COSY_DIR% && %PY311% -m uvicorn server:app --host 0.0.0.0 --port 9881"
)

echo [2/3] Wait for CosyVoice 9881 ready...
set /a tries=0
:wait_cosy
netstat -ano | findstr ":9881" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto cosy_ok
set /a tries+=1
if %tries% geq 40 goto cosy_timeout
timeout /t 3 /nobreak >nul
goto wait_cosy
:cosy_ok
echo       CosyVoice ready.
goto backend_step
:cosy_timeout
echo       WARN: CosyVoice not listening yet, continue anyway.
:backend_step

echo [3/3] Check main backend (3000)...
netstat -ano | findstr ":3000" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo       Backend already running.
) else (
  echo       Starting backend run.py...
  start "AI-Backend" /min cmd /c "cd /d %ROOT% && backend\venv\Scripts\python.exe run.py"
)

echo.
echo ============================================================
echo  Done! Next steps:
echo    Desktop app : cd /d %ROOT%  &&  npm start
echo    Web browser : http://localhost:3000
echo    Voice check : Settings page - TTS provider = CosyVoice2
echo ============================================================
echo.
pause
