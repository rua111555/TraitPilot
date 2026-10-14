#!/usr/bin/env bash
# SkillAgent Web 一键启动/停止
#
#   ./start_web.sh                 启动（后台，日志 web.log）→ http://127.0.0.1:8787
#   ./start_web.sh 9000            指定端口
#   ./start_web.sh 0.0.0.0:8787    指定主机:端口（局域网可访问，慎用）
#   ./start_web.sh stop [主机:端口] 停止
set -u
cd "$(dirname "$0")"

ACTION=start
HOST=127.0.0.1
PORT=8787
if [ "${1:-}" = "stop" ]; then ACTION=stop; shift; fi
case "${1:-}" in
  "" ) ;;
  *:* ) HOST="${1%%:*}"; PORT="${1##*:}" ;;
  *)   if [ -n "${2:-}" ]; then HOST="$1"; PORT="$2"; else PORT="$1"; fi ;;
esac
PORT="${PORT:-8787}"
HOST="${HOST:-127.0.0.1}"

pat="run_web.py --host $HOST --port $PORT"

if [ "$ACTION" = "stop" ]; then
  pkill -f "$pat" 2>/dev/null && echo "已停止  http://$HOST:$PORT" || echo "未发现运行中的 http://$HOST:$PORT"
  exit 0
fi

if pgrep -f "$pat" >/dev/null 2>&1; then
  echo "已在运行：http://$HOST:$PORT  （日志：web.log）"
  exit 0
fi

nohup python3 run_web.py --host "$HOST" --port "$PORT" > web.log 2>&1 &
sleep 2
code=$(curl -s -o /dev/null -w "%{http_code}" -m 3 "http://$HOST:$PORT/api/config")
if [ "$code" = "200" ]; then
  echo "✅ 已启动  http://$HOST:$PORT   （日志 web.log / 停止 ./start_web.sh stop）"
else
  echo "⚠️  启动可能失败，看 web.log："; tail -5 web.log
fi
