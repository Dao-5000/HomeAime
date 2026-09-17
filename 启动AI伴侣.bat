@echo off
rem ============================================
rem  AI Companion Launcher (Windows)
rem  用法：双击打开浏览器（server 已经在跑）
rem  · server 已经在「启动文件夹」开机自启，不用每次手动跑
rem  · 此 bat 仅做：检查 server 是否在跑 + 打开浏览器
rem  · 如果 server 没在跑，再用此 bat 一键启动
rem ============================================
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ==================================================
echo   AI 伴侣 · 启动器
echo ==================================================
echo.

rem 检查 Node.js
where node >nul 2>&1
if %errorlevel% neq 0 (
  echo [错误] 没找到 node.exe，请先安装 Node.js 18+
  echo        下载地址: https://nodejs.org/
  pause
  exit /b 1
)

rem 检查 3000 端口
netstat -ano 2>nul | findstr /c:":3000" | findstr /c:"LISTENING" >nul
if %errorlevel%==0 (
  echo [√] server 已在跑（3000 端口监听中）
  echo     直接打开浏览器...
) else (
  echo [*] server 没在跑，启动中...
  wmic process call create "cmd.exe /c node server.js", "%~dp0" >nul 2>&1
  if %errorlevel% neq 0 (
    echo     (wmic 不可用，回退到 start /B)
    start "AI Server" /B cmd /c "node server.js"
  )
  set /a COUNT=0
:WAIT_LOOP
  timeout /t 1 /nobreak >nul
  set /a COUNT+=1
  netstat -ano 2>nul | findstr /c:":3000" | findstr /c:"LISTENING" >nul
  if %errorlevel%==0 (
    echo [√] server 已起来（耗时 %COUNT% 秒）
    goto OPEN_BROWSER
  )
  if %COUNT% geq 8 (
    echo [错误] 8 秒内 3000 端口未监听
    echo        请手动跑: cd /d %~dp0 ^&^& node server.js
    pause
    exit /b 1
  )
  goto WAIT_LOOP
)

:OPEN_BROWSER
echo.
echo [*] 打开浏览器...
start http://localhost:3000
echo.
echo ==================================================
echo   提示：
echo   · 关掉此窗口不影响 server
echo   · server 已设为开机自启（启动文件夹有 server-autostart.vbs）
echo   · 停止 server：任务管理器结束 node.exe
echo ==================================================
echo.
exit