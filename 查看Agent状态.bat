@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   V3 本地 Agent - 后台运行状态
echo ============================================================
echo.

set PY313=C:\Users\24773\AppData\Local\Programs\Python\Python313\python.exe
if not exist "%PY313%" set PY313=python

if not exist "scripts\status-agent.py" (
  echo   [错误] 找不到 scripts\status-agent.py
  echo.
  pause
  exit /b 1
)

"%PY313%" scripts\status-agent.py

echo.
echo ============================================================
echo   按任意键关闭本窗口（不会影响后台 Agent）
echo ============================================================
pause >nul
exit /b 0
