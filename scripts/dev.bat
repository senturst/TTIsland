@echo off
chcp 65001 >nul 2>&1
REM ============================================================================
REM  TTIsland - local dev server launcher (Windows)
REM
REM  Why this script exists:
REM    Running uvicorn directly often leaves the previous process alive, holding
REM    the port (WinError 10048) so the new process fails to start and you think
REM    your code change did not take effect - but it's the OLD code still running.
REM    This script cleans the port precisely, then starts.
REM
REM  Usage: double-click, or run scripts\dev.bat from project root
REM ============================================================================
setlocal

set PORT=8000
set ROOT=%~dp0..
cd /d "%ROOT%"

echo [1/3] Freeing port %PORT% ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    echo       Killing PID %%p
    taskkill /PID %%p /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo [2/3] Checking config and database ...
".venv\Scripts\python.exe" -c "from app.data.loader import get_config; get_config(); from app.db import migrate; migrate.apply_migrations(); migrate.init_llm_cache_db(); print('       OK')"
if errorlevel 1 (
    echo       Config check failed; startup aborted. Fix configs\ and retry.
    pause
    exit /b 1
)

echo [3/3] Starting http://127.0.0.1:%PORT% ...
echo       Press Ctrl+C to stop
echo.
REM  --reload applies to Python code only. Configs (configs\*.yaml and prompts)
REM  are hot-reloaded by the app: balance edits apply instantly, breaking changes
REM  (deleted IDs) are blocked and require restart - safer than a process restart
REM  because it will not drop an in-progress run.
if "%1"=="--no-reload" (
    ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT% --log-level info
) else (
    ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT% --log-level info --reload --reload-dir app
)

endlocal
