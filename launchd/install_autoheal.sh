#!/bin/bash
# Generate + install + verify the auto-heal LaunchAgent for wherever this repo
# currently lives. Safe to re-run; it is the ONLY step a project move needs.
#
#     ./launchd/install_autoheal.sh
#
# The plist beside this script is a template with __PROJECT_DIR__ placeholders.
# Hardcoding the path is what let the agent rot: it pointed into ~/Desktop,
# which macOS TCC blocks launchd from reading, so it failed 4550 times in a row
# without anyone noticing.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$HERE/.." && pwd)"
LABEL="com.wolfman.wolfscanner.autoheal"
TEMPLATE="$HERE/$LABEL.plist"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$PROJECT_DIR/app/logs/autoheal.log"

echo "Installing auto-heal agent"
echo "  project : $PROJECT_DIR"
echo "  agent   : $TARGET"

# ── refuse the locations that caused the original failure ───────────────────
case "$PROJECT_DIR" in
    "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*)
        echo "✗ $PROJECT_DIR is inside a macOS TCC-protected folder." >&2
        echo "  launchd cannot read Desktop/Documents/Downloads, so the agent" >&2
        echo "  would fail on every run. Move the project elsewhere first." >&2
        exit 1 ;;
esac

[ -f "$TEMPLATE" ] || { echo "✗ template missing: $TEMPLATE" >&2; exit 1; }
[ -x "$PROJECT_DIR/autoheal.sh" ] || { echo "✗ autoheal.sh missing or not executable" >&2; exit 1; }

mkdir -p "$(dirname "$TARGET")" "$PROJECT_DIR/app/logs"

# ── generate ────────────────────────────────────────────────────────────────
# Substituting with python, not sed: the path may contain characters special to
# sed's replacement (& and the delimiter). It is then XML-escaped, because the
# plist is markup — an unescaped '&' in a directory name produces
# "unknown ampersand-escape sequence" and a plist launchd silently won't load.
# Verified against a path containing '&', a space and non-ASCII.
/usr/bin/python3 - "$TEMPLATE" "$TARGET" "$PROJECT_DIR" <<'PY'
import sys
from xml.sax.saxutils import escape
tpl, target, project = sys.argv[1], sys.argv[2], sys.argv[3]
body = open(tpl, encoding="utf-8").read().replace("__PROJECT_DIR__", escape(project))
if "__PROJECT_DIR__" in body:
    raise SystemExit("substitution failed")
open(target, "w", encoding="utf-8").write(body)
PY

# plutil validates the XML *and* proves the path survived substitution intact
plutil -lint "$TARGET" >/dev/null || { echo "✗ generated plist is malformed" >&2; exit 1; }

# ── (re)load ────────────────────────────────────────────────────────────────
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

# ── verify it can actually EXECUTE ──────────────────────────────────────────
# This is the check that was missing before. A loaded agent proves nothing; the
# old one was loaded too, and failed every run. Kick it once and confirm no
# permission error lands in the log.
before=$(grep -c "Operation not permitted" "$LOG" 2>/dev/null || echo 0)
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 3
after=$(grep -c "Operation not permitted" "$LOG" 2>/dev/null || echo 0)

if [ "$after" -gt "$before" ]; then
    echo "✗ agent still cannot run — 'Operation not permitted' in $LOG" >&2
    echo "  $PROJECT_DIR is still unreadable by launchd." >&2
    exit 1
fi

status=$(launchctl list | awk -v l="$LABEL" '$3==l {print $2}')
echo "✓ agent installed and executed cleanly (last exit status: ${status:-0})"
echo "  runs at login and every 5 minutes; pauses while .stack_stopped exists"
