#!/bin/bash
# Auto-heal — the stack restarts ITSELF after a crash or reboot.
#
# Installed as a launchd agent (launchd/com.wolfman.wolfscanner.autoheal.plist
# → ~/Library/LaunchAgents): runs at login and every 5 minutes. If any of the
# four stack processes is missing it relaunches EVERYTHING via ./run_all.sh bg
# (which handles locks, cleanup and the pre-flight test gate) and says so on
# Telegram. A deliberate './run_all.sh stop' writes .stack_stopped, which
# makes this script a no-op until the next deliberate './run_all.sh bg'.
#
# The watchdog module BARKS about a dead process; this script HEALS it.
# If pre-flight tests are red the relaunch aborts (correct: never launch
# broken code) and the ❌ Telegram note asks for a human.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

[ -f .stack_stopped ] && exit 0

# Detection reads ps, NOT pgrep. run_all.sh documents pgrep answering "not
# running" for a demonstrably live process on this machine, always in the same
# direction — it reports absence, never a phantom. Here that direction is the
# expensive one: a false "down" restarts a HEALTHY live trading stack, killing
# an engine mid-cycle for nothing. ps walks the process table directly and has
# not been caught doing it, so both scripts now use the same source of truth.
REQUIRED=("app.py" "bot.py" "strategy2_scanner.py" "strategy3_scanner.py")
MISSING=()
for p in "${REQUIRED[@]}"; do
    ps -Ao command= | grep -qE "[p]ython.*$p" || MISSING+=("$p")
done
[ ${#MISSING[@]} -eq 0 ] && exit 0

echo "[$(date '+%F %T')] auto-heal: ${MISSING[*]} down — relaunching stack"

TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' app/.env 2>/dev/null | cut -d= -f2)
CHAT=$(grep '^TELEGRAM_GROUP_CHAT_ID=' app/.env 2>/dev/null | cut -d= -f2)
THREAD=$(grep '^TELEGRAM_ALERTS_THREAD_ID=' app/.env 2>/dev/null | cut -d= -f2)
notify() {
    [ -n "$TOKEN" ] && [ -n "$CHAT" ] || return 0
    curl -s --max-time 10 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d "chat_id=${CHAT}" ${THREAD:+-d "message_thread_id=${THREAD}"} \
        --data-urlencode "text=$1" >/dev/null
}

notify "🩺 auto-heal: ${MISSING[*]} 停止運行 — 自動重啟整個 stack"
if ./run_all.sh bg; then
    notify "✅ auto-heal: stack 已重啟完成"
else
    notify "❌ auto-heal: 重啟失敗 (pre-flight tests red?) — 需要人工處理"
    exit 1
fi
