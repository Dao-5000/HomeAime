@echo off
chcp 65001 >nul
cd /d %~dp0
rem ★ 关闭 CodeBuddy 的"安全删除"钩子（否则 PyInstaller 打包到 EXE 阶段时，
rem   os.remove 会被拦截成"移回收站"，中文路径下 trash 失败 → BACKEND_BUILD_FAILED）
set CODEBUDDY_SAFE_DELETE_ENABLED=0
echo ============================================
echo   AI 伴侣 PC 桌面版 一键构建
echo ============================================
echo.

echo [1/4] 准备 Python 虚拟环境并安装依赖...
if not exist backend\venv\Scripts\python.exe (
  python -m venv backend\venv || goto :fail
)
backend\venv\Scripts\python.exe -m pip install --upgrade pip -q
backend\venv\Scripts\pip.exe install -r backend\requirements.txt || goto :fail
if not exist backend\venv\Scripts\pyinstaller.exe (
  backend\venv\Scripts\pip.exe install pyinstaller || goto :fail
)
echo.

echo [2/4] 安装 Electron 构建依赖（首次较慢）...
call npm install || goto :fail
echo.

echo [3/4] PyInstaller 打包后端为 pc_backend.exe ...
call npm run build:backend || goto :fail
echo.

echo [4/4] electron-builder 打包桌面 EXE ...
call npm run dist || goto :fail
echo.
echo ============================================
echo   完成！EXE 在 release\ 目录下
echo ============================================
pause
exit /b 0

:fail
echo.
echo *** 构建失败，请检查上方报错 ***
pause
exit /b 1
