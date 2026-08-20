#!/usr/bin/env bash
#
# restart.sh — run ./run_all.sh bg on behalf of a process that is about to be
# killed by it, then report the outcome to Telegram.
#
# Not meant to be typed by hand (though it works): it exists because
# run_all.sh pkills every python process it knows, including whichever one
# asked for the restart. This script is bash, so no pkill pattern in
# run_all.sh matches it — it outlives the stack it replaces and is therefore
# the only thing left able to say whether the restart worked.
#
# The failure case is the one worth having this for: preflight (lint + tests)
# runs BEFORE anything is killed, so a red suite aborts the launch with the
# old stack still running and untouched. That is a very different message from
# "your bot is down", and only this script is alive to send it.
#
#   ./restart.sh [reason]
#
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$DIR/app"
PYTHON="/Users/wolfman/miniforge3/bin/python"
REASON="${1:-manual}"
LOCK="$APP/.restart.lock"

cd "$DIR" || exit 1

# ── Read .env from the FILE, not from an inherited snapshot ────────────────
# config.py does os.environ.setdefault() for every .env key, so any python
# process that imports it carries the whole file in its environment — and
# passes it to everything it spawns. This script is spawned BY such a process,
# so without the strip below, run_all.sh and every engine it launches inherit
# the .env of whenever that chain started, and later edits are ignored forever.
#
# On 2026-08-20 that meant disarming live trading in .env left the running
# stack with LIVE_TRADING=true, and a /restart would have relaunched it ARMED.
# The pre-flight tests caught it and aborted the launch, which is the only
# reason it was noticed.
#
# PROTECTED names are never unset: .env should not contain them, but unsetting
# PATH here would take the interpreter out with it.
PROTECTED=" PATH HOME SHELL USER LOGNAME LANG TERM PWD TMPDIR "
if [ -f "$APP/.env" ]; then
    while IFS= read -r _line || [ -n "$_line" ]; do
        case "$_line" in ""|"#"*) continue ;; esac
        case "$_line" in *"="*) : ;; *) continue ;; esac
        _key="${_line%%=*}"
        _key="$(printf '%s' "$_key" | tr -d "[:space:]")"
        [ -z "$_key" ] && continue
        case "$PROTECTED" in *" $_key "*) continue ;; esac
        unset "$_key" 2>/dev/null || true
    done < "$APP/.env"
fi

# One at a time. mkdir is atomic on every filesystem this runs on, so two
# simultaneous taps cannot both get past here and race two run_all.sh.
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "[restart] another restart is already running — skipping ($REASON)"
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

echo "[restart] $(date "+%Y-%m-%d %H:%M:%S") starting — reason: $REASON"
./run_all.sh bg
RC=$?
echo "[restart] run_all.sh exited rc=$RC"

# Give the freshly launched processes a moment to appear in `ps` before the
# report counts them, or a good restart reports itself as half-started.
[ "$RC" -eq 0 ] && sleep 5

cd "$APP" && "$PYTHON" restart_ctl.py finish "$RC"
exit "$RC"
