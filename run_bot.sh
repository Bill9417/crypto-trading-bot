#!/bin/bash
set -euo pipefail

echo "Starting Crypto Scanner Bot..."

cd "$(dirname "${BASH_SOURCE[0]}")/app"

# Pre-flight tests FIRST — before touching the running bot, so a red suite never
# leaves you with no bot running. (set -e aborts here if preflight exits non-zero.)
./preflight.sh

existing_pids="$(pgrep -f '/Users/wolfman/miniforge3/bin/python bot.py' || true)"
if [ -n "$existing_pids" ]; then
  echo "Stopping existing bot process(es): $existing_pids"
  echo "$existing_pids" | xargs kill
  for _ in 1 2 3 4 5; do
    sleep 1
    if ! pgrep -f '/Users/wolfman/miniforge3/bin/python bot.py' >/dev/null; then
      break
    fi
  done
fi

/Users/wolfman/miniforge3/bin/python bot.py
