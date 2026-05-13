#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PORT=8888
for ((i=1; i<=$#; i++)); do
  if [[ "${!i}" == "--port" ]]; then
    next=$((i + 1))
    if [[ $next -le $# ]]; then
      PORT="${!next}"
    fi
  elif [[ "${!i}" == --port=* ]]; then
    PORT="${!i#--port=}"
  fi
done

echo "Freeing HTTP port ${PORT}..."
if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -ti tcp:${PORT} -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    kill -9 $pids 2>/dev/null || true
  fi
fi

# Kill EOS Utility and gphoto2 processes that may block USB access
echo "Clearing USB locks..."
pkill -9 "EOS Utility" 2>/dev/null || true
pkill -9 gphoto2 2>/dev/null || true
pkill -9 ptpcamerad 2>/dev/null || true
sleep 1

if [[ ! -x ".venv/bin/python" ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -q -r requirements.txt
echo "Starting Canon EOS Web Preview..."
echo "Open your browser to: http://localhost:8888"
exec .venv/bin/python server.py "$@"
