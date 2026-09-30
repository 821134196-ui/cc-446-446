#!/usr/bin/env bash
# 一条本地启动命令：建库 + 载入在住/待退/争议样例 + 启动 Web 与进程内提醒。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${MINIBAR_PORT:-8000}"
HOST="${MINIBAR_HOST:-127.0.0.1}"
RESET=""
[ "${1:-}" = "--reset" ] && RESET="--reset"

PY="$(command -v python3 || command -v python)"
echo "使用 $PY（仅标准库，无需安装依赖）"
exec "$PY" -m minibar.app --host "$HOST" --port "$PORT" $RESET
