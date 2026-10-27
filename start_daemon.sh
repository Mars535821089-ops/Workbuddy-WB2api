#!/usr/bin/env bash
# ===========================================================================
# workbuddy2api macOS/Linux 守护进程启动脚本
# ===========================================================================
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

PORT="${PORT:-8790}"
HOST="${HOST:-0.0.0.0}"
PID_FILE="$DIR/.workbuddy/server.pid"
LOG_DIR="$DIR/logs"
LOG_FILE="$LOG_DIR/server.log"

mkdir -p "$DIR/.workbuddy" "$LOG_DIR"

# 1. 检查端口是否已被占用
RUNNING_PID=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [ -n "$RUNNING_PID" ]; then
  echo "服务已在端口 $PORT 运行 (PID: $RUNNING_PID)。"
  echo "如需重启，请先运行 ./stop_daemon.sh 或手动终止。"
  exit 0
fi

# 2. 定位 Python 解释器
if [ -x "$DIR/.venv/bin/uvicorn" ]; then
  UVICORN="$DIR/.venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN="$(command -v uvicorn)"
else
  echo "错误: 未找到 uvicorn，请确保已安装虚拟环境 .venv"
  exit 1
fi

echo "正在启动 workbuddy2api 服务 (端口 $PORT)..."
nohup "$UVICORN" admin.server:app --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# 3. 等待服务就绪并健康检查
echo "等待服务启动 (PID: $NEW_PID)..."
SUCCESS=0
for i in {1..15}; do
  sleep 1
  if curl -s -f -o /dev/null "http://127.0.0.1:$PORT/admin"; then
    SUCCESS=1
    break
  fi
  # 检查进程是否意外退出
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    echo "错误: 服务进程异常退出，请查看日志: $LOG_FILE"
    tail -n 20 "$LOG_FILE"
    exit 1
  fi
done

if [ "$SUCCESS" -eq 1 ]; then
  echo "=========================================================="
  echo "workbuddy2api 已成功启动！"
  echo "后台管理地址: http://127.0.0.1:$PORT/admin"
  echo "进程 PID:     $NEW_PID"
  echo "运行日志:     $LOG_FILE"
  echo "停止命令:     ./stop_daemon.sh"
  echo "=========================================================="
else
  echo "警告: 服务已启动 (PID: $NEW_PID)，但未能及时响应 HTTP 请求，请检查日志: $LOG_FILE"
fi
