"""Bybit position mode — one-way vs hedge, decided PER SYMBOL.

Bybit stores position mode per symbol, not per account, so one account can hold
SKHYNIX in hedge mode and XAUT in one-way at the same time (ours does). Every
order therefore has to carry the right `positionIdx`:

    one-way   → 0
    hedge     → 1 for a LONG position, 2 for a SHORT position

THE TRAP. In hedge mode positionIdx follows the POSITION, never the order side.
Closing a long is `side=sell, positionIdx=1`. Deriving the index from the order
side instead gives 2, and Bybit then reads a reduce-only sell as an order
against the SHORT book — so the "close" does not close anything. Every helper
here takes `pos_side` ('long'/'short') for exactly that reason; no caller
should ever pass an order side.

HOW THE MODE IS KNOWN. Bybit v5 has no per-symbol GET for it — /v5/position/list
returns both hedge-index rows as empty placeholders for any symbol without a
position, which reads as 'hedge' even for a symbol that is plainly one-way (XAUT
filled at positionIdx 0 the same afternoon its placeholder rows said 1 and 2).
The only trustworthy signals are a LIVE position's own positionIdx and what the
exchange says when an order is wrong. So:

  · a live position teaches us for free   → learn_from_idx()
  · otherwise assume what we saw last, defaulting to one-way
  · and if an order comes back "position idx not match position mode", flip the
    remembered mode, persist it and retry ONCE → send_with_mode()

That retry is the point of the module: on 2026-07-09 a hedge-mode XAUT rejected
S3's entry with 10001 and the long was simply missed. Now the same situation
costs one wasted request and the trade still goes on.

SCOPE KEYS. `symbol` here is an opaque cache key, not necessarily a market. Copy
-trading followers hold their own Bybit accounts with their own per-symbol
modes, so they must never inherit what we learned about the owner's account —
copy_engine namespaces its keys as "<uid>@<symbol>" (see scope_key). The owner's
own engines pass the plain ccxt symbol.
"""
import json
import os
import threading

ONE_WAY = "one_way"
HEDGE = "hedge"

STATE_FILE = os.path.join(os.path.dirname(__file__), "bybit_position_mode.json")

_lock = threading.Lock()
_cache = {"loaded": False, "modes": {}}


# ── remembered modes ─────────────────────────────────────────────────────────
def _load() -> dict:
    if not _cache["loaded"]:
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                _cache["modes"] = json.load(f) or {}
        except Exception:  # noqa: BLE001 — missing/corrupt = nothing learned yet
            _cache["modes"] = {}
        _cache["loaded"] = True
    return _cache["modes"]


def scope_key(account: str, symbol: str) -> str:
    """Cache key for a symbol on a NON-owner account (a copy-trading follower).
    Their position mode is their own; inheriting ours would aim orders at the
    wrong index on somebody else's money."""
    return f"{account}@{symbol}"


def mode_of(symbol: str) -> str:
    """Remembered mode for `symbol`, defaulting to one-way — which is what the
    bot's symbols actually are, so the common path stays a plain positionIdx 0
    with no extra requests."""
    with _lock:
        return _load().get(str(symbol)) or ONE_WAY


def learn(symbol: str, mode: str) -> None:
    """Remember `mode` for `symbol` and persist it, so a restart does not cost
    another rejected order to rediscover the same thing."""
    if mode not in (ONE_WAY, HEDGE):
        return
    with _lock:
        modes = _load()
        if modes.get(str(symbol)) == mode:
            return
        modes[str(symbol)] = mode
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(modes, f)
            os.replace(tmp, STATE_FILE)
        except Exception as exc:  # noqa: BLE001 — in-memory value still applies
            print(f"[bybit-mode] could not persist {symbol}={mode}: {exc}")
    print(f"[bybit-mode] {symbol} is in {mode.replace('_', '-')} mode")


def learn_from_idx(symbol: str, position_idx) -> None:
    """Teach from a LIVE position's own positionIdx — free and authoritative,
    unlike the placeholder rows /v5/position/list returns for a flat symbol."""
    try:
        i = int(position_idx)
    except (TypeError, ValueError):
        return
    if i in (1, 2):
        learn(symbol, HEDGE)
    elif i == 0:
        learn(symbol, ONE_WAY)


# ── index selection ──────────────────────────────────────────────────────────
def idx_for(mode: str, pos_side: str) -> int:
    """positionIdx for a position on `pos_side` under `mode`.

    pos_side is the side of the POSITION being opened, reduced or stopped —
    'long'/'short' (or Bybit's 'Buy'/'Sell') — never the side of the order.
    """
    if mode != HEDGE:
        return 0
    s = str(pos_side or "").strip().lower()
    return 2 if s in ("short", "sell") else 1


def idx(symbol: str, pos_side: str) -> int:
    """positionIdx to use right now for `symbol`'s `pos_side` position."""
    return idx_for(mode_of(symbol), pos_side)


# ── the mismatch retry ───────────────────────────────────────────────────────
def is_mismatch(exc) -> bool:
    """Bybit's 'the index you sent does not fit this symbol's mode' error.

    Matched on retMsg, not on retCode alone: 10001 is Bybit's generic parameter
    error and covers a great deal more than position mode, so retrying blindly
    on it would resend orders that failed for unrelated reasons.
    """
    return "position idx not match" in str(exc).lower()


def send_with_mode(symbol: str, pos_side: str, send):
    """Run `send(position_idx)`, and if Bybit says the index does not match the
    symbol's mode, flip the remembered mode, persist it and retry ONCE.

    Any other exception propagates untouched — this must never turn an
    unrelated failure into a second live order.
    """
    mode = mode_of(symbol)
    try:
        return send(idx_for(mode, pos_side))
    except Exception as exc:  # noqa: BLE001 — re-raised below unless it's ours
        if not is_mismatch(exc):
            raise
        other = HEDGE if mode == ONE_WAY else ONE_WAY
        print(f"[bybit-mode] {symbol} rejected positionIdx for {mode} — "
              f"retrying as {other}")
        learn(symbol, other)
        return send(idx_for(other, pos_side))
