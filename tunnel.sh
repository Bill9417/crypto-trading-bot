#!/usr/bin/env bash
#
# tunnel.sh — manage the Cloudflare tunnel that exposes the local dashboard
# (http://127.0.0.1:4000) to the public internet.
#
#   ./tunnel.sh start     start the tunnel in the BACKGROUND (survives closing
#                         the terminal / Ctrl+C). Prints the public URL.
#   ./tunnel.sh url       print the current public link (and check it's reachable)
#       show whether the tunnel is running + its URL
#   ./tunnel.sh stop      stop the tunnel
#   ./tunnel.sh restart   stop, then start again (NOTE: gives a NEW random URL)
#  
# ── IMPORTANT about the URL ──────────────────────────────────────────────────
# This uses a Cloudflare *quick tunnel* (`--url`). Cloudflare hands out a NEW
# random https://<words>.trycloudflare.com address EVERY time the tunnel starts.
# Your current link, https://star-liable-guided-tracking.trycloudflare.com,
# stays alive ONLY while the one process started on Jun 28 keeps running. If you
# stop it (or reboot the Mac), the next start gives a DIFFERENT URL — there is
# no way to reclaim that exact name with a quick tunnel.
#
# So:  • to keep star-liable: DON'T stop the tunnel (this script's `start` is for
#        bringing one up fresh, e.g. after a reboot — it will be a new URL).
#      • to find whatever the current URL is, run:  ./tunnel.sh url
#      • to get a PERMANENT URL that survives reboots, you need a *named* tunnel
#        with your own domain (one-time `cloudflared tunnel login`). Ask and I'll
#        wire that up.
# ─────────────────────────────────────────────────────────────────────────────

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/cloudflared.log"
PORT="${PORT:-4000}"
PATTERN="[c]loudflared tunnel --url http://localhost:$PORT"

# The current public URL = the most recent trycloudflare address in the log.
current_url() {
    grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -1
}

is_running() { pgrep -f "$PATTERN" >/dev/null 2>&1; }

pids() { pgrep -f "$PATTERN" | tr '\n' ' '; }

check_reachable() {
    local u="$1" code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 "$u/" 2>/dev/null)"
    if [ "$code" = "000" ] || [ -z "$code" ]; then
        echo "   (⚠ not reachable yet — give it a few seconds, or check the web app on :$PORT)"
    else
        echo "   (✅ reachable — HTTP $code)"
    fi
}

cmd_start() {
    if is_running; then
        echo "Tunnel already running (pid $(pids))."
        echo "Public URL: $(current_url)"
        return 0
    fi
    if ! command -v cloudflared >/dev/null 2>&1; then
        echo "ERROR: cloudflared not installed.  Install with:  brew install cloudflared"
        exit 1
    fi
    echo "Starting Cloudflare tunnel → http://localhost:$PORT  (background, survives terminal close)…"
    : > "$LOG"                       # fresh log so the new URL is unambiguous
    nohup cloudflared tunnel --url "http://localhost:$PORT" >> "$LOG" 2>&1 & disown
    # wait up to ~20s for Cloudflare to assign the URL
    local url=""
    for _ in $(seq 1 40); do
        url="$(current_url)"
        [ -n "$url" ] && break
        sleep 0.5
    done
    if [ -n "$url" ]; then
        echo ""
        echo "✅ Tunnel is up (running in the background)."
        echo "   Public URL:  $url"
        check_reachable "$url"
        echo "   Stop it with:  $0 stop"
    else
        echo "⚠️  Tunnel started but no URL appeared yet. Watch it:  tail -f $LOG"
    fi
}

cmd_url() {
    local u; u="$(current_url)"
    if [ -z "$u" ]; then
        echo "No tunnel URL found. Is it running?  →  $0 start"
        return 1
    fi
    echo "$u"
    check_reachable "$u"
}

cmd_status() {
    if is_running; then
        echo "Tunnel: RUNNING (pid $(pids))"
        echo "URL   : $(current_url)"
    else
        echo "Tunnel: stopped"
        local u; u="$(current_url)"
        [ -n "$u" ] && echo "(last URL seen: $u — now offline)"
    fi
}

cmd_stop() {
    if ! is_running; then echo "Tunnel not running."; return 0; fi
    echo "Stopping tunnel (pid $(pids))…"
    pkill -f "$PATTERN" 2>/dev/null && echo "Stopped." || echo "Nothing to stop."
}

case "${1:-status}" in
    start)            cmd_start  ;;
    url|link|check)   cmd_url    ;;
    status)           cmd_status ;;
    stop)             cmd_stop   ;;
    restart)          cmd_stop; sleep 1; cmd_start ;;
    *) echo "Usage: $0 [start|url|status|stop|restart]"; exit 1 ;;
esac
