#!/bin/bash
# sync.sh — one-command GitHub sync.
#
#   ./sync.sh              show local changes + how far ahead/behind GitHub you are
#   ./sync.sh pull         get the latest from GitHub (your local edits are auto-stashed)
#   ./sync.sh push         commit ALL local changes and push (message = timestamp)
#   ./sync.sh push "msg"   same, with your own commit message
set -e
cd "$(dirname "$0")"

BRANCH=$(git rev-parse --abbrev-ref HEAD)

pull_latest() {
    echo "⬇  pulling origin/$BRANCH …"
    if ! git pull --rebase --autostash origin "$BRANCH"; then
        echo ""
        echo "⚠  Conflict — your edits clash with GitHub."
        echo "   Fix the files git listed, then:  git rebase --continue"
        echo "   Or give up on the pull with:     git rebase --abort"
        exit 1
    fi
}

case "${1:-status}" in
    pull)
        pull_latest
        echo "✓ up to date."
        ;;

    push)
        shift
        MSG="${*:-sync $(date '+%Y-%m-%d %H:%M')}"
        git add -A
        if git diff --cached --quiet; then
            echo "nothing new to commit."
        else
            git commit -m "$MSG"
            echo "✓ committed: $MSG"
        fi
        pull_latest                       # integrate GitHub first so the push can't be rejected
        echo "⬆  pushing to origin/$BRANCH …"
        git push origin "$BRANCH"
        echo "✓ pushed."
        ;;

    *)
        git fetch origin "$BRANCH" --quiet || true
        echo "── local changes ─────────────────────────────"
        git status --short
        [ -z "$(git status --short)" ] && echo "(none)"
        AHEAD=$(git rev-list --count "origin/$BRANCH..HEAD" 2>/dev/null || echo '?')
        BEHIND=$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo '?')
        echo "── vs GitHub ─────────────────────────────────"
        echo "commits to push: $AHEAD · commits to pull: $BEHIND"
        echo ""
        echo "usage: ./sync.sh pull   |   ./sync.sh push [\"message\"]"
        ;;
esac
