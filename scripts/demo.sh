#!/usr/bin/env sh
# Convenience wrapper: start the API locally, run the demo, stop the API.
set -eu

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"
export OUTAGE_DB_PATH="${OUTAGE_DB_PATH:-$(mktemp -d)/demo.db}"

"$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT INT TERM

"$PYTHON" - "$PORT" <<'PY'
import sys
import time
import urllib.request

url = f"http://127.0.0.1:{sys.argv[1]}/health"
for _ in range(100):
    try:
        urllib.request.urlopen(url, timeout=0.5).read()
        break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("API did not become healthy in time")
PY

"$PYTHON" scripts/demo.py "http://127.0.0.1:$PORT"
