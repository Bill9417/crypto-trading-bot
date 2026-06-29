#!/usr/bin/env bash
#
# run_all.sh — start the Wolf Scanner web dashboard + the live trading engine.
#
#   ./run_all.sh           start attached (live log; Ctrl+C / closing the
#                          terminal STOPS everything)
#   ./run_all.sh bg        start DETACHED — keeps running after you close the
#                          terminal or press Ctrl+C. Stop with: ./run_all.sh stop
#   ./run_all.sh stop      stop any running web / bot / scanner processes
#   ./run_all.sh status    show whether each process is running
#
# ── ONE ENGINE AT A TIME ─────────────────────────────────────────────────────
# The live engine is chosen by STRATEGY2_LIVE in app/.env (the same switch that
# arms Strategy-2 live trading), so the 25 USDT account is only ever driven by
# one strategy:
#   STRATEGY2_LIVE=false (default) → web + S1 bot (LIVE) + S2 scanner (alert-only,
#                                    so /strategy2 still shows live 15m signals)
#   STRATEGY2_LIVE=true            → web + S2 scanner (LIVE engine); S1 is NOT
#                                    started at all
#
# Runs the test suite first (the safety gate). Bypass with SKIP_TESTS=1.
# Safe to re-run: it kills any previous instances and clears stale locks first,
# so you never end up with duplicate engines double-trading / double-alerting.

set -u

# --- locations -------------------------------------------------------------
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # crypto/
APP="$DIR/app"                                          # crypto/app/
cd "$APP"
PYTHON="/Users/wolfman/miniforge3/bin/python"
LOG_DIR="$APP/logs"
mkdir -p "$LOG_DIR"

# --- helpers ---------------------------------------------------------------
# Read a boolean KEY from app/.env (true/1/yes/on, case-insensitive). Returns 0
# (shell-true) when set, 1 otherwise. Mirrors config._env_bool so the launcher
# and the bot agree on which engine is live.
read_env_bool() {
    local val
    val="$(grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-)"
    val="${val%%#*}"                                   # drop any inline comment
    val="$(printf '%s' "$val" | tr -d " \"'" | tr '[:upper:]' '[:lower:]')"
    [ "$val" = "1" ] || [ "$val" = "true" ] || [ "$val" = "yes" ] || [ "$val" = "on" ]
}

stop_all() {
    echo "Stopping Wolf Scanner (web + bot + scanner)..."
    pkill -f "[p]ython.*app.py"                2>/dev/null && echo "  • web app stopped"    || echo "  • web app not running"
    pkill -f "[p]ython.*bot.py"                2>/dev/null && echo "  • S1 bot stopped"     || echo "  • S1 bot not running"
    pkill -f "[p]ython.*strategy2_scanner.py"  2>/dev/null && echo "  • S2 scanner stopped" || echo "  • S2 scanner not running"
    pkill -f "[t]ail -n 5 -f .*logs/" 2>/dev/null   # kill any stray log tail from a prior run
    rm -f "$APP/bot.lock" 2>/dev/null
}

status_all() {
    if pgrep -f "[p]ython.*app.py"               >/dev/null; then echo "  web app    : RUNNING (pid $(pgrep -f '[p]ython.*app.py' | tr '\n' ' '))";              else echo "  web app    : stopped"; fi
    if pgrep -f "[p]ython.*bot.py"               >/dev/null; then echo "  S1 bot     : RUNNING (pid $(pgrep -f '[p]ython.*bot.py' | tr '\n' ' '))";              else echo "  S1 bot     : stopped"; fi
    if pgrep -f "[p]ython.*strategy2_scanner.py" >/dev/null; then echo "  S2 scanner : RUNNING (pid $(pgrep -f '[p]ython.*strategy2_scanner.py' | tr '\n' ' '))"; else echo "  S2 scanner : stopped"; fi
}

# --- choose the live engine from app/.env ----------------------------------
if read_env_bool STRATEGY2_LIVE; then
    ENGINE_NAME="Strategy 2 scanner (LIVE)"
    ENGINE_CMD="strategy2_scanner.py"
    ENGINE_LOG="$LOG_DIR/strategy2.log"
    START_ALERT_SCANNER=0           # the scanner IS the engine; don't start a 2nd one
else
    ENGINE_NAME="S1 bot (LIVE)"
    ENGINE_CMD="bot.py"
    ENGINE_LOG="$LOG_DIR/bot.log"
    START_ALERT_SCANNER=1           # run the S2 scanner alongside S1, alert-only
fi

# --- subcommands -----------------------------------------------------------
MODE="start"
case "${1:-start}" in
    stop)   stop_all; exit 0 ;;
    status) echo "Wolf Scanner status:"; echo "  live engine: $ENGINE_NAME"; status_all; exit 0 ;;
    bg)     MODE="bg" ;;       # detached: survives terminal close / Ctrl+C
    start)  MODE="start" ;;    # attached: live log, Ctrl+C stops everything
    *)      echo "Usage: $0 [start|bg|stop|status]"; exit 1 ;;
esac

# --- sanity check ----------------------------------------------------------
if [ ! -x "$PYTHON" ]; then
    echo "ERROR: python not found at $PYTHON"; exit 1
fi
if [ ! -f "$APP/.env" ]; then
    echo "WARNING: no .env found — using defaults (dashboard on 127.0.0.1:4000, engine = S1 bot)."
fi

# --- pre-flight tests (safety gate) ---------------------------------------
./preflight.sh || { echo "Launch aborted — tests failed (bypass with SKIP_TESTS=1)."; exit 1; }

# --- clean slate -----------------------------------------------------------
echo "Clearing any previous instances..."
pkill -f "[p]ython.*app.py" 2>/dev/null
pkill -f "[p]ython.*bot.py" 2>/dev/null
pkill -f "[p]ython.*strategy2_scanner.py" 2>/dev/null
pkill -f "[t]ail -n 5 -f .*logs/" 2>/dev/null  # kill any stray log tail from a prior run
rm -f "$APP/bot.lock" 2>/dev/null
sleep 1

HOST="$(grep -E '^FLASK_HOST=' .env 2>/dev/null | cut -d= -f2)"; HOST="${HOST:-127.0.0.1}"
PORT="$(grep -E '^FLASK_PORT=' .env 2>/dev/null | cut -d= -f2)"; PORT="${PORT:-4000}"

# --- DETACHED mode: launch with nohup + disown, print status, exit ---------
# These processes have no controlling terminal, so closing the terminal or
# pressing Ctrl+C cannot stop them. Stop them explicitly with: ./run_all.sh stop
if [ "$MODE" = "bg" ]; then
    echo "Starting DETACHED — live engine: $ENGINE_NAME"
    nohup "$PYTHON" -u app.py >> "$LOG_DIR/app.log" 2>&1 & disown
    nohup "$PYTHON" -u "$ENGINE_CMD" >> "$ENGINE_LOG" 2>&1 & disown
    if [ "$START_ALERT_SCANNER" = "1" ]; then
        nohup "$PYTHON" -u strategy2_scanner.py >> "$LOG_DIR/strategy2.log" 2>&1 & disown
    fi
    sleep 3
    echo ""
    echo "──────────────────────────────────────────────"
    echo "  Wolf Scanner is running (detached)"
    echo "  Live engine: $ENGINE_NAME"
    status_all
    echo "  Dashboard : http://$HOST:$PORT"
    echo "  Watch log : tail -f $ENGINE_LOG"
    echo "  Stop all  : $0 stop"
    echo "──────────────────────────────────────────────"
    exit 0
fi

# --- start web app ---------------------------------------------------------
# -u = unbuffered stdout/stderr, so print() output streams to the log file in
# real time instead of being block-buffered (otherwise the logs look "empty").
echo "Starting web dashboard..."
"$PYTHON" -u app.py >> "$LOG_DIR/app.log" 2>&1 &
APP_PID=$!

# --- start the live engine (S1 bot or S2 scanner) --------------------------
echo "Starting live engine: $ENGINE_NAME"
"$PYTHON" -u "$ENGINE_CMD" >> "$ENGINE_LOG" 2>&1 &
ENGINE_PID=$!

# --- optionally start the S2 scanner alongside S1 (alert-only) -------------
SCANNER_PID=""
if [ "$START_ALERT_SCANNER" = "1" ]; then
    echo "Starting S2 scanner (alert-only companion)..."
    "$PYTHON" -u strategy2_scanner.py >> "$LOG_DIR/strategy2.log" 2>&1 &
    SCANNER_PID=$!
fi

# --- clean shutdown on Ctrl+C / kill --------------------------------------
cleanup() {
    echo ""
    echo "Shutting down..."
    kill "$APP_PID" "$ENGINE_PID" 2>/dev/null
    [ -n "$SCANNER_PID" ] && kill "$SCANNER_PID" 2>/dev/null
    # give the bot a moment to release its lock, then force if needed
    sleep 2
    pkill -f "[p]ython.*app.py" 2>/dev/null
    pkill -f "[p]ython.*bot.py" 2>/dev/null
    pkill -f "[p]ython.*strategy2_scanner.py" 2>/dev/null
    pkill -f "[t]ail -n 5 -f .*logs/" 2>/dev/null  # kill any stray log tail from a prior run
    rm -f "$APP/bot.lock" 2>/dev/null
    echo "All stopped."
    exit 0
}
trap cleanup INT TERM

# --- report ----------------------------------------------------------------
sleep 2
echo ""
echo "──────────────────────────────────────────────"
echo "  Wolf Scanner is running"
echo "  Live engine : $ENGINE_NAME"
echo "  Dashboard   : http://$HOST:$PORT"
echo "  Web log     : $LOG_DIR/app.log        (pid $APP_PID)"
echo "  Engine log  : $ENGINE_LOG   (pid $ENGINE_PID)"
[ -n "$SCANNER_PID" ] && echo "  Scanner log : $LOG_DIR/strategy2.log  (pid $SCANNER_PID, alert-only)"
echo "  Press Ctrl+C to stop everything."
echo "──────────────────────────────────────────────"
echo ""
echo "Live engine log (Ctrl+C to stop everything):"
echo ""

# Stream the live engine's log so you see it work; if a core process dies, exit.
tail -n 5 -f "$ENGINE_LOG" &
TAIL_PID=$!

while kill -0 "$APP_PID" 2>/dev/null && kill -0 "$ENGINE_PID" 2>/dev/null; do
    sleep 3
done

# the web app or the live engine exited on its own
kill "$TAIL_PID" 2>/dev/null
echo ""
echo "A core process exited. Check the logs above."
cleanup
