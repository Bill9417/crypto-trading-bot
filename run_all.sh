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
# The live engine is chosen by STRATEGY2_LIVE / STRATEGY3_LIVE in app/.env, so
# the 25 USDT account is only ever driven by one strategy:
#   both false (default)  → web + S1 bot (LIVE) + S2 + S3 scanners (alert-only)
#   STRATEGY2_LIVE=true   → web + S2 scanner (LIVE engine) + S1 scan-only + S3 alert-only
#   STRATEGY3_LIVE=true   → web + S3 flip engine (LIVE) + S1 scan-only + S2 alert-only
#   BOTH true             → S2 wins; S3 refuses to arm (in code) and stays alert-only
# S3 (strategy3_scanner.py) runs TWO engines per symbol (config.STRATEGY3_SYMBOLS
# / config.strategy3_params): "flagflip" — TV.pine flag + Vegas-line agreement,
# flip on the opposite flag (XAUT 30m; pine/strategies/TV_strategy_XAUT_30min.pine) — and
# "occ" — SMMA8 open/close cross on 90m buckets, stop-and-reverse (currently
# unused; STRATEGY3_OCC_* in app/.env routes symbols to it).
# Signals from Binance charts, orders on Bybit.
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

# _pids PATTERN — PIDs whose full command matches, read from ps, not pgrep.
#
# `pgrep -f` LIES on this machine. 2026-08-10, verbatim from restart.log while
# the process was demonstrably alive with 12h56m of uptime:
#
#     Clearing any previous instances...
#       • S2 scanner not running
#
# It had already done the same thing on 2026-08-09, when status_all printed one
# S2 pid at the exact moment two existed — the detail that made that incident
# look impossible to explain. Intermittent, and always in the same direction:
# it reports absence, never a phantom.
#
# ps walks the process table directly and has not been caught doing this, so it
# is the source of truth here. The [p]ython bracket trick still keeps the grep
# from matching itself.
_pids() {
    ps -Ao pid=,command= | grep -E "$1" | awk '{print $1}'
}

# kill_pattern PATTERN LABEL — SIGNAL FIRST, verify, then SIGKILL by PID.
#
# `pkill` reports that a signal was SENT, never that the target acted on it, and
# the original code took that exit status as proof of death. On 2026-08-09 a
# scanner outlived its own replacement by ~11h: two processes then long-polled
# getUpdates, Telegram answered the second with 409 Conflict, and every bot
# command was dead for half a day while `ps` showed a healthy stack.
#
# The first fix added a guard that asked "is it running?" and returned early
# when the answer was no. That made things WORSE — strictly worse than the code
# it replaced, which at least always attempted the kill. On 2026-08-10 pgrep
# answered "not running" for a live scanner, kill_pattern returned without
# sending a single signal, the launch proceeded, and a second scanner started
# beside the first: the exact failure the function exists to prevent, now caused
# by it.
#
# So: never ask permission to kill. Signal unconditionally — pkill against a
# pattern matching nothing is free — then verify with ps, and escalate to
# SIGKILL BY PID rather than by pattern, because pattern matching is the part
# that proved unreliable. A negative answer may not short-circuit anything.
kill_pattern() {
    local pat="$1" label="$2" i left
    pkill -f "$pat" 2>/dev/null              # unconditional; costs nothing
    for i in 1 2 3 4 5; do
        left="$(_pids "$pat")"
        [ -z "$left" ] && break
        sleep 1
    done
    left="$(_pids "$pat")"
    if [ -z "$left" ]; then
        echo "  • $label stopped"
        return 0
    fi
    echo "  ⚠ $label ignored SIGTERM after 5s — sending SIGKILL (pids: $(echo "$left" | tr '\n' ' '))"
    # shellcheck disable=SC2086 — deliberate word splitting: one kill, many pids
    kill -9 $left 2>/dev/null
    pkill -9 -f "$pat" 2>/dev/null           # belt and braces
    sleep 1
    left="$(_pids "$pat")"
    if [ -n "$left" ]; then
        echo "  ✗ $label SURVIVED SIGKILL — pids: $(echo "$left" | tr '\n' ' ')" >&2
        return 1
    fi
    echo "  • $label killed"
}

stop_all() {
    echo "Stopping Wolf Scanner (web + bot + scanner)..."
    kill_pattern "[p]ython.*app.py"               "web app"
    kill_pattern "[p]ython.*bot.py"               "S1 bot"
    kill_pattern "[p]ython.*strategy2_scanner.py" "S2 scanner"
    kill_pattern "[p]ython.*strategy3_scanner.py" "S3 flip"
    pkill -f "[t]ail -n 5 -f .*logs/" 2>/dev/null   # kill any stray log tail from a prior run
    rm -f "$APP/bot.lock" 2>/dev/null
    # Tell the auto-heal launchd agent this stop is INTENTIONAL — without the
    # flag it would relaunch everything within 5 minutes.
    touch "$DIR/.stack_stopped"
    echo "  • auto-heal paused (.stack_stopped) — next './run_all.sh bg' resumes it"
}

# status_all — also on ps, and it COUNTS. During the 2026-08-09 incident this
# printed "S2 scanner : RUNNING (pid 41184)" while two scanners were alive, so
# the duplicate was invisible in the one command you would run to look for it.
# A second pid is not a cosmetic detail here; it is the whole failure.
_status_line() {
    local label="$1" pat="$2" pids n
    pids="$(_pids "$pat" | tr '\n' ' ')"
    n="$(_pids "$pat" | grep -c .)"
    if [ "$n" -eq 0 ]; then
        printf "  %-11s: stopped\n" "$label"
    elif [ "$n" -eq 1 ]; then
        printf "  %-11s: RUNNING (pid %s)\n" "$label" "${pids% }"
    else
        printf "  %-11s: ⚠ %s COPIES RUNNING (pids %s) — duplicates double-alert and break Telegram\n" \
               "$label" "$n" "${pids% }"
    fi
}

status_all() {
    _status_line "web app"    "[p]ython.*app.py"
    _status_line "S1 bot"     "[p]ython.*bot.py"
    _status_line "S2 scanner" "[p]ython.*strategy2_scanner.py"
    _status_line "S3 flip"    "[p]ython.*strategy3_scanner.py"
}

# --- choose the live engine from app/.env ----------------------------------
# Precedence: S2 > S3 > S1. The S3 scanner ALWAYS runs (alert-only unless it is
# the armed engine — it self-gates in code, incl. refusing when S2 is also live).
START_S3_COMPANION=1
if read_env_bool STRATEGY2_LIVE; then
    ENGINE_NAME="Strategy 2 scanner (LIVE)"
    ENGINE_CMD="strategy2_scanner.py"
    ENGINE_LOG="$LOG_DIR/strategy2.log"
    START_ALERT_SCANNER=0           # the scanner IS the engine; don't start a 2nd one
    START_SCANONLY_S1=1            # S1 scan-only companion → keeps the main dashboard
                                    # refreshing hourly WITHOUT trading (no lock, no orders)
    if read_env_bool STRATEGY3_LIVE; then
        echo "WARNING: STRATEGY2_LIVE and STRATEGY3_LIVE are BOTH true — one live"
        echo "         engine at a time. S2 stays live; S3 will refuse to arm and"
        echo "         runs alert-only. Set STRATEGY2_LIVE=false to hand over to S3."
    fi
elif read_env_bool STRATEGY3_LIVE; then
    ENGINE_NAME="Strategy 3 Vegas Flag Flip (LIVE)"
    ENGINE_CMD="strategy3_scanner.py"
    ENGINE_LOG="$LOG_DIR/strategy3.log"
    START_S3_COMPANION=0            # the flip scanner IS the engine
    START_ALERT_SCANNER=1           # S2 keeps alerting (its live gate is false here)
    START_SCANONLY_S1=1
else
    ENGINE_NAME="S1 bot (LIVE)"
    ENGINE_CMD="bot.py"
    ENGINE_LOG="$LOG_DIR/bot.log"
    START_ALERT_SCANNER=1           # run the S2 scanner alongside S1, alert-only
    START_SCANONLY_S1=0            # S1 is already the live engine here
fi

# --- S1 execution venue for the companion ----------------------------------
# S1_BYBIT_MIRROR=true upgrades the S1 companion from scan-only to a REAL
# second engine that EXECUTES ON BYBIT (fixed S1_BYBIT_ORDER_USDT notional via
# the 🪞 mirror) while leaving Binance untouched — S1 signals + S3 flag-flip
# then trade side by side on the Bybit account. Without the flag the old
# scan-only behaviour is kept.
S1_COMPANION_ENV="SCAN_ONLY=true"
S1_COMPANION_DESC="S1 scan-only companion (refreshes the dashboard hourly; no orders)"
if read_env_bool S1_BYBIT_MIRROR; then
    S1_COMPANION_ENV="S1_EXEC=bybit"
    S1_COMPANION_DESC="S1 engine → BYBIT (🪞 mirror executes fills; Binance untouched)"
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

# --- ONE LAUNCH AT A TIME --------------------------------------------------
# A launch is kill-then-start with a ~60s pre-flight in between, which makes it
# a long non-atomic operation with THREE independent callers: restart.sh
# (Telegram /restart), autoheal.sh (launchd, every 5 min) and a human typing it.
#
# restart.sh has its own lock, but that only serialises restart-vs-restart —
# autoheal.sh calls this script directly and never sees it. Interleave two
# launches as kill(A) · kill(B) · start(A) · start(B) and both survive: a full
# duplicate stack, which is how two scanners end up long-polling getUpdates and
# taking every Telegram command down with 409 Conflict.
#
# The lock belongs HERE, on the operation, not on one of its callers. mkdir is
# atomic, so two simultaneous launches cannot both win.
#
# Stale-lock stealing is not optional: this script pkills a process tree and
# could itself be killed mid-flight, and a lock that outlives its owner would
# permanently disable both /restart and auto-heal — a far worse failure than the
# race it prevents. Anything older than 15 minutes is wreckage, not an owner.
LAUNCH_LOCK="$APP/.launch.lock"
if [ -d "$LAUNCH_LOCK" ]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LAUNCH_LOCK" 2>/dev/null || echo 0) ))
    if [ "$LOCK_AGE" -gt 900 ]; then
        echo "  (clearing a stale launch lock — ${LOCK_AGE}s old, owner is gone)"
        rmdir "$LAUNCH_LOCK" 2>/dev/null
    fi
fi
if ! mkdir "$LAUNCH_LOCK" 2>/dev/null; then
    echo "Another launch is already in progress (${LAUNCH_LOCK##*/}) — skipping this one."
    echo "That is the safe outcome: the in-flight launch will finish the job."
    exit 0
fi
trap 'rmdir "$LAUNCH_LOCK" 2>/dev/null' EXIT

# --- pre-flight tests (safety gate) ---------------------------------------
./preflight.sh || { echo "Launch aborted — tests failed (bypass with SKIP_TESTS=1)."; exit 1; }

# A deliberate launch re-arms the auto-heal agent.
rm -f "$DIR/.stack_stopped"

# --- clean slate -----------------------------------------------------------
# ABORT rather than launch on top of a survivor. Starting a second copy of a
# scanner is worse than not restarting at all: two engines double-alert, double
# every LINE push against a 200/month quota, and — the one that actually bit —
# fight over Telegram's getUpdates, which serves 409 Conflict to the loser and
# takes the whole command bot down. A failed restart that leaves the old stack
# running is recoverable and obvious; a silent duplicate is neither.
echo "Clearing any previous instances..."
CLEAN=0
kill_pattern "[p]ython.*app.py"               "web app"    || CLEAN=1
kill_pattern "[p]ython.*bot.py"               "S1 bot"     || CLEAN=1
kill_pattern "[p]ython.*strategy2_scanner.py" "S2 scanner" || CLEAN=1
kill_pattern "[p]ython.*strategy3_scanner.py" "S3 flip"    || CLEAN=1
pkill -f "[t]ail -n 5 -f .*logs/" 2>/dev/null  # kill any stray log tail from a prior run
rm -f "$APP/bot.lock" 2>/dev/null
if [ "$CLEAN" != "0" ]; then
    echo "" >&2
    echo "✗ LAUNCH ABORTED — a previous process survived SIGKILL (see above)." >&2
    echo "  The old stack is still running and untouched. Investigate that pid" >&2
    echo "  before restarting; starting a duplicate would break Telegram." >&2
    exit 1
fi

HOST="$(grep -E '^FLASK_HOST=' .env 2>/dev/null | cut -d= -f2)"; HOST="${HOST:-127.0.0.1}"
PORT="$(grep -E '^FLASK_PORT=' .env 2>/dev/null | cut -d= -f2)"; PORT="${PORT:-4000}"

# --- log rotation: keep files bounded (one .1 generation, >10MB rotates) ----
for f in "$LOG_DIR"/*.log; do
  if [ -f "$f" ] && [ "$(stat -f%z "$f" 2>/dev/null || echo 0)" -gt 10485760 ]; then
    mv "$f" "$f.1"
    echo "  (rotated $(basename "$f") — was over 10MB)"
  fi
done

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
    if [ "$START_S3_COMPANION" = "1" ]; then
        nohup "$PYTHON" -u strategy3_scanner.py >> "$LOG_DIR/strategy3.log" 2>&1 & disown
    fi
    if [ "$START_SCANONLY_S1" = "1" ]; then
        echo "  + $S1_COMPANION_DESC"
        env "$S1_COMPANION_ENV" nohup "$PYTHON" -u bot.py >> "$LOG_DIR/bot.log" 2>&1 & disown
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

# --- S3 Vegas-flip companion (alert-only unless it IS the engine) -----------
S3_PID=""
if [ "$START_S3_COMPANION" = "1" ]; then
    echo "Starting S3 Vegas Flag Flip (alert-only companion)..."
    "$PYTHON" -u strategy3_scanner.py >> "$LOG_DIR/strategy3.log" 2>&1 &
    S3_PID=$!
fi

# --- optionally start the S1 scan-only companion (S2-engine mode) ----------
# Scans + refreshes the main dashboard hourly, but holds no lock and places no
# orders, so S2 stays the sole live engine.
SCANONLY_PID=""
if [ "$START_SCANONLY_S1" = "1" ]; then
    echo "Starting $S1_COMPANION_DESC..."
    env "$S1_COMPANION_ENV" "$PYTHON" -u bot.py >> "$LOG_DIR/bot.log" 2>&1 &
    SCANONLY_PID=$!
fi

# --- clean shutdown on Ctrl+C / kill --------------------------------------
cleanup() {
    echo ""
    echo "Shutting down..."
    kill "$APP_PID" "$ENGINE_PID" 2>/dev/null
    [ -n "$SCANNER_PID" ] && kill "$SCANNER_PID" 2>/dev/null
    [ -n "$S3_PID" ] && kill "$S3_PID" 2>/dev/null
    [ -n "$SCANONLY_PID" ] && kill "$SCANONLY_PID" 2>/dev/null
    # give the bot a moment to release its lock, then force if needed
    sleep 2
    # Verified, same as every other kill path here — "All stopped." was printed
    # unconditionally before, which is the same unchecked claim that let a
    # scanner survive a restart and break Telegram for half a day.
    LEFT=0
    kill_pattern "[p]ython.*app.py"               "web app"    || LEFT=1
    kill_pattern "[p]ython.*bot.py"               "S1 bot"     || LEFT=1
    kill_pattern "[p]ython.*strategy2_scanner.py" "S2 scanner" || LEFT=1
    kill_pattern "[p]ython.*strategy3_scanner.py" "S3 flip"    || LEFT=1
    pkill -f "[t]ail -n 5 -f .*logs/" 2>/dev/null  # kill any stray log tail from a prior run
    rm -f "$APP/bot.lock" 2>/dev/null
    if [ "$LEFT" = "0" ]; then
        echo "All stopped."
        exit 0
    fi
    echo "✗ Something survived SIGKILL (see above) — NOT all stopped." >&2
    exit 1
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
[ -n "$S3_PID" ] && echo "  S3 flip log : $LOG_DIR/strategy3.log  (pid $S3_PID, alert-only)"
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
