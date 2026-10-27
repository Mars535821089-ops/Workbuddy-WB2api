#!/usr/bin/env bash
# ===========================================================================
# workbuddy2api macOS/Linux 守护进程停止脚本
# ===========================================================================
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8790}"
PID_FILE="$DIR/.workbuddy/server.pid"

STOPPED=0

# 1. 尝试通过 PID 文件停止
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "正在停止 PID: $PID..."
    kill -15 "$PID" 2>/dev/null || true
    for i in {1..10}; do
      if ! kill -0 "$PID" 2>/dev/null; then
        STOPPED=1
        break
      fi
      sleep 0.5
    done
    if [ "$STOPPED" -eq 0 ]; then
      echo "进程未响应 SIGTERM，正在强制终止 (SIGKILL)..."
      kill -9 "$PID" 2>/dev/null || true
    fi
  fi
  rm -f "$PID_FILE"
fi

# 2. 检查是否有遗留的端口监听
PORT_PIDS=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [ -n "$PORT_PIDS" ]; then
  echo "正在清理端口 $PORT 遗留进程: $PORT_PIDS"
  kill -15 $PORT_PIDS 2>/dev/null || true
  sleep 1
  PORT_PIDS=$(lsof -ti :"$PORT" 2>/dev/null || true)
  if [ -n "$PORT_PIDS" ]; then
    kill -9 $PORT_PIDS 2>/dev/null || true
  fi
fi

echo "workbuddy2api 服务已停止。"
