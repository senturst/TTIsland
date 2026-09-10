@echo off
REM ============================================================================
REM  本地开发服务器启动脚本（Windows）
REM
REM  为什么需要它：
REM    直接跑 uvicorn 时，上一轮的进程经常没被真正杀掉，端口仍被占用，
REM    于是新进程启动失败（WinError 10048），而你以为代码改动没生效——
REM    实际上服务跑的还是旧代码。这个脚本先按端口精确清理，再启动。
REM
REM  用法：双击，或在项目根目录执行 scripts\dev.bat
REM ============================================================================
setlocal

set PORT=8000
set ROOT=%~dp0..
cd /d "%ROOT%"

echo [1/3] 清理端口 %PORT% ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    echo       结束占用进程 PID %%p
    taskkill /PID %%p /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo [2/3] 校验配置与数据库 ...
".venv\Scripts\python.exe" -c "from app.data.loader import get_config; get_config(); from app.db import migrate; migrate.apply_migrations(); migrate.init_llm_cache_db(); print('       OK')"
if errorlevel 1 (
    echo       配置校验失败，已中止启动。请修正 configs\ 下的问题后重试。
    pause
    exit /b 1
)

echo [3/3] 启动 http://127.0.0.1:%PORT% ...
echo       按 Ctrl+C 停止
echo.
REM  --reload 只用于 Python 代码。配置（configs\*.yaml 与提示词）走应用层热重载：
REM   数值改动即时生效，破坏性变更（删除 ID）会被拦截并要求重启，
REM   比进程级重启更安全——不会丢掉正在进行的一局。
if "%1"=="--no-reload" (
    ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --log-level info
) else (
    ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --log-level info --reload --reload-dir app
)

endlocal
