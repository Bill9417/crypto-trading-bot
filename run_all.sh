#!/usr/bin/env bash
#
# run_all.sh — start the Wolf Scanner web dashboard + the scanner bot together.
#
#   ./run_all.sh           start both attached (live log; Ctrl+C / closing the
#                          terminal STOPS both)
#   ./run_all.sh bg        start both DETACHED — keeps running after you close
#                          the terminal or press Ctrl+C. Stop with: ./run_all.sh stop
#   ./run_all.sh stop      stop any running web/bot processes and exit
#   ./run_all.sh status    show whether each process is running
#
# Runs the test suite first (the safety gate). Bypass with SKIP_TESTS=1.
# Safe to re-run: it kills any previous instances and clears stale locks first,
# so you never end up with duplicate bots double-scanning / double-alerting.

set -u

# --- locations -------------------------------------------------------------
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # crypto/
APP="$DIR/app"                                          # crypto/app/
cd "$APP"
PYTHON="/Users/wolfman/miniforge3/bin/python"
LOG_DIR="$APP/logs"
mkdir -p "$LOG_DIR"

# --- helpers ---------------------------------------------------------------
stop_all() {
    echo "Stopping Wolf Scanner (web + bot)..."
    pkill -f "[p]ython.*app.py" 2>/dev/null && echo "  • web app stopped"  || echo "  • web app not running"
    pkill -f "[p]ython.*bot.py" 2>/dev/null && echo "  • bot stopped"      || echo "  • bot not running"
    pkill -f "[t]ail -n 5 -f .*logs/bot.log" 2>/dev/null  # kill any stray log tail from a prior run
    rm -f "$APP/bot.lock" 2>/dev/null
}

status_all() {
    if pgrep -f "[p]ython.*app.py" >/dev/null; then echo "  web app : RUNNING (pid $(pgrep -f '[p]ython.*app.py' | tr '\n' ' '))"; else echo "  web app : stopped"; fi
    if pgrep -f "[p]ython.*bot.py" >/dev/null; then echo "  bot     : RUNNING (pid $(pgrep -f '[p]ython.*bot.py' | tr '\n' ' '))"; else echo "  bot     : stopped"; fi
}

# --- subcommands -----------------------------------------------------------
MODE="start"
case "${1:-start}" in
    stop)   stop_all; exit 0 ;;
    status) echo "Wolf Scanner status:"; status_all; exit 0 ;;
    bg)     MODE="bg" ;;       # detached: survives terminal close / Ctrl+C
    start)  MODE="start" ;;    # attached: live log, Ctrl+C stops both
    *)      echo "Usage: $0 [start|bg|stop|status]"; exit 1 ;;
esac

# --- sanity check ----------------------------------------------------------
if [ ! -x "$PYTHON" ]; then
    echo "ERROR: python not found at $PYTHON"; exit 1
fi
if [ ! -f "$APP/.env" ]; then
    echo "WARNING: no .env found — using defaults (dashboard on 127.0.0.1:4000)."
fi

# --- pre-flight tests (safety gate) ---------------------------------------
./preflight.sh || { echo "Launch aborted — tests failed (bypass with SKIP_TESTS=1)."; exit 1; }

# --- clean slate -----------------------------------------------------------
echo "Clearing any previous instances..."
pkill -f "[p]ython.*app.py" 2>/dev/null
pkill -f "[p]ython.*bot.py" 2>/dev/null
pkill -f "[t]ail -n 5 -f .*logs/bot.log" 2>/dev/null  # kill any stray log tail from a prior run
rm -f "$APP/bot.lock" 2>/dev/null
sleep 1

HOST="$(grep -E '^FLASK_HOST=' .env 2>/dev/null | cut -d= -f2)"; HOST="${HOST:-127.0.0.1}"
PORT="$(grep -E '^FLASK_PORT=' .env 2>/dev/null | cut -d= -f2)"; PORT="${PORT:-4000}"

# --- DETACHED mode: launch with nohup + disown, print status, exit ---------
# These processes have no controlling terminal, so closing the terminal or
# pressing Ctrl+C cannot stop them. Stop them explicitly with: ./run_all.sh stop
if [ "$MODE" = "bg" ]; then
    echo "Starting DETACHED (keeps running after you close the terminal)..."
    nohup "$PYTHON" -u app.py >> "$LOG_DIR/app.log" 2>&1 & disown
    nohup "$PYTHON" -u bot.py >> "$LOG_DIR/bot.log" 2>&1 & disown
    sleep 3
    echo ""
    echo "──────────────────────────────────────────────"
    echo "  Wolf Scanner is running (detached)"
    status_all
    echo "  Dashboard : http://$HOST:$PORT"
    echo "  Watch log : tail -f $LOG_DIR/bot.log"
    echo "  Stop both : $0 stop"
    echo "──────────────────────────────────────────────"
    exit 0
fi

# --- start web app ---------------------------------------------------------
# -u = unbuffered stdout/stderr, so print() output streams to the log file in
# real time instead of being block-buffered (otherwise the logs look "empty").
echo "Starting web dashboard..."
"$PYTHON" -u app.py >> "$LOG_DIR/app.log" 2>&1 &
APP_PID=$!

# --- start bot -------------------------------------------------------------
echo "Starting scanner bot..."
"$PYTHON" -u bot.py >> "$LOG_DIR/bot.log" 2>&1 &
BOT_PID=$!

# --- clean shutdown on Ctrl+C / kill --------------------------------------
cleanup() {
    echo ""
    echo "Shutting down..."
    kill "$APP_PID" "$BOT_PID" 2>/dev/null
    # give the bot a moment to release its lock, then force if needed
    sleep 2
    pkill -f "[p]ython.*app.py" 2>/dev/null
    pkill -f "[p]ython.*bot.py" 2>/dev/null
    pkill -f "[t]ail -n 5 -f .*logs/bot.log" 2>/dev/null  # kill any stray log tail from a prior run
    rm -f "$APP/bot.lock" 2>/dev/null
    echo "Both stopped."
    exit 0
}
trap cleanup INT TERM

# --- report ----------------------------------------------------------------
sleep 2
echo ""
echo "──────────────────────────────────────────────"
echo "  Wolf Scanner is running"
echo "  Dashboard : http://$HOST:$PORT"
echo "  Web log   : $LOG_DIR/app.log   (pid $APP_PID)"
echo "  Bot log   : $LOG_DIR/bot.log   (pid $BOT_PID)"
echo "  Press Ctrl+C to stop both."
echo "──────────────────────────────────────────────"
echo ""
echo "Live bot log (Ctrl+C to stop everything):"
echo ""

# Stream the bot log so you see scans live; if either process dies, exit.
tail -n 5 -f "$LOG_DIR/bot.log" &
TAIL_PID=$!

while kill -0 "$APP_PID" 2>/dev/null && kill -0 "$BOT_PID" 2>/dev/null; do
    sleep 3
done

# one of them exited on its own
kill "$TAIL_PID" 2>/dev/null
echo ""
echo "A process exited. Check the logs above."
cleanup
