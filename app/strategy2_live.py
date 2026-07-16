"""
Strategy 2 — LIVE execution layer (opt-in, OFF by default).

The S2 scanner (strategy2_scanner.py) is alert-only. This module is the ONLY
place that can turn a Strategy-2 signal into a real order, and it does nothing
unless STRATEGY2_LIVE is explicitly enabled. With the default config the scanner
behaves exactly as before — no order is ever placed.

When enabled, on each NEW signal the scanner hands it here and we:
  1. Apply the "best plan" filter — high conviction only (score ≥ MIN for a long,
     ≤ 100-MIN for a short) AND in the most-liquid tradeable tier. No BTC-regime
     filter by design: a strong signal trades even against Bitcoin.
  2. Enforce ONE-ENGINE-AT-A-TIME — refuse to place an order while the S1 bot
     (bot.lock held by a live PID) is running, so the ~25 USDT account is only
     ever driven by a single strategy.
  3. Respect the shared executor caps (MAX_CONCURRENT_POSITIONS, no duplicate
     position per symbol).
  4. Size an ATR bracket (stop capped at MAX_SL_PCT, targets at 1R/2R — the same
     maths as the S1 trade plan) and place a real bracketed MARKET order via the
     shared executor. The SL + TP rest ON the exchange, so the position stays
     protected even if this scanner process dies.

The global LIVE_TRADING gate still applies underneath: with LIVE_TRADING=false the
executor logs the order as a dry-run and sends nothing to Binance.
"""
import os
import time

import config
import executor
import telegram_utils

ATR_PERIOD = 14
BOT_LOCK_FILE = os.path.join(os.path.dirname(__file__), "bot.lock")

# Telegram warn de-dupe so a running S1 bot doesn't spam a "skipped" note every signal.
_last_warn: dict = {}


def _warn_once(key: str, msg: str, cooldown: int = 3600) -> None:
    now = time.time()
    if now - _last_warn.get(key, 0) >= cooldown:
        _last_warn[key] = now
        try:
            telegram_utils.send_message(msg)
        except Exception:  # noqa: BLE001 — a failed alert must never block trading logic
            pass


def s1_bot_running() -> bool:
    """True if the S1 bot (bot.py) is currently running, detected via its PID lock
    file. We refuse to place live S2 orders while S1 is alive so the 25 USDT
    account is only ever driven by ONE engine. Errs on the side of caution: an
    unreadable lock or an alive-but-unsignalable PID both count as 'running'."""
    try:
        with open(BOT_LOCK_FILE, "r", encoding="utf-8") as f:
            pid = int((f.read() or "0").strip())
    except (FileNotFoundError, ValueError):
        return False
    except Exception:  # noqa: BLE001
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)          # signal 0 = liveness probe, sends nothing
    except ProcessLookupError:
        return False             # no such process → S1 is not running
    except PermissionError:
        return True              # process exists (owned by another user) → alive
    except Exception:  # noqa: BLE001
        return False
    return True


def _atr(ohlcv, period: int = ATR_PERIOD):
    """Simple-average True Range over the closed candles (same maths as
    bot.calculate_atr). Returns None when there is not enough history."""
    if not ohlcv or len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlcv)):
        high = float(ohlcv[i][2])
        low = float(ohlcv[i][3])
        prev_close = float(ohlcv[i - 1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def trade_levels(price, is_long: bool, atr, *, sl_mult=None, tp1_r=None, tp2_r=None):
    """entry / sl / tp1 / tp2 for a market S2 entry. ATR stop (config.ATR_SL_MULTIPLIER)
    capped at config.MAX_SL_PCT, targets at 1R / 2R — mirrors the S1 trade plan so the
    risk:reward is genuine per coin. entry is the signal price (filled at market).
    The keyword overrides exist for premium_trade_levels below; live execution
    always calls this with the plain defaults."""
    entry = float(price)
    sl_mult = sl_mult if sl_mult is not None else config.ATR_SL_MULTIPLIER
    tp1_r = tp1_r if tp1_r is not None else config.ATR_TP1_MULTIPLIER
    tp2_r = tp2_r if tp2_r is not None else config.ATR_TP_MULTIPLIER
    if atr:
        sl_distance = atr * sl_mult
        sl = entry - sl_distance if is_long else entry + sl_distance
    else:
        sl = entry * (1 - config.FIXED_SL_PCT) if is_long else entry * (1 + config.FIXED_SL_PCT)

    # Cap the stop distance so one bad coin can't risk more than MAX_SL_PCT.
    if is_long and (entry - sl) / entry > config.MAX_SL_PCT:
        sl = entry * (1 - config.MAX_SL_PCT)
    elif (not is_long) and (sl - entry) / entry > config.MAX_SL_PCT:
        sl = entry * (1 + config.MAX_SL_PCT)

    # Guard against a degenerate stop on the wrong side of entry.
    if is_long and sl >= entry:
        sl = entry * (1 - config.MAX_SL_PCT)
    elif (not is_long) and sl <= entry:
        sl = entry * (1 + config.MAX_SL_PCT)

    risk = abs(entry - sl)
    if is_long:
        tp1 = entry + risk * tp1_r
        tp2 = entry + risk * tp2_r
    else:
        tp1 = entry - risk * tp1_r
        tp2 = entry - risk * tp2_r
    return entry, sl, tp1, tp2


def premium_trade_levels(price, is_long: bool, atr):
    """The ⭐ premium plan published to the Signals topic — SL 2×ATR, TP1 at
    0.75R, TP2 2R runner (config-tunable). Chosen from the 60d replay grid:
    58.7% of gate-passing signals reached this TP1 before the stop, vs ~50%
    for the old 1R-on-1.5×ATR plan. Alert/page/outcome-tracker only — live
    execution keeps the plain trade_levels above."""
    return trade_levels(price, is_long, atr,
                        sl_mult=config.STRATEGY2_PREMIUM_SL_MULT,
                        tp1_r=config.STRATEGY2_PREMIUM_TP1_R,
                        tp2_r=config.STRATEGY2_PREMIUM_TP2_R)


def passes_filter(direction: str, score: int, rank=None) -> tuple[bool, str]:
    """The 'best plan' gate. Returns (ok, reason_if_skipped).

    LONG: score must be ≥ STRATEGY2_LIVE_MIN_SCORE.
    SHORT: score must be ≤ (100 − STRATEGY2_LIVE_MIN_SCORE), and ENABLE_SHORTS on.
    Both: the symbol must rank inside the tradeable, liquid tier (rank < TOP_N).
    No BTC-regime filter is applied — by design (high-conviction signals only)."""
    min_score = config.STRATEGY2_LIVE_MIN_SCORE
    if direction == "long":
        if score < min_score:
            return False, f"score {score} < {min_score} (long conviction floor)"
    elif direction == "short":
        if not config.ENABLE_SHORTS:
            return False, "shorts disabled (ENABLE_SHORTS=false)"
        short_ceiling = 100 - min_score
        if score > short_ceiling:
            return False, f"score {score} > {short_ceiling} (short conviction ceiling)"
    else:
        return False, f"unknown direction {direction!r}"

    if rank is not None and rank >= config.STRATEGY2_LIVE_TOP_N:
        return False, f"rank {rank} outside top {config.STRATEGY2_LIVE_TOP_N} (illiquid)"
    return True, ""


def maybe_trade(sig: dict, ohlcv, rank=None):
    """Entry point the scanner calls on every NEW signal. A pure no-op unless
    STRATEGY2_LIVE is enabled. Returns the executor plan dict if an order was
    placed (or dry-run logged), else None."""
    if not config.STRATEGY2_LIVE:
        return None

    symbol = sig["symbol"]
    direction = sig["direction"]            # "long" / "short"
    score = int(sig.get("score", 0))
    base = sig.get("base", symbol)

    ok, why = passes_filter(direction, score, rank)
    if not ok:
        print(f"[s2-live] skip {base} {direction}: {why}")
        return None

    # One engine at a time — never trade S2 live while the S1 bot is running.
    if s1_bot_running():
        msg = (f"⛔ S2 LIVE skipped {base} {direction.upper()} (score {score}) — "
               f"the S1 bot is running. Run ONE engine at a time: stop S1 to let "
               f"Strategy 2 trade live.")
        print(f"[s2-live] {msg}")
        _warn_once("s1_running", msg)
        return None

    if not executor.has_capacity():
        print(f"[s2-live] skip {base}: concurrency cap full "
              f"({executor.concurrent_commitments()}/{config.MAX_CONCURRENT_POSITIONS})")
        return None
    # Fail CLOSED on an unreadable account — a transient snapshot failure must
    # never let us stack a 2nd position on a symbol we already hold (which would
    # double real exposure). Only once the account reads cleanly do we trust the
    # duplicate-position guard below.
    if not executor.account_snapshot().get("ok"):
        print(f"[s2-live] skip {base}: account snapshot unavailable — failing closed (no duplicate risk)")
        return None
    if executor.has_open_position(symbol):
        print(f"[s2-live] skip {base}: already holding a position")
        return None

    is_long = direction == "long"
    atr = _atr(ohlcv)
    entry, sl, tp1, tp2 = trade_levels(sig["price"], is_long, atr)
    exec_dir = "LONG" if is_long else "SHORT"

    print(f"[s2-live] PLACING {exec_dir} {base} score {score} | entry {entry:.6g} "
          f"SL {sl:.6g} TP {tp2:.6g}")
    # NOTE: S2 live always enters at MARKET via open_trade (taker). The account's
    # USE_POST_ONLY_ENTRY / USE_RESTING_ORDERS flags apply only to the S1 resting
    # path and are intentionally NOT used here — the scanner has no resting-fill
    # callback, so a maker-only limit could sit unfilled or unmanaged. The fee
    # delta is a few cents on this account; entry_mode is surfaced in status().
    plan = executor.open_trade(
        symbol, exec_dir, entry, sl, tp1, tp2,
        config.STRATEGY2_LIVE_LIGHTS, True,         # lights, aligned → position size
        notify=config.LIVE_ORDER_NOTIFY, manage="bracket",
    )
    if plan and plan.get("error"):
        print(f"[s2-live] order error {base}: {plan['error']}")
    return plan


def status() -> dict:
    """Read-fresh live-execution status for the /strategy2 dashboard rule panel.
    Reads the .env file directly (not the import-time cache) so the panel reflects
    what the scanner will do on its next start, even if env changed since boot."""
    def _flag(name: str, default: str = "false") -> bool:
        return (config.read_env_var(name, default) or default).strip().lower() in (
            "1", "true", "yes", "on")

    def _num(name: str, default, cast):
        try:
            return cast(config.read_env_var(name, str(default)))
        except (TypeError, ValueError):
            return default

    min_score = _num("STRATEGY2_LIVE_MIN_SCORE", config.STRATEGY2_LIVE_MIN_SCORE, int)
    return {
        "enabled": _flag("STRATEGY2_LIVE"),
        "live_master": _flag("LIVE_TRADING"),
        "testnet": _flag("USE_TESTNET", "true"),
        "entry_mode": "market",                 # S2 live enters at market (taker), not post-only/resting
        "s1_running": s1_bot_running(),
        "min_score": min_score,
        "short_ceiling": 100 - min_score,
        "leverage": _num("LEVERAGE", config.LEVERAGE, int),
        "margin_usdt": _num("FIXED_MARGIN_USDT", config.FIXED_MARGIN_USDT, float),
        "max_margin_usdt": _num("MAX_MARGIN_USDT", config.MAX_MARGIN_USDT, float),
        "max_concurrent": _num("MAX_CONCURRENT_POSITIONS", config.MAX_CONCURRENT_POSITIONS, int),
        "top_n": _num("STRATEGY2_LIVE_TOP_N", config.STRATEGY2_LIVE_TOP_N, int),
    }
