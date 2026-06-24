#!/bin/bash
# Pre-flight gate: run the strategy test suite before launching the bot or web.
# If the tests are RED, abort the launch (exit non-zero) so broken strategy/cost
# math never reaches live trading.
#
# Bypass (only when you know why):  SKIP_TESTS=1 ./run_bot.sh
cd "$(dirname "$0")"

if [ "${SKIP_TESTS:-0}" = "1" ]; then
  echo "⚠  SKIP_TESTS=1 — skipping the test suite (launching unchecked)."
  exit 0
fi

echo "Pre-flight: running strategy tests…"
if /Users/wolfman/miniforge3/bin/python -m pytest -q; then
  echo "✓  tests green — continuing launch."
  exit 0
else
  echo "✗  TESTS FAILED — launch aborted. Fix the failure above, or bypass with SKIP_TESTS=1." >&2
  exit 1
fi
