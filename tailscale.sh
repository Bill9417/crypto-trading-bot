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
#   ./tailscale.sh doctor   check the prerequisites Funnel silently blocks on
#
# ── WHY THIS REPLACES tunnel.sh ──────────────────────────────────────────────
# The Cloudflare *quick tunnel* minted a NEW random trycloudflare.com hostname
# every time it started, so every link saved in the family's LINE/Telegram
# chats died on reboot — which is what site_link.py's whole announce-on-change
# machinery exists to paper over. Tailscale Funnel gives one fixed name tied to
# this machine, free on a personal tailnet, with a real certificate.
#
# ── ONE-TIME SETUP (a human must do this once) ───────────────────────────────
#   Run `./tailscale.sh doctor` — it names whichever step is missing. Sign in
#   to the admin console as the account it prints; a different identity 404s
#   on the node-specific links.
#   1. HTTPS certificates: login.tailscale.com/admin/dns → HTTPS Certificates
#      → Enable. Without this Funnel cannot issue a cert and just blocks.
#   2. Funnel in the policy: login.tailscale.com/admin/acls → Funnel →
#      "Add Funnel to policy"  (nodeAttrs → attr: ["funnel"])
#   3. ./tailscale.sh on
#   4. Set the URL in app/.env so the bots announce the right link:
#         PUBLIC_BASE_URL=https://<node>.<tailnet>.ts.net
#   5. Restart the stack so the scanner picks it up:  ./run_all.sh bg
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

# `tailscale funnel` BLOCKS on a browser approval when a prerequisite is
# missing, which looks like a hang. Check them up front and say which one.
doctor() {
    # NOTE: the status JSON goes via a temp file, not a pipe — the heredoc
    # below already claims stdin, so a pipe would be swallowed and python
    # would see an empty document.
    _tsj="$(mktemp -t tsstatus)"
    "$TS" status --json > "$_tsj" 2>/dev/null
    python3 - "$_tsj" "$(node_url)" <<'PY'
import json, sys
url = sys.argv[2] if len(sys.argv) > 2 else ""
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
except Exception:
    print("✗ tailscale is not running or not logged in — open the Tailscale app.")
    raise SystemExit(1)

self_ = d.get("Self") or {}
users = d.get("User") or {}
who = (users.get(str(self_.get("UserID"))) or {}).get("LoginName", "?")
ok = True

print(f"  tailnet account : {who}")
print(f"  node URL        : {url or '(unknown)'}")

if d.get("BackendState") != "Running":
    print(f"✗ backend state is {d.get('BackendState')!r}, expected Running"); ok = False

if not d.get("MagicDNSSuffix"):
    print("✗ MagicDNS is off — enable it at https://login.tailscale.com/admin/dns"); ok = False

if not d.get("CertDomains"):
    tnet = d.get("MagicDNSSuffix") or "?"
    print("✗ HTTPS certificates are NOT enabled — Funnel cannot work without them.")
    print("    Fix: https://login.tailscale.com/admin/dns → HTTPS Certificates → Enable")
    print(f"    The DNS page MUST show tailnet '{tnet}'. If it shows a different")
    print("    name, or no machines, the browser is in the WRONG tailnet and the")
    print("    toggle you flipped applied to an empty one.")
    print("    Ground truth:  tailscale cert " + (self_.get("DNSName","").rstrip(".") or "<node>"))
    print("      → '...does not support getting TLS certs' means still not enabled.")
    ok = False

if ok:
    print("✓ prerequisites look right (Funnel still needs the 'funnel' node attribute)")
    print("    If `on` hangs, add it at https://login.tailscale.com/admin/acls")
    print("    → Funnel section → \"Add Funnel to policy\"")
else:
    print()
    print("  Sign in to the admin console as the SAME account shown above")
    print(f"  ({who}) — signing in as a different identity 404s on the node links.")
raise SystemExit(0 if ok else 1)
PY
    _rc=$?
    rm -f "$_tsj"
    return $_rc
}

case "${1:-status}" in
on)
    URL="$(node_url)"
    echo "Checking prerequisites…"
    if ! doctor; then
        echo
        echo "Not starting — fix the ✗ above first, then re-run:  $0 on"
        exit 1
    fi
    echo
    echo "Exposing 127.0.0.1:$PORT  →  ${URL:-<unknown node>}"
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
    HOST="${URL#https://}"
    echo "Local   127.0.0.1:$PORT  → $(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/login" || echo down)"
    # A plain curl from this machine resolves the ts.net name over the TAILNET
    # and connects to 100.x — it answers 200 even when Funnel is completely
    # dead to the outside world. That false green is exactly what hid a broken
    # public path while a phone off the tailnet could not load the site. So
    # force each PUBLIC ingress IP and report them separately.
    IPS="$(dig +short @8.8.8.8 "$HOST" A 2>/dev/null | grep -E '^[0-9.]+$')"
    if [ -z "$IPS" ]; then
        echo "Public  no A record in public DNS — Funnel is not published."
        exit 1
    fi
    # EVERY published relay must serve, not just one. Public DNS hands the
    # client all the A records and it picks whichever it likes, so one dead
    # ingress is a coin-flip outage for real visitors — not a spare tyre.
    # Treating "any relay answers" as healthy printed a green ✓ on 2026-08-05
    # while a phone off the tailnet got "cannot establish a secure connection"
    # about half the time, which is exactly how the fault stayed invisible.
    good=0; bad=0
    for ip in $IPS; do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
                --resolve "$HOST:443:$ip" "$URL/login" 2>/dev/null)"
        if [ "$code" = "200" ]; then
            good=$((good + 1))
            echo "Public  via $ip → $code"
        else
            bad=$((bad + 1))
            echo "Public  via $ip → ${code:-fail}   <-- this relay is not serving"
        fi
    done
    echo
    if [ "$bad" = "0" ]; then
        echo "✓ reachable from the public internet on all $good relays."
    elif [ "$good" != "0" ]; then
        echo "⚠ PARTIAL — $good of $((good + bad)) relays serve. The browser picks one"
        echo "  at random, so roughly $((100 * bad / (good + bad)))% of visits fail with a TLS error."
        echo "  Re-arm the registration:"
        echo "      $0 rearm"
        exit 1
    else
        echo "✗ NOT reachable publicly. The tailnet path may still work, which is"
        echo "  why a browser on this Mac looks fine. Re-arm the registration:"
        echo "      $0 rearm"
        exit 1
    fi
    ;;
doctor)
    doctor ;;
status)
    OUT="$("$TS" funnel status 2>&1)"
    echo "$OUT"
    echo
    echo "node: $(node_url)"
    # "No serve config" on its own tells you nothing about WHY, so when
    # nothing is being served, say what is still missing.
    case "$OUT" in
        *"No serve config"*)
            echo
            echo "Nothing is being served publicly yet. Checking why:"
            echo
            doctor || true
            ;;
    esac
    ;;
rearm)
    # ONE command, because the two-step remedy is a footgun: on 2026-08-05 the
    # printed "$0 rearm" was pasted as "$0 off && on", the second half
    # was not a command, and the public URL went from intermittently down to
    # fully down — LINE's webhook and every public page with it. A single
    # command cannot be half-executed.
    #
    # `funnel off` + `funnel on` is NOT enough. On 2026-08-05 it left both
    # relays dead while `funnel status` cheerfully reported "Funnel on" — the
    # node kept a stale ingress registration that toggling the flag never
    # cleared. `serve reset` drops the whole serve config and forces a fresh
    # registration, and that is what actually brought a relay back.
    # One at a time. BOTH scanners run the watchdog, and on 2026-08-05 they
    # both detected the same outage in the same five-minute window and both
    # fired this — two `serve reset` racing each other, where one can wipe the
    # config the other just armed. mkdir is atomic; a second caller exits.
    LOCK="${TMPDIR:-/tmp}/wolf-tailscale-rearm.lock"
    if ! mkdir "$LOCK" 2>/dev/null; then
        echo "Another rearm is already running — skipping."
        exit 0
    fi
    trap 'rmdir "$LOCK" 2>/dev/null' EXIT

    # STAMP THE LINE. Until 2026-08-13 this log had no timestamps at all: 21
    # re-arms recorded and no way to tell whether they were 21 in one bad hour
    # or 21 across a month. "Why does this keep happening" is not answerable
    # without a timeline, and the answer changes the fix — a nightly lapse is a
    # renewal problem, a burst is a network problem.
    echo "── $(date '+%Y-%m-%d %H:%M:%S %Z') · re-arming (${REARM_WHY:-watchdog}) ──"
    echo "Re-arming the Funnel registration…"
    "$TS" funnel --https="$PORT" off >/dev/null 2>&1 || true
    "$TS" serve reset >/dev/null 2>&1 || true
    sleep 2
    "$TS" funnel --bg "$PORT"
    # Relays pick the new registration up at their own pace and not together —
    # one can serve a full minute before the other, so this waits for ALL of
    # them rather than stopping at the first green.
    echo "Waiting for the relays to pick it up…"
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        sleep 5
        if "$0" check >/dev/null 2>&1; then break; fi
    done
    echo
    "$0" check
    ;;
*)
    echo "Usage: $0 [on|off|rearm|url|check|status|doctor]"; exit 1 ;;
esac
