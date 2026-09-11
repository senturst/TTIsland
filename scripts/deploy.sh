#!/usr/bin/env bash
# ============================================================================
#  孤岛残响 · 一键部署脚本（Linux / macOS）
#
#  做了什么：
#    1. 在项目根创建 .venv 虚拟环境（已存在则复用）
#    2. 安装 requirements.txt 依赖
#    3. 委托 scripts/restart.py 完成「端口清理 → 配置/数据库校验 → 后台启动」
#
#  用法：
#    bash scripts/deploy.sh            # 部署并后台启动
#    bash scripts/deploy.sh --stop     # 只停止服务
#    bash scripts/deploy.sh --fg       # 前台运行（看日志用）
#
#  注意：纯配置文件（configs/*.yaml）走应用层热重载，改数值不用重跑本脚本；
#        只有改了 Python 代码或增删物品/怪物/天赋 ID 才需要重启（restart.py 已处理）。
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
PORT="${PORT:-8000}"

echo "[1/3] 准备虚拟环境 …"
if [ ! -x "$PY" ]; then
  echo "    创建 .venv …"
  python3 -m venv "$ROOT/.venv"
fi

echo "[2/3] 安装依赖 …"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r "$ROOT/requirements.txt"

echo "[3/3] 部署并启动（委托 restart.py）…"
# --reload 关闭：部署版用稳定后台进程，避免 reload 子进程孤儿
if [ "${1:-}" = "--stop" ] || [ "${1:-}" = "--fg" ]; then
  "$PY" "$ROOT/scripts/restart.py" "$1"
else
  "$PY" "$ROOT/scripts/restart.py"
  echo
  echo "部署完成： http://127.0.0.1:${PORT}/"
  echo "查看日志： tail -f $ROOT/logs/server.log"
fi
