@echo off
chcp 936 >nul
REM ===================================================================
REM  V3 Agent 启动脚本（源码模式）
REM
REM  为什么需要这个：
REM   dist\V3Agent.exe 是 PyInstaller 单文件打包，源码冻结在 exe 里。
REM   改了 v3agent\*.py 后，必须重新打包 或 改用源码运行。
REM   直接双击旧 exe，跑的还是旧代码（分页就是因此不生效）。
REM
REM  本脚本用源码 + 系统 Python 直接运行，改完 .py 重启即可生效。
REM
REM  用法：右键 以管理员身份运行
REM ===================================================================

set "PY=C:\Users\24773\AppData\Local\Programs\Python\Python313\python.exe"
cd /d "D:\v3-wechat"

echo ==========================================
echo   V3 Agent 源码模式
echo ==========================================
echo   Python: %PY%
echo   目录:   %CD%
echo ==========================================
echo.

if not exist "%PY%" (
  echo [错误] 找不到 Python:
  echo        %PY%
  echo        请确认系统 Python 3.13 已安装，或修改本脚本里的 PY 变量。
  echo.
  pause
  exit /b 1
)

"%PY%" -c "import pymssql, websockets" 2>nul
if errorlevel 1 (
  echo [错误] 缺少依赖，请先执行:
  echo        "%PY%" -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo 依赖检查通过，正在连接云端...
echo 本窗口不要关闭，关掉就等于 Agent 下线。
echo.

"%PY%" -m v3agent.main --config config.json --ws

echo.
echo Agent 已退出。
pause
