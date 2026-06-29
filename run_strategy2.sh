#!/bin/bash
set -euo pipefail

# Strategy-2 15m signal scanner — ALERT + PAGE only, never places orders.
# Safe to run alongside the live bot (its own REST client / rate budget).
echo "Starting Strategy-2 15m signal scanner..."

cd "$(dirname "${BASH_SOURCE[0]}")/app"

PY=/Users/wolfman/miniforge3/bin/python

existing_pids="$(pgrep -f "$PY strategy2_scanner.py" || true)"
if [ -n "$existing_pids" ]; then
  echo "Stopping existing scanner process(es): $existing_pids"
  echo "$existing_pids" | xargs kill
  for _ in 1 2 3 4 5; do
    sleep 1
    if ! pgrep -f "$PY strategy2_scanner.py" >/dev/null; then
      break
    fi
  done
fi

"$PY" strategy2_scanner.py
