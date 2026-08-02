#!/usr/bin/env bash
#
# tailscale.sh — expose the local dashboard (http://127.0.0.1:4000) on a
# PERMANENT public HTTPS URL using Tailscale Funnel.
#
#   ./tailscale.sh on       start serving (survives reboots — Tailscale
#                           restores the funnel config on its own)
#   ./tailscale.sh off      stop serving publicly
#   ./tailscale.sh url      print the permanent URL
#   ./tailscale.sh status   show what is currently exposed
#   ./tailscale.sh check    verify the URL actually answers from outside
#
# ── WHY THIS REPLACES tunnel.sh ──────────────────────────────────────────────
# The Cloudflare *quick tunnel* minted a NEW random trycloudflare.com hostname
# every time it started, so every link saved in the family's LINE/Telegram
# chats died on reboot — which is what site_link.py's whole announce-on-change
# machinery exists to paper over. Tailscale Funnel gives one fixed name tied to
# this machine, free on a personal tailnet, with a real certificate.
#
# ── ONE-TIME SETUP (a human must do this once) ───────────────────────────────
#   1. Enable Funnel for the tailnet — Tailscale prints an approval link the
#      first time you run `on`; open it and approve.
#   2. Set the URL in app/.env so the bots announce the right link:
#         PUBLIC_BASE_URL=https://<node>.<tailnet>.ts.net
#   3. Restart the stack so the scanner picks it up:  ./run_all.sh bg
#
# ── TRADE-OFFS, HONESTLY ─────────────────────────────────────────────────────
#   · The URL contains your machine + tailnet name; it is stable and public,
#     not secret. Anyone with the link reaches the login page (the only
#     no-auth pages are /tw, /us and /welcome — by design).
#   · Funnel serves ports 443/8443/10000 only; we use 443.
#   · If the Mac is asleep or Tailscale is logged out, the link is down —
#     same as any self-hosted setup.
# ─────────────────────────────────────────────────────────────────────────────

set -u
PORT="${PORT:-4000}"
TS="$(command -v tailscale || echo /usr/local/bin/tailscale)"

if [ ! -x "$TS" ]; then
    echo "ERROR: tailscale CLI not found. Install the standalone Tailscale app."
    exit 1
fi

node_url() {
    # Trailing dot on DNSName is correct DNS but wrong in a URL — strip it.
    "$TS" status --json 2>/dev/null \
        | python3 -c 'import sys,json;d=json.load(sys.stdin);n=(d.get("Self") or {}).get("DNSName","").rstrip(".");print("https://"+n if n else "")' 2>/dev/null
}

case "${1:-status}" in
on)
    URL="$(node_url)"
    echo "Exposing 127.0.0.1:$PORT  →  ${URL:-<unknown node>}"
    echo "(first run prints an approval link — open it, approve, then re-run)"
    "$TS" funnel --bg "$PORT"
    echo
    "$TS" funnel status
    [ -n "$URL" ] && {
        echo
        echo "Put this in app/.env, then ./run_all.sh bg :"
        echo "    PUBLIC_BASE_URL=$URL"
    }
    ;;
off)
    "$TS" funnel --https=443 off 2>/dev/null || "$TS" funnel off
    echo "Funnel off — the public URL no longer serves."
    ;;
url)
    URL="$(node_url)"
    [ -n "$URL" ] && echo "$URL" || { echo "Could not read the node name (is tailscale logged in?)"; exit 1; }
    ;;
check)
    URL="$(node_url)"
    if [ -z "$URL" ]; then echo "No node URL — is tailscale logged in?"; exit 1; fi
    echo "Local  127.0.0.1:$PORT  → $(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/login" || echo down)"
    echo "Public $URL → $(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$URL/login" || echo unreachable)"
    ;;
status)
    "$TS" funnel status
    echo
    echo "node: $(node_url)"
    ;;
*)
    echo "Usage: $0 [on|off|url|check|status]"; exit 1 ;;
esac
