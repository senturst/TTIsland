@echo off
chcp 65001 >nul 2>&1
REM ============================================================================
REM  孤岛残响 · 一键部署脚本（Windows）
REM
REM  做了什么：
REM    1. 在项目根创建 .venv 虚拟环境（已存在则复用）
REM    2. 安装 requirements.txt 依赖
REM    3. 委托 scripts\restart.py 完成「端口清理 → 配置/数据库校验 → 后台启动」
REM
REM  用法：
REM    双击本文件，或在项目根目录执行 scripts\deploy.bat
REM    scripts\deploy.bat --stop     仅停止服务
REM    scripts\deploy.bat --fg       前台运行（看日志用）
REM
REM  注意：纯配置文件（configs\*.yaml）走应用层热重载，改数值不用重跑本脚本；
REM        只有改了 Python 代码或增删物品/怪物/天赋 ID 才需要重启（restart.py 已处理）。
REM ============================================================================
setlocal

set "ROOT=%~dp0.."
cd /d "%ROOT%"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not defined PORT set "PORT=8000"

echo [1/3] 准备虚拟环境 …
if not exist "%PY%" (
    echo     创建 .venv …
    python -m venv "%ROOT%\.venv"
)

echo [2/3] 安装依赖 …
"%PY%" -m pip install -q --upgrade pip
"%PY%" -m pip install -q -r "%ROOT%\requirements.txt"

echo [3/3] 部署并启动（委托 restart.py）…
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
echo 部署完成： http://127.0.0.1:%PORT%/
echo 查看日志： type "%ROOT%\logs\server.log"

endlocal
