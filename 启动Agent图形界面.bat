@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   V3 本地 Agent 控制台
echo ============================================================
echo.
echo   正在打开控制台窗口，请稍候...
echo.
echo   使用步骤：
echo     1. 窗口里点【启动后台 Agent】
echo     2. 启动成功后，这个窗口可以直接关掉
echo        ---- Agent 会继续在后台运行，不受影响 ----
echo     3. 想停止时，重新双击本文件，点【停止后台 Agent】
echo.
echo   说明：窗口只是遥控器。关窗口 = 关遥控器，不是关 Agent。
echo.

if not exist "dist\V3AgentGui.exe" (
  echo   [错误] 没找到 dist\V3AgentGui.exe
  echo   请先运行：python scripts\build_gui.py
  echo.
  pause
  exit /b 1
)

start "" "dist\V3AgentGui.exe"

echo   已打开。本窗口 3 秒后自动关闭。
ping -n 4 127.0.0.1 >nul
exit /b 0
