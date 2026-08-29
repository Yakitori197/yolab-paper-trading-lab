#!/usr/bin/env bash
# Read-only dashboard on http://127.0.0.1:8787 (foreground; Ctrl-C to stop).
set -u
cd "$(dirname "$0")/.."

echo "Dashboard: http://127.0.0.1:8787"
exec py/.venv/bin/python -m uvicorn dashboard:app --app-dir py --host 127.0.0.1 --port 8787
