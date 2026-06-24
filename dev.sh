#!/usr/bin/env bash
#
# dev.sh — run ONLY the web dashboard in hot-reload mode for fast iteration.
#
# Edit any .py and the server restarts itself; edit a template/CSS/JS and just
# refresh the browser. No manual re-run, no preflight gate (it's a dev loop).
#
# The bot is NOT started here — bot/strategy changes should be applied with a
# deliberate restart (you don't want a live trading bot auto-flapping).
#
#   ./dev.sh            start the auto-reloading web on http://127.0.0.1:4000
#   Ctrl+C             stop it
#
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR/app"

# free the port if a previous web instance is up
lsof -ti:4000 | xargs kill 2>/dev/null || true

echo "Dev web (hot-reload) → http://127.0.0.1:4000   [Ctrl+C to stop]"
FLASK_RELOAD=true exec /Users/wolfman/miniforge3/bin/python app.py
