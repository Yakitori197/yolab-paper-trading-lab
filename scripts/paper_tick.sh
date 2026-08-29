#!/usr/bin/env bash
# One paper-trading tick: fetch latest closed bars, replay, update paper.db,
# then refresh the xlsx ledger. Intended to be run by cron every 4 hours at
# :05 past the boundary (see README for the crontab line).
set -u
cd "$(dirname "$0")/.."

mkdir -p data
{
  echo "==== TICK START $(date -u '+%Y-%m-%d %H:%M:%S') UTC ===="
  py/.venv/bin/python py/paper_loop.py
  py/.venv/bin/python tools/export_trades.py
} >> data/tick.log 2>&1
