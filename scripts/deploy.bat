@echo off
chcp 65001 >nul 2>&1
REM ============================================================================
REM  TTIsland - one-click deploy script (Windows)
REM
REM  What it does:
REM    1. Create .venv under project root (reuse if it already exists)
REM    2. Install requirements.txt
REM    3. Delegate to scripts\restart.py for port cleanup, config/db check, start
REM
REM  Usage:
REM    Double-click, or run scripts\deploy.bat from project root
REM    scripts\deploy.bat --stop     stop service only
REM    scripts\deploy.bat --fg       run in foreground (watch logs)
REM
REM  Note: configs\*.yaml are hot-reloaded by the app; you do NOT need to re-run
REM        this script for balance tweaks. Restart only when Python code or item/
REM        monster/talent IDs change (restart.py handles that).
REM ============================================================================
setlocal

set "ROOT=%~dp0.."
cd /d "%ROOT%"
REM 显式把项目根加入 PYTHONPATH：restart.py 内部的 `python -c` 配置校验
REM 与 `python -m uvicorn` 都会继承此变量，确保稳定导入 app 包
set "PYTHONPATH=%ROOT%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not defined PORT set "PORT=8000"

echo [1/3] Preparing virtualenv...
if not exist "%PY%" (
    echo     Creating .venv...
    python -m venv "%ROOT%\.venv"
)

echo [2/3] Installing dependencies...
"%PY%" -m pip install -q --upgrade pip
"%PY%" -m pip install -q -r "%ROOT%\requirements.txt"

echo [3/3] Deploying and starting (via restart.py)...
if "%1"=="--stop" (
    "%PY%" "%ROOT%\scripts\restart.py" --stop
    goto :eof
)
if "%1"=="--fg" (
    "%PY%" "%ROOT%\scripts\restart.py" --fg
    goto :eof
)
"%PY%" "%ROOT%\scripts\restart.py"
echo.
echo Deploy done: http://127.0.0.1:%PORT%/
echo Logs: type "%ROOT%\logs\server.log"

endlocal
