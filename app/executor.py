"""
Binance Futures (USD-M) order execution layer.

SAFE BY DEFAULT
---------------
While config.LIVE_TRADING is False, this module is in DRY-RUN: every call logs
the exact order it WOULD send (and posts a Telegram note) but transmits NOTHING
to Binance. No keys are required to run in dry-run.

Go live only after:
  1. Adding BINANCE_API_KEY / BINANCE_API_SECRET to .env
  2. Setting LIVE_TRADING=true   (keep USE_TESTNET=true the first time)

The bot's existing dashboard loop still simulates/tracks TP & SL regardless of
mode, so the UI keeps working in dry-run.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import ccxt

from config import (
    LIVE_TRADING,
    USE_TESTNET,
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    LEVERAGE,
    MARGIN_MODE,
    FIXED_MARGIN_USDT,
    MAX_MARGIN_USDT,
    MAX_CONCURRENT_POSITIONS,
    PLACE_BRACKET_ORDERS,
    USE_RESTING_ORDERS,
    USE_POST_ONLY_ENTRY,
    LIVE_PARTIAL_TP,
    LIVE_TP_TARGET,
    LIVE_TRAIL_TO_BREAKEVEN,
    BINANCE_TESTNET_HOST,
    POSITION_SIZE_4_LIGHTS,
    POSITION_SIZE_5_LIGHTS,
    POSITION_SIZE_6_LIGHTS,
    POSITION_SIZE_COUNTER_TREND,
    ENABLE_RISK_SIZING,
    RISK_PCT_PER_TRADE,
)
from telegram_utils import send_message

_exchange = None
_exchange_lock = threading.Lock()
_markets_loaded = False

# Tracks resting bracket orders the bot has placed, keyed by (symbol, direction).
# Lets us stay idempotent across scans and cancel cleanly when a setup dies.
_resting: dict = {}
_resting_lock = threading.Lock()

# Tracks brackets for FILLED positions (SL + partial TP order ids), so leftover
# orders can be cleaned up when the trade finally closes.
_active_brackets: dict = {}
_active_lock = threading.Lock()

# Safety halt: when a startup check (e.g. wrong position mode) fails we flip this
# on so is_live() returns False — the bot keeps scanning but logs orders as
# dry-run instead of sending orders the exchange would mishandle.
_safety_halt = False
_safety_halt_reason = ""


def _keys_present() -> bool:
    return bool(BINANCE_API_KEY) and bool(BINANCE_API_SECRET)


def is_live() -> bool:
    """True only when live trading is switched on, keys exist, and no safety
    halt is active."""
    return bool(LIVE_TRADING) and _keys_present() and not _safety_halt


def halt_live_trading(reason: str) -> None:
    """Force the executor into dry-run for the rest of the process. Used by the
    startup safety gate when the account isn't configured for the bot's orders."""
    global _safety_halt, _safety_halt_reason
    _safety_halt = True
    _safety_halt_reason = reason
    print(f"[executor] LIVE TRADING HALTED — {reason}")


def verify_exchange_access() -> tuple[bool, str]:
    """When live, make one authenticated call against the CONFIGURED host to
    confirm the API keys actually work there.

    This catches the most common testnet misconfig: keys minted on the wrong
    site. There is no field in a key that reveals its origin, so the only
    reliable check is to authenticate against the host we're routing to:
      • demo-fapi.binance.com  (Demo Trading) only accepts keys from demo.binance.com
      • testnet.binancefuture.com (classic Futures Testnet) only accepts keys
        from testnet.binancefuture.com
    Mismatched keys fail authentication here, before any order is ever sent."""
    if not is_live():
        return True, "dry-run — exchange access check skipped"
    net = "TESTNET" if USE_TESTNET else "MAINNET"
    host = BINANCE_TESTNET_HOST if USE_TESTNET else "fapi.binance.com"
    try:
        ex = _get_exchange()
        bal = ex.fetch_balance()
        usdt = (bal.get("USDT") or {}).get("total")
        bal_txt = f" | USDT balance: {usdt}" if usdt is not None else ""
        return True, f"Binance {net} auth OK on {host}{bal_txt}"
    except ccxt.AuthenticationError as exc:  # keys rejected by this host
        if USE_TESTNET:
            hint = (
                f"the API keys do not match the configured testnet host ({host}). "
                "For demo-fapi.binance.com use keys minted at demo.binance.com "
                "(Demo Trading); for testnet.binancefuture.com use keys from "
                "testnet.binancefuture.com. Set BINANCE_TESTNET_HOST to match where "
                "your keys came from, or regenerate keys on the matching site."
            )
        else:
            hint = (
                "the mainnet API key was rejected — confirm it is valid, has "
                "Futures trading enabled, and that this machine's IP is on the "
                "key's allow-list."
            )
        return False, f"Binance {net} authentication FAILED — {hint} ({exc})"
    except Exception as exc:  # noqa: BLE001 — network / endpoint / permission
        return False, f"Could not reach Binance {net} on {host}: {exc}"


def verify_position_mode() -> tuple[bool, str]:
    """When live, confirm the Binance Futures account is in ONE-WAY position
    mode. The bracket orders use reduceOnly / closePosition, which Binance only
    honours in one-way mode; in hedge mode they are rejected, which would leave a
    filled entry with no working stop. Returns (ok, human-readable message)."""
    if not is_live():
        return True, "dry-run — position mode check skipped"
    try:
        ex = _get_exchange()
        res = ex.fapiPrivateGetPositionSideDual()
        # ccxt returns {"dualSidePosition": true} for hedge mode; the value may
        # come back as a bool or the string "true"/"false" depending on version.
        is_hedge = str(res.get("dualSidePosition")).strip().lower() in ("true", "1")
        if is_hedge:
            return False, (
                "Binance account is in HEDGE position mode — the bot's "
                "reduceOnly/closePosition bracket orders require ONE-WAY mode. "
                "Fix in Binance → Futures → Preferences → Position Mode, then restart."
            )
        return True, "Binance position mode: One-Way (OK)"
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not verify Binance position mode: {exc}"


def concurrent_commitments() -> int:
    """How many positions are committed right now: resting brackets already on
    the exchange (queued, not yet filled) + filled positions still being
    managed. Tracked identically in dry-run so the cap is observable there too."""
    with _resting_lock:
        resting = len(_resting)
    with _active_lock:
        active = len(_active_brackets)
    return resting + active


def has_capacity() -> bool:
    """True if a NEW position can still be opened under MAX_CONCURRENT_POSITIONS.
    0 or negative = unlimited."""
    if MAX_CONCURRENT_POSITIONS <= 0:
        return True
    return concurrent_commitments() < MAX_CONCURRENT_POSITIONS


def uses_resting_orders() -> bool:
    return bool(USE_RESTING_ORDERS)


def status_line() -> str:
    model = "resting limit+bracket at queue time" if USE_RESTING_ORDERS else "market-on-touch"
    if not LIVE_TRADING:
        return f"Execution: DRY-RUN (LIVE_TRADING=false) — no orders will be sent. Model: {model}."
    if not _keys_present():
        return f"Execution: DRY-RUN (LIVE_TRADING=true but API keys missing) — no orders will be sent. Model: {model}."
    if _safety_halt:
        return f"Execution: DRY-RUN (SAFETY HALT: {_safety_halt_reason}) — no orders will be sent. Model: {model}."
    net = "TESTNET" if USE_TESTNET else "LIVE MAINNET"
    cap = MAX_CONCURRENT_POSITIONS if MAX_CONCURRENT_POSITIONS > 0 else "unlimited"
    return (
        f"Execution: LIVE on {net} | {model} | leverage {LEVERAGE}x {MARGIN_MODE} | "
        f"margin {FIXED_MARGIN_USDT} USDT/trade (max {MAX_MARGIN_USDT}) | max {cap} concurrent"
    )


def _get_exchange():
    """Lazily build an authenticated ccxt USD-M futures client."""
    global _exchange, _markets_loaded
    if _exchange is not None:
        return _exchange
    with _exchange_lock:
        if _exchange is not None:
            return _exchange
        ex = ccxt.binance({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_API_SECRET,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        if USE_TESTNET:
            # set_sandbox_mode is deprecated for Binance futures — point the
            # USD-M (fapi) endpoints at the demo/testnet host manually instead.
            for k, v in list(ex.urls["api"].items()):
                if isinstance(v, str) and "fapi.binance.com" in v:
                    ex.urls["api"][k] = v.replace("fapi.binance.com", BINANCE_TESTNET_HOST)
            print(f"[executor] Futures endpoints routed to demo host: {BINANCE_TESTNET_HOST}")
        try:
            ex.load_markets()
            _markets_loaded = True
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] Failed to load markets: {exc}")
        _exchange = ex
        return _exchange


def position_scale(lights_count: int, aligned: bool) -> float:
    """Conviction-based size multiplier applied on top of the fixed margin."""
    if not aligned:
        return POSITION_SIZE_COUNTER_TREND
    if lights_count >= 6:
        return POSITION_SIZE_6_LIGHTS
    if lights_count >= 5:
        return POSITION_SIZE_5_LIGHTS
    return POSITION_SIZE_4_LIGHTS


def _margin_for(lights_count: int, aligned: bool) -> float:
    margin = FIXED_MARGIN_USDT * position_scale(lights_count, aligned)
    return max(0.0, min(margin, MAX_MARGIN_USDT))


def risk_based_margin(entry, sl) -> float | None:
    """Margin (USDT) sized so the loss AT the stop equals RISK_PCT_PER_TRADE of
    account equity, given the entry/stop distance and current leverage:

        risk$  = equity * RISK_PCT_PER_TRADE
        notional = risk$ / (|entry-sl| / entry)      # loss at SL == risk$
        margin   = notional / LEVERAGE

    Returns None (→ caller falls back to the fixed margin) when risk sizing is
    OFF, the account can't be read, or the inputs are unusable. Clamped to
    MAX_MARGIN_USDT and the available balance so it can never over-commit; if the
    result is below the exchange min-order floor the order is skipped downstream.
    """
    if not ENABLE_RISK_SIZING or not entry or not sl:
        return None
    snap = account_snapshot()
    if not snap.get("ok"):
        return None
    bal = snap.get("balance") or {}
    equity = bal.get("margin_balance") or bal.get("wallet")
    if not equity or equity <= 0:
        return None
    stop_frac = abs(entry - sl) / entry
    if stop_frac <= 0:
        return None
    notional = (equity * RISK_PCT_PER_TRADE) / stop_frac
    margin = notional / LEVERAGE if LEVERAGE else notional
    margin = min(margin, MAX_MARGIN_USDT)
    avail = bal.get("available")
    if avail and margin > avail:
        margin = avail
    return max(0.0, margin)


def _round_amount(symbol: str, amount: float) -> float:
    if is_live() and _markets_loaded:
        try:
            return float(_get_exchange().amount_to_precision(symbol, amount))
        except Exception:  # noqa: BLE001
            pass
    return amount


def _round_price(symbol: str, price: float) -> float:
    if is_live() and _markets_loaded:
        try:
            return float(_get_exchange().price_to_precision(symbol, price))
        except Exception:  # noqa: BLE001
            pass
    return price


# Binance USDⓈ-M perpetuals have a ~5 USDT minimum order value even when the
# market metadata omits the cost limit. Used as a floor for the check below.
_MIN_NOTIONAL_FLOOR_USDT = 5.0


def below_min_order_size(symbol: str, amount: float, price: float) -> tuple[bool, str]:
    """True when an order would fall under the exchange's minimum quantity or
    notional, so we can skip it cleanly instead of eating a -4164/-1013 rejection.
    On a small account this is common for high-priced coins (e.g. BTC) whose
    minimum lot is worth more than the whole per-trade margin. No-op in dry-run."""
    if not (is_live() and _markets_loaded):
        return False, ""
    amount = amount or 0.0
    price = price or 0.0
    notional = amount * price
    try:
        limits = _get_exchange().market(symbol).get("limits", {})
        min_amount = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
    except Exception:  # noqa: BLE001
        min_amount = min_cost = None
    if min_amount is not None and amount < min_amount:
        return True, f"qty {amount} < market min {min_amount}"
    floor = max(min_cost or 0.0, _MIN_NOTIONAL_FLOOR_USDT)
    if notional < floor:
        return True, f"notional {notional:.2f} USDT < min {floor:.2f} USDT"
    return False, ""


def _place_stop_with_retry(ex, symbol, close_side, amount, stop_price, *, attempts=4):
    """Place the reduceOnly STOP_MARKET that protects the whole position, retrying
    a few times with a short backoff. A stop can be transiently rejected right
    after the entry fills (e.g. -2022 ReduceOnly while the position propagates, or
    a rate-limit / network blip); retrying turns those into a placed stop instead
    of a naked position. Returns the order dict, or re-raises the last error if
    every attempt failed."""
    last_exc = None
    for i in range(attempts):
        try:
            return ex.create_order(
                symbol, "STOP_MARKET", close_side, amount, None,
                {"stopPrice": stop_price, "reduceOnly": True},
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print(f"[executor] SL attempt {i + 1}/{attempts} failed for {symbol}: {exc}")
            time.sleep(0.6 * (i + 1))
    raise last_exc


def open_trade(symbol, direction, entry, sl, tp1, tp2, lights_count, aligned, *, notify=True, manage="bracket"):
    """
    Open a position for an activated signal (legacy market-on-touch path, used when
    USE_RESTING_ORDERS is False).

    Returns a plan dict describing what was (or would be) placed. In dry-run the
    plan is logged and returned with live=False; no network call is made.

    `manage` is "bracket" (S1/S2) or "trailing" (S3/S4: SL only, no TP placed).
    """
    is_long = direction == "LONG"
    # Risk-based sizing overrides the fixed margin when enabled (else None → fixed).
    margin = risk_based_margin(entry, sl) or _margin_for(lights_count, aligned)
    notional = margin * LEVERAGE
    raw_amount = (notional / entry) if entry else 0.0
    amount = _round_amount(symbol, raw_amount)

    plan = {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "manage": manage,
        "lights": lights_count,
        "leverage": LEVERAGE,
        "margin_usdt": round(margin, 2),
        "notional_usdt": round(notional, 2),
        "amount": amount,
        # Fields move_stop_to_breakeven / the concurrency cap rely on, so the
        # legacy market path tracks the same shape the resting path does.
        "close_side": "sell" if is_long else "buy",
        "tp_rest": amount,
        "live": False,
        "orders": [],
        "error": None,
    }

    if amount <= 0:
        plan["error"] = "computed amount is zero"
        print(f"[executor] Skip {symbol}: {plan['error']}")
        return plan

    too_small, why = below_min_order_size(symbol, amount, entry)
    if too_small:
        plan["error"] = f"below min order size ({why})"
        print(f"[executor] Skip {symbol}: {plan['error']} — too small for this account.")
        return plan

    if not is_live():
        msg = (
            f"🧪 DRY-RUN order ({direction} {symbol})\n"
            f"entry {entry} | SL {sl} | TP1 {tp1} | TP2 {tp2}\n"
            f"margin {plan['margin_usdt']} USDT × {LEVERAGE}x = "
            f"{plan['notional_usdt']} USDT notional | qty {amount}"
        )
        print(f"[executor][DRY-RUN] {msg.replace(chr(10), ' | ')}")
        if notify:
            send_message(msg)
        # Track the (simulated) open position so the concurrency cap counts it.
        with _active_lock:
            plan["opened_at"] = time.time()
            _active_brackets[(symbol, direction)] = plan
        return plan

    # ── LIVE PATH ─────────────────────────────────────────────────────────
    ex = _get_exchange()
    entry_side = "buy" if is_long else "sell"
    close_side = "sell" if is_long else "buy"

    # 1) ENTRY. If this fails, no position exists, so there is nothing to protect.
    try:
        try:
            ex.set_margin_mode(MARGIN_MODE, symbol)
        except Exception as exc:  # noqa: BLE001 — already set / no position is fine
            print(f"[executor] set_margin_mode note for {symbol}: {exc}")
        try:
            ex.set_leverage(LEVERAGE, symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] set_leverage note for {symbol}: {exc}")

        # Clear any stale conditional SL/TP left on this symbol by a PRIOR closed
        # trade before placing fresh brackets. reduceOnly stops do NOT auto-cancel
        # when a position closes, so without this a leftover stop could fire
        # against the new position.
        try:
            _cancel_protective_orders(ex, symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] pre-open stale-order cleanup note {symbol}: {exc}")

        entry_order = ex.create_order(symbol, "market", entry_side, amount)
        plan["orders"].append({"role": "entry", "id": entry_order.get("id")})
        plan["live"] = True
    except Exception as exc:  # noqa: BLE001
        plan["error"] = str(exc)
        print(f"[executor] LIVE entry FAILED for {symbol}: {exc}")
        if notify:
            send_message(f"⚠️ LIVE entry FAILED: {direction} {symbol}\n{exc}")
        return plan

    # 2) Entry FILLED → the position now EXISTS. Track it IMMEDIATELY so it counts
    #    toward the concurrency cap and can always be found/managed, even if a
    #    protective leg below fails.
    plan["opened_at"] = time.time()
    with _active_lock:
        _active_brackets[(symbol, direction)] = plan

    if PLACE_BRACKET_ORDERS:
        sl_p = _round_price(symbol, sl)
        # Stop loss — protects the WHOLE position. Retry transient rejections
        # (e.g. -2022 ReduceOnly while the fill propagates); if it STILL can't be
        # placed, EMERGENCY-CLOSE rather than let the position run naked.
        try:
            sl_order = _place_stop_with_retry(ex, symbol, close_side, amount, sl_p)
            plan["orders"].append({"role": "sl", "id": sl_order.get("id")})
            plan["sl_placed"] = True
        except Exception as exc:  # noqa: BLE001
            plan["error"] = f"stop-loss placement failed: {exc}"
            plan["sl_placed"] = False
            print(f"[executor] CRITICAL: SL failed after retries for {symbol}: {exc} — emergency closing")
            closed = close_position_market(symbol)
            with _active_lock:
                _active_brackets.pop((symbol, direction), None)
            # Safety alarms ALWAYS send (even when routine order notifications are
            # muted) — a naked position is real-money risk the user must know about.
            if closed.get("ok"):
                send_message(f"🛑 {direction} {symbol}: stop-loss could NOT be placed — position "
                             f"was EMERGENCY-CLOSED to avoid running naked.")
            else:
                send_message(f"🚨 {direction} {symbol}: POSITION OPEN with NO STOP-LOSS and the "
                             f"emergency close also FAILED ({closed.get('error')}). MANUAL ACTION NEEDED.",
                             force=True)   # true emergency — always audible, even in quiet mode
            return plan

        # Take-profit. NON-fatal: the position is already stop-protected, so a TP
        # failure leaves the position open WITH its stop instead of aborting.
        try:
            if manage == "trailing":
                # S3/S4: no take-profit — the bot ratchets the stop each candle.
                plan["tp_rest"] = amount
            elif LIVE_PARTIAL_TP:
                # 50% at TP1 + 50% at TP2 (matches the resting path). NOTE: on a
                # tiny account each half can fall under Binance's ~5 USDT floor —
                # that's why LIVE_PARTIAL_TP defaults OFF for the 25 USDT account.
                half = _round_amount(symbol, amount / 2.0)
                tp1_order = ex.create_order(
                    symbol, "TAKE_PROFIT_MARKET", close_side, half, None,
                    {"stopPrice": _round_price(symbol, tp1), "reduceOnly": True},
                )
                plan["orders"].append({"role": "tp1", "id": tp1_order.get("id")})
                tp2_order = ex.create_order(
                    symbol, "TAKE_PROFIT_MARKET", close_side, amount - half, None,
                    {"stopPrice": _round_price(symbol, tp2), "reduceOnly": True},
                )
                plan["orders"].append({"role": "tp2", "id": tp2_order.get("id")})
                plan["tp_rest"] = amount - half
            else:
                # Single 100% target — the tiny-account default, identical to the
                # resting path's single-TP mode so the whole position closes at one
                # take-profit (LIVE_TP_TARGET: tp2=2R runner, tp1=1R de-risk).
                tp_target = tp1 if LIVE_TP_TARGET == "tp1" else tp2
                tp_order = ex.create_order(
                    symbol, "TAKE_PROFIT_MARKET", close_side, amount, None,
                    {"stopPrice": _round_price(symbol, tp_target), "reduceOnly": True},
                )
                plan["orders"].append({"role": "tp", "id": tp_order.get("id")})
                plan["tp_rest"] = amount
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] TP placement failed for {symbol} (position IS stop-protected): {exc}")
            if notify:
                send_message(f"⚠️ {direction} {symbol}: take-profit order failed ({exc}). "
                             f"Position is OPEN and stop-protected; no TP set.")

    net = "TESTNET" if USE_TESTNET else "MAINNET"
    msg = (
        f"✅ LIVE {net} order filled: {direction} {symbol}\n"
        f"qty {amount} @ ~{entry} | SL {sl} | TP1 {tp1} | TP2 {tp2}\n"
        f"margin {plan['margin_usdt']} USDT × {LEVERAGE}x"
    )
    print(f"[executor][LIVE] {msg.replace(chr(10), ' | ')}")
    if notify:
        send_message(msg)
    return plan


def close_symbol(symbol, *, notify=False):
    """Cancel any open orders for a symbol (used on manual exit / cleanup)."""
    with _resting_lock:
        for key in [k for k in _resting if k[0] == symbol]:
            _resting.pop(key, None)
    if not is_live():
        print(f"[executor][DRY-RUN] Would cancel open orders for {symbol}")
        return
    try:
        ex = _get_exchange()
        ex.cancel_all_orders(symbol)
        print(f"[executor][LIVE] Cancelled open orders for {symbol}")
        if notify:
            send_message(f"🧹 Cancelled open orders for {symbol}")
    except Exception as exc:  # noqa: BLE001
        print(f"[executor] Failed to cancel orders for {symbol}: {exc}")


# ── Resting-limit model ───────────────────────────────────────────────────
# The moment a setup is queued we place a resting LIMIT entry + SL/TP bracket on
# Binance. The exchange fills the entry exactly when price touches it (no polling
# lag, no missed entries) and the bracket protects the position even if the bot
# goes offline.

def _resting_plan(symbol, direction, entry, sl, tp1, tp2, lights_count, aligned, manage="bracket"):
    is_long = direction == "LONG"
    # Risk-based sizing overrides the fixed margin when enabled (else None → fixed).
    margin = risk_based_margin(entry, sl) or _margin_for(lights_count, aligned)
    notional = margin * LEVERAGE
    amount = _round_amount(symbol, (notional / entry) if entry else 0.0)
    half = _round_amount(symbol, amount / 2.0)
    tp_target = tp1 if LIVE_TP_TARGET == "tp1" else tp2
    return {
        "symbol": symbol, "direction": direction, "is_long": is_long,
        "close_side": "sell" if is_long else "buy",
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "tp_target": tp_target, "tp_label": LIVE_TP_TARGET,
        # "bracket" = fixed SL + TP1/TP2 (S1/S2). "trailing" = SL only, ratcheted by
        # the bot each closed candle, NO take-profit order (S3/S4).
        "manage": manage,
        # tp_rest is the size the post-TP1 stop covers (bracket: the remaining 50%).
        # A trailing position never partials, so its stop always covers the FULL
        # amount — move_stop() defaults its size to tp_rest, so this must be `amount`.
        "amount": amount, "half": half,
        "tp_rest": amount if manage == "trailing" else (amount - half),
        "margin_usdt": round(margin, 2),
        "notional_usdt": round(notional, 2), "lights": lights_count,
        "sl_placed": False,
        "tp_placed": {"tp1": False, "tp2": False, "tp": False},
    }


def _same_resting(existing, plan) -> bool:
    """True if an already-placed resting order matches the new plan (idempotent)."""
    if not existing:
        return False
    keys = ("entry", "sl", "tp1", "tp2", "amount")
    return all(existing.get(k) == plan.get(k) for k in keys)


def _tp_plan_text(plan) -> str:
    if plan.get("manage") == "trailing":
        return "trailing stop (no fixed TP)"
    if LIVE_PARTIAL_TP:
        return f"TP1(50%) {plan['tp1']} + TP2(50%) {plan['tp2']}"
    return f"TP({plan['tp_label']}) {plan['tp_target']}"


def _place_stop(ex, plan, orders):
    """Place the stop-loss for a position that exists. Idempotent via
    plan['sl_placed']. Returns True if the stop is now confirmed placed.

    Uses a **reduceOnly** trigger (the mechanism the market path and the manual
    Account-page setter both use successfully on this account). reduceOnly is
    rejected before a position exists, so the queue-time attempt fails by design
    and on_resting_filled places it the moment the entry fills. (The old
    closePosition approach failed pre-fill with -4509 AND could not coexist with
    a closePosition take-profit on the same side — -4130 — leaving positions
    naked; reduceOnly has neither problem.)"""
    if plan.get("sl_placed"):
        return True
    symbol = plan["symbol"]
    try:
        o = ex.create_order(
            symbol, "STOP_MARKET", plan["close_side"], plan["amount"], None,
            {"stopPrice": _round_price(symbol, plan["sl"]), "reduceOnly": True},
        )
        orders.append({"role": "sl", "id": o.get("id")})
        plan["sl_placed"] = True
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[executor] SL order note for {symbol}: {exc}")
        return False


def _place_take_profits(ex, plan, orders):
    """Place the take-profit order(s) for a position that exists.
    Updates plan['tp_placed'] and appends to orders.
    Partial mode = 50% at TP1 + 50% at TP2; single mode = 100% at one target.

    All legs use **reduceOnly** triggers — the mechanism proven to work on this
    account (the market path and manual setter both use it). reduceOnly is
    rejected before a position exists, so queue-time attempts fail by design and
    on_resting_filled places them once the entry fills. (closePosition was used
    here before for "offline" pre-fill booking, but Binance rejects it pre-fill
    -4509 on this account, and two closePosition legs on the same side collide
    with -4130 — so it never actually protected anything.)"""
    # Trailing strategies (S3/S4) have NO fixed take-profit — the stop is ratcheted
    # by the bot each closed candle and is the only exit order on the exchange.
    if plan.get("manage") == "trailing":
        return
    symbol = plan["symbol"]
    close_side = plan["close_side"]
    if LIVE_PARTIAL_TP:
        # TP2 — the runner target on the remaining 50%.
        if not plan["tp_placed"].get("tp2"):
            try:
                o = ex.create_order(
                    symbol, "TAKE_PROFIT_MARKET", close_side, plan["tp_rest"], None,
                    {"stopPrice": _round_price(symbol, plan["tp2"]), "reduceOnly": True},
                )
                orders.append({"role": "tp2", "id": o.get("id")})
                plan["tp_placed"]["tp2"] = True
            except Exception as exc:  # noqa: BLE001
                print(f"[executor] tp2 order note for {symbol}: {exc}")
        # TP1 — the 50% partial.
        if not plan["tp_placed"].get("tp1") and plan["half"] > 0:
            try:
                o = ex.create_order(
                    symbol, "TAKE_PROFIT_MARKET", close_side, plan["half"], None,
                    {"stopPrice": _round_price(symbol, plan["tp1"]), "reduceOnly": True},
                )
                orders.append({"role": "tp1", "id": o.get("id")})
                plan["tp_placed"]["tp1"] = True
            except Exception as exc:  # noqa: BLE001
                print(f"[executor] tp1 order note for {symbol}: {exc}")
    else:
        if not plan["tp_placed"].get("tp"):
            try:
                o = ex.create_order(
                    symbol, "TAKE_PROFIT_MARKET", close_side, plan["amount"], None,
                    {"stopPrice": _round_price(symbol, plan["tp_target"]), "reduceOnly": True},
                )
                orders.append({"role": "tp", "id": o.get("id")})
                plan["tp_placed"]["tp"] = True
            except Exception as exc:  # noqa: BLE001
                print(f"[executor] TP order note for {symbol}: {exc}")


def place_resting_order(symbol, direction, entry, sl, tp1, tp2, lights_count, aligned, *, notify=False, manage="bracket"):
    """Place (or refresh) a resting LIMIT entry + SL/TP bracket for a queued setup.

    Idempotent across scans: if an identical resting order already exists it is
    left alone; if the plan changed (e.g. a new scan moved the entry) the old
    orders are cancelled and replaced.

    `manage` is "bracket" (S1/S2: SL + TP1/TP2) or "trailing" (S3/S4: SL only,
    ratcheted by the bot each candle — no take-profit order is placed).
    """
    if not USE_RESTING_ORDERS:
        return None

    plan = _resting_plan(symbol, direction, entry, sl, tp1, tp2, lights_count, aligned, manage=manage)
    key = (symbol, direction)

    if plan["amount"] <= 0:
        return None

    too_small, why = below_min_order_size(symbol, plan["amount"], entry)
    if too_small:
        print(f"[executor] Skip resting {direction} {symbol}: below min order size ({why}) — too small for this account.")
        return None

    with _resting_lock:
        existing = _resting.get(key)
    if _same_resting(existing, plan):
        return existing  # nothing changed — leave the resting order in place

    # Plan changed or new — clear any previous resting orders for this setup first.
    if existing is not None:
        cancel_resting_order(symbol, direction, reason="refreshed by new scan")

    if not is_live():
        msg = (
            f"🧪 DRY-RUN resting bracket ({direction} {symbol})\n"
            f"LIMIT entry {entry} | SL {sl} | {_tp_plan_text(plan)}\n"
            f"qty {plan['amount']} | margin {plan['margin_usdt']} USDT × {LEVERAGE}x"
        )
        print(f"[executor][DRY-RUN] {msg.replace(chr(10), ' | ')}")
        if notify:
            send_message(msg)
        plan["orders"] = []
        with _resting_lock:
            _resting[key] = plan
        return plan

    # ── LIVE PATH ─────────────────────────────────────────────────────────
    ex = _get_exchange()
    entry_side = "buy" if plan["is_long"] else "sell"
    close_side = "sell" if plan["is_long"] else "buy"
    amount = plan["amount"]
    orders = []
    try:
        try:
            ex.set_margin_mode(MARGIN_MODE, symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] set_margin_mode note for {symbol}: {exc}")
        try:
            ex.set_leverage(LEVERAGE, symbol)
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] set_leverage note for {symbol}: {exc}")

        # 1. Resting LIMIT entry — fills exactly at the planned entry price.
        #    Post-Only (GTX) when enabled: guarantees a maker fill and refuses to
        #    cross the book (the entry is always on the passive side anyway).
        entry_params = {"timeInForce": "GTX"} if USE_POST_ONLY_ENTRY else {}
        entry_order = ex.create_order(
            symbol, "limit", entry_side, amount, _round_price(symbol, entry), entry_params,
        )
        orders.append({"role": "entry", "id": entry_order.get("id")})

        if PLACE_BRACKET_ORDERS:
            # 2. Stop loss + 3. take profit(s). These reduceOnly triggers are
            #    REJECTED before the entry fills (no position yet), so the
            #    queue-time attempt is best-effort and on_resting_filled places
            #    BOTH the moment the entry fills. A filled position must never be
            #    left with a TP but no SL.
            _place_stop(ex, plan, orders)
            _place_take_profits(ex, plan, orders)

        plan["orders"] = orders
        plan["live"] = True
        with _resting_lock:
            _resting[key] = plan

        net = "TESTNET" if USE_TESTNET else "MAINNET"
        msg = (
            f"⏳ LIVE {net} resting bracket: {direction} {symbol}\n"
            f"LIMIT entry {entry} | SL {sl} | {_tp_plan_text(plan)}\n"
            f"qty {amount} | margin {plan['margin_usdt']} USDT × {LEVERAGE}x"
        )
        print(f"[executor][LIVE] {msg.replace(chr(10), ' | ')}")
        if notify:
            send_message(msg)
        return plan
    except Exception as exc:  # noqa: BLE001
        print(f"[executor] LIVE resting order FAILED for {symbol}: {exc}")
        if notify:
            send_message(f"⚠️ LIVE resting order FAILED: {direction} {symbol}\n{exc}")
        # Roll back any partial bracket so we never leave a naked order resting.
        if orders:
            try:
                _get_exchange().cancel_all_orders(symbol)
            except Exception:  # noqa: BLE001
                pass
        return None


def cancel_resting_order(symbol, direction, *, reason="", notify=False):
    """Cancel a resting setup's entry + bracket orders (setup died before fill)."""
    key = (symbol, direction)
    with _resting_lock:
        existing = _resting.pop(key, None)
    if existing is None:
        return

    label = f" ({reason})" if reason else ""
    if not is_live():
        print(f"[executor][DRY-RUN] Would cancel resting {direction} {symbol}{label}")
        return
    try:
        ex = _get_exchange()
        for o in existing.get("orders", []):
            oid = o.get("id")
            if not oid:
                continue
            # SL/TP are conditional (strategy book) → need the stop flag to cancel.
            stop = o.get("role") in ("sl", "tp", "tp1", "tp2")
            try:
                ex.cancel_order(oid, symbol, params={"stop": True} if stop else {})
            except Exception as exc:  # noqa: BLE001
                print(f"[executor] cancel {o.get('role')} {symbol} note: {exc}")
        print(f"[executor][LIVE] Cancelled resting {direction} {symbol}{label}")
        if notify:
            send_message(f"🧹 Cancelled resting {direction} {symbol}{label}")
    except Exception as exc:  # noqa: BLE001
        print(f"[executor] Failed to cancel resting {symbol}: {exc}")


def on_resting_filled(symbol, direction):
    """The resting entry has filled. Stop tracking it as pending and make sure
    the take-profit orders are in place (now that the position definitely
    exists, any reduce-only TP that the exchange rejected at queue time can be
    placed). The SL/TP bracket then manages the open position on the exchange."""
    key = (symbol, direction)
    with _resting_lock:
        plan = _resting.pop(key, None)
    if plan is None:
        return

    if not is_live():
        tp_txt = _tp_plan_text(plan)
        print(f"[executor][DRY-RUN] Resting entry filled: {direction} {symbol} @ {plan.get('entry')} | {tp_txt}")
        # Track the (simulated) open position so the concurrency cap counts it
        # exactly as it would live. on_trade_closed clears it in both modes.
        with _active_lock:
            _active_brackets[key] = plan
        return

    # Place the stop-loss AND take-profit(s) that the exchange rejected before the
    # fill (reduceOnly triggers need a live position). The SL retry is critical:
    # without it the position runs with a TP but no stop, and the bot later thinks
    # an unprotected position was stopped out.
    orders = plan.get("orders", [])
    if PLACE_BRACKET_ORDERS:
        ex = _get_exchange()
        try:
            _place_stop(ex, plan, orders)
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] SL placement on fill failed for {symbol}: {exc}")
        try:
            _place_take_profits(ex, plan, orders)
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] TP placement on fill failed for {symbol}: {exc}")
        # A filled position with no working stop is dangerous — alert loudly so it
        # can be protected by hand instead of failing silently.
        if not plan.get("sl_placed"):
            warn = (f"⚠️ NO STOP-LOSS placed for {direction} {symbol} after fill — "
                    f"position is UNPROTECTED. Set a stop manually on the Account page.")
            print(f"[executor][ALERT] {warn}")
            try:
                send_message(warn)
            except Exception:  # noqa: BLE001
                pass
    plan["orders"] = orders
    with _active_lock:
        _active_brackets[key] = plan


def move_stop(symbol, direction, new_stop_price, *, size=None, notify=False, reason=""):
    """Cancel the tracked stop and place a fresh **reduceOnly** STOP_MARKET at
    new_stop_price. The single primitive behind BOTH the breakeven move (S1/S2)
    and the trailing ratchet (S3/S4). reduceOnly is the only trigger mechanism
    that works on this account (coexists with any TP, no -4130/-4509).

    `size` defaults to the position's remaining size (`tp_rest`, which equals the
    full amount for trailing positions that never partial). Returns True on
    success. No-op (returns False) if the position isn't a live-tracked bracket."""
    key = (symbol, direction)
    with _active_lock:
        plan = _active_brackets.get(key)
    if plan is None:
        return False  # not a live-tracked filled position
    if size is None:
        size = plan.get("tp_rest") or plan.get("amount")

    if not is_live():
        print(f"[executor][DRY-RUN] Would move SL→{new_stop_price} for {direction} {symbol}"
              f"{(' ('+reason+')') if reason else ''}")
        plan["sl"] = new_stop_price
        return True

    try:
        ex = _get_exchange()
        sp = _round_price(symbol, new_stop_price)
        # 1. Place the NEW stop FIRST (reduceOnly so it coexists with any live TP
        #    without -4130). Doing this before the cancel means a rejected new order
        #    never leaves the position momentarily naked — the old stop still stands.
        sl_order = ex.create_order(
            symbol, "STOP_MARKET", plan["close_side"], size, None,
            {"stopPrice": sp, "reduceOnly": True},
        )
        # 2. Now cancel the prior stop(s) (conditional → need the stop flag).
        for o in [o for o in plan.get("orders", []) if o.get("role") == "sl"]:
            oid = o.get("id")
            if oid:
                try:
                    ex.cancel_order(oid, symbol, params={"stop": True})
                except Exception:  # noqa: BLE001 — already gone is fine
                    pass
        plan["orders"] = [o for o in plan.get("orders", []) if o.get("role") != "sl"]
        plan["orders"].append({"role": "sl", "id": sl_order.get("id")})
        plan["sl"] = new_stop_price
        plan["sl_placed"] = True
        with _active_lock:
            _active_brackets[key] = plan
        print(f"[executor][LIVE] Moved SL→{sp} for {direction} {symbol}"
              f"{(' ('+reason+')') if reason else ''}")
        if notify:
            send_message(f"🛡️ {direction} {symbol}: stop moved to {sp}"
                         f"{(' — '+reason) if reason else ''}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[executor] move_stop failed for {symbol}: {exc}")
        return False


def move_stop_to_breakeven(symbol, direction, entry, *, notify=False):
    """After TP1's 50% closes, replace the live stop with a breakeven stop on the
    remaining 50% — mirrors the dashboard simulation so the live result matches the
    dry-run. Thin wrapper over move_stop. No-op if LIVE_TRAIL_TO_BREAKEVEN is off."""
    if not LIVE_TRAIL_TO_BREAKEVEN:
        return
    key = (symbol, direction)
    with _active_lock:
        plan = _active_brackets.get(key)
    if plan is None:
        return  # not a live-tracked filled position
    size = plan.get("tp_rest") or plan.get("amount")
    if move_stop(symbol, direction, entry, size=size) and notify and is_live():
        send_message(f"🛡️ {direction} {symbol}: TP1 banked — stop moved to breakeven "
                     f"{_round_price(symbol, entry)}")


def on_trade_closed(symbol, direction):
    """The trade has fully closed (TP2 or SL). Cancel any leftover bracket orders
    (e.g. the unfilled partial TP after a stop-out) so nothing dangles."""
    key = (symbol, direction)
    with _active_lock:
        plan = _active_brackets.pop(key, None)
    if plan is None or not is_live():
        return
    try:
        ex = _get_exchange()
        # Leftover SL/TP are conditional (strategy book); cancel_order without the
        # stop flag can't reach them, so clear them with the protective helper.
        _cancel_protective_orders(ex, symbol)
        # Cancel any non-conditional leftovers we tracked (e.g. an unfilled entry).
        for o in plan.get("orders", []):
            oid = o.get("id")
            if not oid or o.get("role") in ("entry", "sl", "tp", "tp1", "tp2"):
                continue
            try:
                ex.cancel_order(oid, symbol)
            except Exception:  # noqa: BLE001 — already filled/cancelled is fine
                pass
        print(f"[executor][LIVE] Cleaned up bracket for closed {direction} {symbol}")
    except Exception as exc:  # noqa: BLE001
        print(f"[executor] Cleanup for {symbol} note: {exc}")


def reconcile_resting(active_keys, *, notify=False):
    """Cancel resting orders for setups that are no longer queued.

    `active_keys` is the set of (symbol, direction) still on the queue after a
    scan. Any resting order not in that set describes a setup that stopped
    qualifying, so its un-filled entry must be pulled before it can fill."""
    if not USE_RESTING_ORDERS:
        return
    with _resting_lock:
        stale = [key for key in _resting if key not in active_keys]
    for symbol, direction in stale:
        cancel_resting_order(symbol, direction, reason="setup no longer qualifies", notify=notify)


def has_resting_order(symbol, direction) -> bool:
    with _resting_lock:
        return (symbol, direction) in _resting


# ── Read-only account snapshot (for the dashboard Account page) ───────────────
_account_cache: dict = {"ts": 0.0, "data": None}
_account_cache_lock = threading.Lock()
ACCOUNT_CACHE_TTL = 5.0  # seconds — keep a refreshing page from hammering the API


def account_snapshot(*, force: bool = False) -> dict:
    """Read-only snapshot of the live USD-M futures account: wallet balance,
    open positions and open orders. Cached for a few seconds so an auto-
    refreshing dashboard page doesn't spam the API. NEVER sends an order.

    Works whenever API keys exist (independent of LIVE_TRADING) so the page can
    show the real account even while order placement is off."""
    if not _keys_present():
        return {"ok": False, "error": "No Binance API keys configured.",
                "net": None, "live": False, "balance": None,
                "positions": [], "orders": []}
    now = time.time()
    with _account_cache_lock:
        cached = _account_cache["data"]
        if not force and cached is not None and (now - _account_cache["ts"]) < ACCOUNT_CACHE_TTL:
            return cached

    net = "TESTNET" if USE_TESTNET else "MAINNET"
    try:
        ex = _get_exchange()
        raw = ex.fetch_balance()
        info = raw.get("info", {}) or {}

        def _num(*keys):
            for k in keys:
                v = info.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return None

        balance = {
            "wallet": _num("totalWalletBalance"),
            "margin_balance": _num("totalMarginBalance"),
            "unrealized_pnl": _num("totalUnrealizedProfit"),
            "available": _num("availableBalance"),
        }

        positions = []
        for p in ex.fetch_positions():
            try:
                amt = float(p.get("contracts") or 0.0)
            except (TypeError, ValueError):
                amt = 0.0
            if not amt:
                continue
            positions.append({
                "symbol": p.get("symbol"),
                "side": (p.get("side") or "").upper(),
                "contracts": amt,
                "notional": p.get("notional"),
                "entry": p.get("entryPrice"),
                "mark": p.get("markPrice"),
                "liq": p.get("liquidationPrice"),
                "leverage": p.get("leverage"),
                "unrealized_pnl": p.get("unrealizedPnl"),
                "pnl_pct": p.get("percentage"),
            })

        # Acknowledge ccxt's stricter-rate-limit warning for the all-symbols
        # query; our ~5s cache keeps the call frequency low.
        ex.options["warnOnFetchOpenOrdersWithoutSymbol"] = False
        # Plain orders (limit/market) AND conditional SL/TP triggers, which on
        # this account live in a separate strategy book invisible to the plain
        # query — fetch them per open-position symbol with the stop flag so the
        # dashboard actually shows the protection on each position.
        raw_orders = list(ex.fetch_open_orders())
        seen_ids = {o.get("id") for o in raw_orders}
        for p in positions:
            psym = p.get("symbol")
            if not psym:
                continue
            try:
                for o in ex.fetch_open_orders(psym, params={"stop": True}):
                    if o.get("id") not in seen_ids:
                        raw_orders.append(o)
                        seen_ids.add(o.get("id"))
            except Exception as exc:  # noqa: BLE001
                print(f"[executor] conditional-orders fetch note {psym}: {exc}")
        sym_mark = {p["symbol"]: p.get("mark") for p in positions}
        orders = []
        for o in raw_orders:
            oinfo = o.get("info", {}) or {}
            trigger = o.get("triggerPrice") or oinfo.get("stopPrice")
            # 'sl' / 'tp' inferred from trigger vs the position's mark (type strings
            # are unreliable here — see _trigger_role). Lets the dashboard tell a
            # protected position from a naked one even though Binance reports the
            # conditional order's type as plain 'market'.
            role = _trigger_role(o.get("side"), trigger, sym_mark.get(o.get("symbol")))
            orders.append({
                "symbol": o.get("symbol"),
                "side": (o.get("side") or "").upper(),
                "type": o.get("type") or oinfo.get("type"),
                "role": role,
                "price": o.get("price"),
                "trigger": trigger,
                "amount": o.get("amount"),
                "reduce_only": o.get("reduceOnly", oinfo.get("reduceOnly")),
                "close_position": str(oinfo.get("closePosition")).lower() == "true",
            })

        data = {
            "ok": True, "net": net, "live": is_live(),
            "balance": balance, "positions": positions, "orders": orders,
        }
        with _account_cache_lock:
            _account_cache["ts"] = now
            _account_cache["data"] = data
        return data
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "net": net, "live": is_live(),
                "balance": None, "positions": [], "orders": []}


def open_position_symbols() -> set[str]:
    """Symbols that currently hold a live, nonzero position on the exchange.

    Reads the cached read-only ``account_snapshot()`` (~5s TTL) so calling it once
    per scan costs at most one extra API request. Returns an EMPTY set whenever the
    account can't be read (no keys, or the snapshot failed) — the caller then
    applies no re-entry gate rather than blocking trading on a transient API hiccup.
    """
    snap = account_snapshot()
    if not snap.get("ok"):
        return set()
    return {p.get("symbol") for p in (snap.get("positions") or []) if p.get("symbol")}


def has_open_position(symbol) -> bool:
    """True if ``symbol`` already holds a live position (see open_position_symbols)."""
    return symbol in open_position_symbols()


def open_directional_counts() -> dict:
    """How many live positions are currently LONG vs SHORT, from the cached
    snapshot. Returns {"LONG": n, "SHORT": n} (zeros when the account can't be
    read). Used by the optional correlation / same-direction cap so a basket of
    same-side alt trades isn't treated as independent bets."""
    counts = {"LONG": 0, "SHORT": 0}
    snap = account_snapshot()
    if not snap.get("ok"):
        return counts
    for p in (snap.get("positions") or []):
        side = (p.get("side") or "").upper()
        if side in counts:
            counts[side] += 1
    return counts


def reconcile_open_positions(*, min_age_sec: float = 90.0) -> dict:
    """Release tracked brackets whose position has CLOSED on the exchange.

    The S2 scanner has no per-fill callback (unlike the S1 bot, which calls
    on_trade_closed when a trade exits), so without this _active_brackets would
    only ever grow: once it reaches MAX_CONCURRENT_POSITIONS the cap wedges shut
    and the engine silently stops opening new trades. Each sweep we compare the
    tracked brackets to the live positions and, for any tracked (symbol,
    direction) that is no longer open, call on_trade_closed — which frees the cap
    slot AND cancels the stale reduceOnly SL/TP so it can't fire against a future
    position on the same symbol.

    Fail-safe: a missing/failed snapshot prunes NOTHING. Brackets younger than
    ``min_age_sec`` are skipped, so a just-opened position whose fill has not yet
    propagated into the snapshot is never mistaken for closed."""
    if not is_live():
        return {"ok": False, "reason": "dry-run", "released": []}
    snap = account_snapshot()
    if not snap.get("ok"):
        return {"ok": False, "reason": "snapshot unavailable", "released": []}
    live_keys = {
        (p.get("symbol"), (p.get("side") or "").upper())
        for p in (snap.get("positions") or [])
        if p.get("symbol") and float(p.get("contracts") or 0.0)
    }
    now = time.time()
    with _active_lock:
        candidates = [
            k for k, v in _active_brackets.items()
            if now - float(v.get("opened_at") or 0.0) >= min_age_sec
        ]
    released = []
    for (symbol, direction) in candidates:
        if (symbol, direction) in live_keys:
            continue
        try:
            on_trade_closed(symbol, direction)      # frees the cap slot + cancels stale SL/TP
            released.append((symbol, direction))
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] reconcile cleanup note {symbol} {direction}: {exc}")
    if released:
        print(f"[executor][RECONCILE] released {len(released)} closed position(s): {released}")
    return {"ok": True, "released": released}


def funding_rate(symbol: str):
    """Current funding rate for a perpetual as a per-interval fraction
    (Binance 8h): 0.001 = 0.10%/8h. Positive = longs pay shorts (crowded longs).

    Read-only public data — works without API keys. Returns None on any error so
    the optional funding filter fails OPEN (never blocks trading on a hiccup)."""
    try:
        fr = _get_exchange().fetch_funding_rate(symbol)
        rate = fr.get("fundingRate")
        return float(rate) if rate is not None else None
    except Exception as exc:  # noqa: BLE001
        print(f"[executor] funding-rate note {symbol}: {exc}")
        return None


def _open_position(ex, symbol):
    """Return the live nonzero position dict for a symbol, or None."""
    for p in ex.fetch_positions([symbol]):
        try:
            if float(p.get("contracts") or 0.0):
                return p
        except (TypeError, ValueError):
            pass
    return None


def _trigger_role(order_side, trigger, mark):
    """Classify a reduceOnly trigger order as 'sl' / 'tp' from its trigger vs mark.
    Binance returns strategy-book SL/TP with ccxt type 'market' and no origType —
    only a triggerPrice — so type strings can't tell them apart. Instead: a SELL
    trigger BELOW mark (closing a long) or a BUY trigger ABOVE mark (closing a
    short) is a STOP-LOSS; the mirror is a take-profit. Returns None if unknown."""
    try:
        trigger = float(trigger); mark = float(mark)
    except (TypeError, ValueError):
        return None
    if not trigger or not mark:
        return None
    s = (order_side or "").lower()
    if s == "sell":                     # closing a long
        return "sl" if trigger < mark else "tp"
    if s == "buy":                      # closing a short
        return "sl" if trigger > mark else "tp"
    return None


def _fetch_protective_orders(ex, symbol):
    """Conditional SL/TP (STOP/TAKE_PROFIT) orders for a symbol. On this account
    they live in a separate strategy book that the plain order API can't see, so
    they MUST be queried with the stop flag."""
    try:
        return ex.fetch_open_orders(symbol, params={"stop": True})
    except Exception as exc:  # noqa: BLE001
        print(f"[executor] fetch protective orders note {symbol}: {exc}")
        return []


def _cancel_protective_orders(ex, symbol, *, exclude_ids=()):
    """Cancel every conditional SL/TP order for a symbol. cancel_all_orders does
    NOT touch the strategy book, so each must be cancelled by id with the stop
    flag. `exclude_ids` keeps freshly-placed orders. Returns the count cancelled."""
    n = 0
    for o in _fetch_protective_orders(ex, symbol):
        oid = o.get("id")
        if not oid or oid in exclude_ids:
            continue
        try:
            ex.cancel_order(oid, symbol, params={"stop": True})
            n += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] cancel protective {oid} {symbol} note: {exc}")
    return n


def close_position_market(symbol: str) -> dict:
    """MANUAL: close an open position at market (reduce-only), reading the exact
    size from the exchange. Cancels the symbol's resting orders first so no
    bracket lingers after the close. Returns {ok, ...} for the dashboard."""
    if not _keys_present():
        return {"ok": False, "error": "No Binance API keys configured."}
    try:
        ex = _get_exchange()
        pos = _open_position(ex, symbol)
        if pos is None:
            return {"ok": False, "error": f"No open position for {symbol}."}
        amt = abs(float(pos.get("contracts") or 0.0))
        side = (pos.get("side") or "").lower()           # long / short
        close_side = "sell" if side == "long" else "buy"
        try:
            ex.cancel_all_orders(symbol)                 # drop plain orders
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] cancel-before-close note {symbol}: {exc}")
        _cancel_protective_orders(ex, symbol)            # AND the strategy-book SL/TP
        order = ex.create_order(symbol, "market", close_side,
                                _round_amount(symbol, amt), None, {"reduceOnly": True})
        # Best-effort: forget any in-process tracking for this symbol.
        with _resting_lock:
            for k in [k for k in _resting if k[0] == symbol]:
                _resting.pop(k, None)
        with _active_lock:
            for k in [k for k in _active_brackets if k[0] == symbol]:
                _active_brackets.pop(k, None)
        with _account_cache_lock:
            _account_cache["data"] = None               # force fresh snapshot
        print(f"[executor][MANUAL] Closed {side} {symbol} qty {amt} at market")
        return {"ok": True, "symbol": symbol, "closed": amt, "side": side, "id": order.get("id")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def set_protection(symbol: str, *, sl=None, tp=None) -> dict:
    """MANUAL: set a stop-loss and/or take-profit for a symbol's open position
    using reduceOnly trigger orders.

    On this account, conditional (stop / take-profit) orders go into Binance's
    separate "strategy order" book that the standard order API can't list or
    cancel, and closePosition orders hit -4130 once one already exists.
    reduceOnly triggers coexist without that conflict, so 'Set' always works —
    but they may NOT appear in the dashboard's order list; verify in the Binance
    app. 'Set' REPLACES protection: a best-effort cancel runs first, then the
    provided SL/TP are placed at the full position size."""
    if not _keys_present():
        return {"ok": False, "error": "No Binance API keys configured."}
    if sl is None and tp is None:
        return {"ok": False, "error": "Enter a stop-loss and/or take-profit price."}
    try:
        ex = _get_exchange()
        pos = _open_position(ex, symbol)
        if pos is None:
            return {"ok": False, "error": f"No open position for {symbol}."}
        side = (pos.get("side") or "").lower()           # long / short
        close_side = "sell" if side == "long" else "buy"
        amt = abs(float(pos.get("contracts") or 0.0))
        mark = float(pos.get("markPrice") or 0.0)

        # Side validation — long: SL below / TP above mark; short: mirrored.
        if mark:
            if side == "long":
                if sl is not None and sl >= mark:
                    return {"ok": False, "error": f"LONG stop-loss must be BELOW mark ({mark})."}
                if tp is not None and tp <= mark:
                    return {"ok": False, "error": f"LONG take-profit must be ABOVE mark ({mark})."}
            else:
                if sl is not None and sl <= mark:
                    return {"ok": False, "error": f"SHORT stop-loss must be ABOVE mark ({mark})."}
                if tp is not None and tp >= mark:
                    return {"ok": False, "error": f"SHORT take-profit must be BELOW mark ({mark})."}

        # Snapshot the prior protective orders BEFORE adding ours, so we can clear
        # exactly the stale ones afterwards — never cancelling first (which would
        # leave the position momentarily naked if the new order then fails). These
        # live in the strategy book, so they MUST be read with the stop flag —
        # without it the old SL/TP are never found and Binance accumulates a new
        # conditional record on every Set.
        prior_ids = [o.get("id") for o in _fetch_protective_orders(ex, symbol) if o.get("id")]

        rounded_amt = _round_amount(symbol, amt)

        def _place(otype, price, role):
            """Place one reduce-only trigger; return (placed_dict, error_str)."""
            try:
                o = ex.create_order(symbol, otype, close_side, rounded_amt, None,
                                    {"stopPrice": _round_price(symbol, price), "reduceOnly": True})
                return {"role": role, "id": o.get("id"), "price": price}, None
            except Exception as exc:  # noqa: BLE001
                return None, f"{role}: {exc}"

        placed, errors = [], []
        if sl is not None:
            ok_dict, err = _place("STOP_MARKET", sl, "sl")
            (placed if ok_dict else errors).append(ok_dict or err)
        if tp is not None:
            ok_dict, err = _place("TAKE_PROFIT_MARKET", tp, "tp")
            (placed if ok_dict else errors).append(ok_dict or err)

        # Only now that the new protection is in, drop the prior (now-stale)
        # strategy-book orders (cancel by id with the stop flag).
        if placed and prior_ids:
            new_ids = {p["id"] for p in placed}
            for oid in prior_ids:
                if oid in new_ids:
                    continue
                try:
                    ex.cancel_order(oid, symbol, params={"stop": True})
                except Exception as exc:  # noqa: BLE001
                    print(f"[executor] cancel-stale {oid} {symbol} note: {exc}")

        with _account_cache_lock:
            _account_cache["data"] = None
        print(f"[executor][MANUAL] Set protection (reduceOnly) {symbol}: placed={placed} errors={errors}")
        if not placed:
            return {"ok": False, "symbol": symbol,
                    "error": "; ".join(errors) or "nothing was placed"}
        return {"ok": True, "symbol": symbol, "placed": placed, "errors": errors,
                "note": ("some legs failed: " + "; ".join(errors)) if errors
                        else "set — SL/TP replaced"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def ensure_stop_losses(*, notify=True) -> dict:
    """Guardian: every open position MUST have a stop-loss. Scans live positions
    and, for any with NO stop-loss order, places one (reduceOnly STOP_MARKET) at
    the strategy's max distance — entry ∓ MAX_SL_PCT — clamped to stay on the
    correct side of the mark so an already-underwater position is stopped right
    away. Idempotent: never touches a position that already has a stop, so it is
    safe to call repeatedly / automatically. Returns {ok, protected, skipped}."""
    import config as _cfg
    if not is_live():
        return {"ok": False, "error": "dry-run / no keys — not protecting", "protected": []}
    try:
        ex = _get_exchange()
        positions = [p for p in ex.fetch_positions() if float(p.get("contracts") or 0.0)]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "protected": []}

    max_sl = float(getattr(_cfg, "MAX_SL_PCT", 0.04))
    protected, skipped = [], []
    for p in positions:
        symbol = p.get("symbol")
        side = (p.get("side") or "").lower()
        mark = float(p.get("markPrice") or 0.0)
        entry = float(p.get("entryPrice") or 0.0) or mark
        if not symbol or side not in ("long", "short") or not mark:
            continue
        # Already has a stop-loss? Classify each reduceOnly trigger by trigger vs
        # mark (Binance reports these with ccxt type 'market', so type strings
        # can't be trusted — see _trigger_role).
        has_sl = False
        for o in _fetch_protective_orders(ex, symbol):
            trig = o.get("triggerPrice") or (o.get("info") or {}).get("stopPrice")
            if _trigger_role(o.get("side"), trig, mark) == "sl":
                has_sl = True
                break
        if has_sl:
            skipped.append({"symbol": symbol, "reason": "already protected"})
            continue
        # Prefer the trade's PLANNED (ATR) stop when we still track this position,
        # so a rescued naked position keeps its intended (often tighter) risk
        # rather than always falling back to the MAX_SL_PCT cap. Fall back to
        # entry ∓ MAX_SL_PCT when unknown, and clamp to the right side of mark so
        # an already-underwater position is stopped immediately, not rejected.
        planned_sl = None
        with _active_lock:
            bp = _active_brackets.get((symbol, side.upper()))
        if bp and bp.get("sl"):
            try:
                planned_sl = float(bp["sl"])
            except (TypeError, ValueError):
                planned_sl = None
        if side == "long":
            sl = planned_sl if (planned_sl and planned_sl < entry) else entry * (1 - max_sl)
            if sl >= mark:
                sl = mark * (1 - 0.005)
        else:
            sl = planned_sl if (planned_sl and planned_sl > entry) else entry * (1 + max_sl)
            if sl <= mark:
                sl = mark * (1 + 0.005)
        res = set_protection(symbol, sl=sl)
        if res.get("ok"):
            protected.append({"symbol": symbol, "side": side, "sl": round(sl, 8)})
        else:
            skipped.append({"symbol": symbol, "reason": res.get("error")})

    if protected and notify:
        lines = "\n".join(f"  {x['side'].upper()} {x['symbol']} → SL {x['sl']:.6g}"
                          for x in protected)
        send_message(f"🛡️ Auto-protected {len(protected)} naked position(s):\n{lines}")
    if protected:
        print(f"[executor][GUARDIAN] auto-set SL on {len(protected)} naked position(s): "
              f"{[x['symbol'] for x in protected]}")
    return {"ok": True, "protected": protected, "skipped": skipped}


def cancel_protection(symbol: str) -> dict:
    """MANUAL: cancel ALL stop-loss / take-profit orders for a symbol (the Reset
    button). Clears the strategy book so a fresh Set doesn't stack a duplicate
    conditional record. Does NOT touch the position itself — it just removes the
    protective orders, leaving the position unprotected until you set new ones."""
    if not _keys_present():
        return {"ok": False, "error": "No Binance API keys configured."}
    try:
        ex = _get_exchange()
        n = _cancel_protective_orders(ex, symbol)
        # Also drop the in-process tracking so the bot won't reference stale ids.
        with _active_lock:
            plan = _active_brackets.get((symbol, "LONG")) or _active_brackets.get((symbol, "SHORT"))
            if plan:
                plan["orders"] = [o for o in plan.get("orders", []) if o.get("role") == "entry"]
                plan["sl_placed"] = False
                plan["tp_placed"] = {"tp1": False, "tp2": False, "tp": False}
        with _account_cache_lock:
            _account_cache["data"] = None
        print(f"[executor][MANUAL] Reset protection {symbol}: cancelled {n} order(s)")
        return {"ok": True, "symbol": symbol, "cancelled": n}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def realized_pnl_history(limit: int = 80) -> dict:
    """Read-only realized-P&L history for the account (covers BOTH bot and
    manual trades — it comes straight from Binance income records)."""
    if not _keys_present():
        return {"ok": False, "error": "No Binance API keys configured.", "trades": []}
    try:
        ex = _get_exchange()
        rows = ex.fapiPrivateGetIncome({"incomeType": "REALIZED_PNL", "limit": limit})
        trades = []
        for it in rows:
            try:
                pnl = float(it.get("income") or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            trades.append({
                "symbol": it.get("symbol"),
                "pnl": pnl,
                "asset": it.get("asset"),
                "time": int(it.get("time") or 0),
            })
        trades.sort(key=lambda x: x["time"], reverse=True)
        return {"ok": True, "trades": trades}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "trades": []}


def realized_pnl_summary(limit: int = 1000) -> dict:
    """Ground-truth account P&L straight from Binance income records — what the
    /performance page reconciles its (simulated) stats against.

    Sums ALL income types in one call and reports the true wallet change:
        net = realized P&L + commission (negative) + funding (±)
    plus the per-trade realized rows and win/loss tally. This is what Binance
    actually shows (fees included), so the page can match the exchange exactly."""
    if not _keys_present():
        return {"ok": False, "error": "No Binance API keys configured."}
    try:
        ex = _get_exchange()
        rows = ex.fapiPrivateGetIncome({"limit": limit})
        realized = commission = funding = 0.0
        trades = []
        day_net: dict[str, float] = {}     # local calendar day → net income that day
        tz = ZoneInfo("Asia/Taipei")
        for it in rows:
            typ = it.get("incomeType")
            try:
                amt = float(it.get("income") or 0.0)
            except (TypeError, ValueError):
                amt = 0.0
            if typ == "REALIZED_PNL":
                realized += amt
                if amt != 0.0:
                    trades.append({"symbol": it.get("symbol"), "pnl": amt,
                                   "time": int(it.get("time") or 0)})
            elif typ == "COMMISSION":
                commission += amt          # Binance reports fees as negative income
            elif typ == "FUNDING_FEE":
                funding += amt             # can be + or -
            else:
                continue
            # Daily buckets over the SAME income types the net figure uses, so the
            # bars sum to `net` exactly (transfers/rebates excluded from both).
            ts = int(it.get("time") or 0)
            if ts:
                day = datetime.fromtimestamp(ts / 1000, tz).strftime("%Y-%m-%d")
                day_net[day] = day_net.get(day, 0.0) + amt
        # Continuous last-14-day series (zero-filled) so the chart shows quiet
        # days as gaps in activity rather than silently skipping them.
        today = datetime.now(tz).date()
        daily = []
        for i in range(13, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            daily.append({"date": d, "net": round(day_net.get(d, 0.0), 4)})
        trades.sort(key=lambda x: x["time"], reverse=True)   # newest first
        pnls = [t["pnl"] for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        n = wins + losses
        gross_win = sum(p for p in pnls if p > 0)
        gross_loss = -sum(p for p in pnls if p < 0)          # positive magnitude
        # Max drawdown over the realized-P&L curve (chronological), in USDT.
        cum = peak = mdd = 0.0
        for t in sorted(trades, key=lambda x: x["time"]):
            cum += t["pnl"]; peak = max(peak, cum); mdd = min(mdd, cum - peak)
        # Current streak: consecutive same-result trades from the most recent.
        streak = 0; streak_type = None
        for p in pnls:
            res = "W" if p > 0 else ("L" if p < 0 else None)
            if res is None:
                continue
            if streak_type is None:
                streak_type, streak = res, 1
            elif res == streak_type:
                streak += 1
            else:
                break
        return {
            "ok": True,
            "realized": round(realized, 4),
            "commission": round(commission, 4),
            "funding": round(funding, 4),
            "net": round(realized + commission + funding, 4),
            "n_trades": n, "wins": wins, "losses": losses,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "gross_win": round(gross_win, 4),
            "gross_loss": round(gross_loss, 4),
            "profit_factor": (round(gross_win / gross_loss, 2) if gross_loss > 0
                              else (None if gross_win == 0 else float("inf"))),
            "avg_win": round(gross_win / wins, 4) if wins else 0.0,
            "avg_loss": round(-gross_loss / losses, 4) if losses else 0.0,
            "expectancy": round(realized / n, 4) if n else 0.0,
            "max_drawdown": round(mdd, 4),
            "streak": streak, "streak_type": streak_type,
            "best": round(max(pnls), 4) if pnls else 0.0,
            "worst": round(min(pnls), 4) if pnls else 0.0,
            "daily": daily,
            "trades": trades[:80],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
