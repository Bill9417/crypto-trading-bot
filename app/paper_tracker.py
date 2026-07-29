"""
paper_tracker.py — forward, read-only paper-trading of S1 variants.

Places NO real orders, touches NO live trading path. Ticks roughly once an
hour (S1's timeframe), reusing backtest.py's evaluate()/apply_costs() —
which call bot.py's OWN live signal functions, not a duplicate — to decide
whether a fresh signal fired on the latest closed candle, then manages open
paper positions with the SAME bracket rules backtest.simulate_trade()
replays historically (50% at TP1 + breakeven stop on the rest, 50% runner
to TP2), just applied incrementally as real candles close instead of
replayed over a fixed historical array.

WHY THIS EXISTS: the 2026-07-27 walk-forward found S1 net-negative over 360
days (re-confirming 2026-06-22's finding on the earlier 5-light version),
but slicing that SAME data by direction found LONGS positive (+0.123R over
32 trades) while SHORTS carried the whole negative expectancy (-0.184R over
47 trades) — a real pattern, but found by re-slicing the very data used to
judge it, so not yet independently confirmed. This gives both the current
full strategy and the longs-only hypothesis a genuine forward track record
instead of re-slicing history a third time.

Four tracked variants:
    s1_full        — exactly what's live today (longs + shorts)
    s1_longs_only  — same rules, only takes LONG signals
    s1_lowvol      — same rules, only when the SYMBOL's own ATR is below
                     LOWVOL_MAX_ATR_PCT of its price at entry
    s1_lowvol_3r   — that same entry filter PLUS a different exit: no 50%
                     partial, no breakeven move, one wide target at
                     SINGLE_TARGET_R x risk. The best candidate found so far.

The lowvol variant came out of s1_regime_lab.py (2026-07-28): expectancy
rises monotonically as the volatility ceiling tightens (+0.03R at <1.5% →
+0.13R at <1.2% → +0.17R at <1.0% → +0.28R at <0.8%), so it's a plateau
rather than one lucky threshold; the surviving trades span 31 different
symbols (so it is NOT a disguised "only trade BTC/ETH" filter); and BOTH
directions turn positive once it's applied — which is why longs_only is
kept as a control rather than promoted. The direction effect looks like a
proxy: shorts were simply taken on more volatile coins more often.

s1_lowvol_3r adds the exit half, from s1_exit_lab.py (2026-07-29). Holding
entries fixed and varying only the exit, S1's live bracket caps EVERY
winner at 1.5R gross while losers cost a full ~1.05R — in 127 trades it has
never once returned more than 1.44R. On the low-vol subset all 11 tested
exit rules turn positive, and a single wide target scores best (+0.231R vs
+0.080R for the current bracket on the same 50 trades).

⚠️ NONE of this is proof. ~127 trades, ~30 gates and exit rules tested;
re-running the same collection a day apart moved the baseline by 0.07R on
universe drift alone. That is why these run forward here instead of being
switched on live — the whole point is to stop trusting re-sliced history.

One deliberate difference from backtest.simulate_trade: a position that
never hits SL/TP1 within the hold cap is FORCE-CLOSED at mark-to-market
here, not silently dropped. Dropping is fine for a fixed historical replay
(the trade "didn't happen" in the sample); a live forward tracker must give
every opened paper trade an actual recorded outcome, or its own win-rate
would be quietly survivorship-biased by excluding the trades that just sat
there.

Wired into bot.py's existing 1-minute scheduler (self-throttled to hourly
here) exactly like s1_bybit_mirror.guardian_tick() — try/except-wrapped at
the call site so a bug in here can never touch a real trade.
"""
import json
import os
import time

import backtest as BT
import market_data

STATE_FILE = os.path.join(os.path.dirname(__file__), "paper_tracker_state.json")

TICK_SEC = 3600              # once per closed 1h candle — no point checking faster
FETCH_DAYS = 14               # enough real history for evaluate()'s 260-candle window
N_CANDIDATES = 100
TOP_N_TRADE = 30              # today's top-N by volume (forward tracking doesn't need
                               # walk_forward.py's point-in-time correction — "today's
                               # top-N" genuinely IS the live universe right now)
MAX_WAIT_BARS = BT.MAX_WAIT_BARS      # same as the historical sim
MAX_HOLD_BARS = BT.MAX_HOLD_BARS

VARIANTS = ("s1_full", "s1_longs_only", "s1_lowvol", "s1_lowvol_3r")

# Ceiling for the lowvol variants: the symbol's own ATR(14) as a % of its
# price at entry. 1.0 sits in the middle of the tested plateau (0.8-1.2 all
# improved 4/6 folds) rather than at its best-scoring edge (0.6 scored
# highest but on only 10 trades) — deliberately not tuned to the peak.
LOWVOL_MAX_ATR_PCT = 1.0

# s1_lowvol_3r's single target, in multiples of the trade's own risk
# (|entry - stop|). From s1_exit_lab: on low-vol entries EVERY tested exit
# rule turned positive, and a single wide target scored highest (+0.231R vs
# +0.080R for S1's current bracket on the same 50 trades).
SINGLE_TARGET_R = 3.0

_last_tick = 0.0


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt = fresh start
        d = {}
    d.setdefault("variants", {})
    for v in VARIANTS:
        d["variants"].setdefault(v, {"open": {}, "closed": []})
    return d


def _save(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _wants(variant: str, is_long: bool, oh: list) -> bool:
    """Does `variant` take this signal? `oh` is the same candle window
    evaluate() just judged, so the volatility read is as-of the entry bar."""
    if variant == "s1_longs_only":
        return is_long
    if variant in ("s1_lowvol", "s1_lowvol_3r"):
        atr_pct = BT._atr_pct(oh)
        return atr_pct is not None and atr_pct < LOWVOL_MAX_ATR_PCT
    return True


def _drop_tradfi_perps(symbols):
    markets = getattr(BT.ex, "markets", None) or {}
    if not markets:
        return symbols
    return [s for s in symbols if not market_data.is_tradfi_market(markets.get(s))]


def _fill_pending(pos: dict, oh1h: list):
    """Advance a not-yet-filled resting entry against new candles. Returns
    'filled' / 'expired' / None (still pending, keep waiting)."""
    is_long = pos["dir"] == "LONG"
    for c in oh1h:
        if c[0] <= pos["last_ts"]:
            continue
        pos["last_ts"] = c[0]
        pos["wait_bars"] += 1
        hi, lo = c[2], c[3]
        if hi >= pos["tp1"] if is_long else lo <= pos["tp1"]:
            return "expired"        # target already printed — the resting order cancels
        if (lo <= pos["entry"]) if is_long else (hi >= pos["entry"]):
            pos["opened_ts"] = c[0]
            return "filled"
        if pos["wait_bars"] >= MAX_WAIT_BARS:
            return "expired"
    return None


def _manage_open(pos: dict, oh1h: list):
    """Advance an open position. Returns a closed-trade dict once it should
    exit, else None (still open)."""
    is_long = pos["dir"] == "LONG"
    entry, sl, tp1, tp2 = pos["entry"], pos["sl"], pos["tp1"], pos["tp2"]
    tp1_pct = ((tp1 - entry) / entry * 100) if is_long else ((entry - tp1) / entry * 100)
    single = pos.get("exit") == "single"
    for c in oh1h:
        if c[0] <= pos["last_ts"]:
            continue
        pos["last_ts"] = c[0]
        pos["bars"] += 1
        hi, lo = c[2], c[3]
        if single:
            # One target, no partial, no breakeven move — the whole position
            # lives or dies on SL vs a single wide target (s1_exit_lab's
            # "single target 3R"). Stop is checked first: on a bar that spans
            # both, assume the worse fill.
            if (lo <= sl) if is_long else (hi >= sl):
                pnl = ((sl - entry) / entry * 100) if is_long else ((entry - sl) / entry * 100)
                return _close(pos, pnl, c[0], "sl")
            if (hi >= tp2) if is_long else (lo <= tp2):
                pnl = ((tp2 - entry) / entry * 100) if is_long else ((entry - tp2) / entry * 100)
                return _close(pos, pnl, c[0], "target")
            if pos["bars"] >= MAX_HOLD_BARS:
                mtm = ((c[4] - entry) / entry * 100) if is_long else ((entry - c[4]) / entry * 100)
                return _close(pos, mtm, c[0], "time")
            continue
        if not pos["partial"]:
            sl_hit = (lo <= sl) if is_long else (hi >= sl)
            if sl_hit:
                pnl = ((sl - entry) / entry * 100) if is_long else ((entry - sl) / entry * 100)
                return _close(pos, pnl, c[0], "sl")
            if (hi >= tp1) if is_long else (lo <= tp1):
                pos["partial"] = True
        else:
            if (lo <= entry) if is_long else (hi >= entry):
                return _close(pos, tp1_pct * 0.5, c[0], "breakeven")
            if (hi >= tp2) if is_long else (lo <= tp2):
                tp2_pct = ((tp2 - entry) / entry * 100) if is_long else ((entry - tp2) / entry * 100)
                return _close(pos, tp1_pct * 0.5 + tp2_pct * 0.5, c[0], "tp2")
        if pos["bars"] >= MAX_HOLD_BARS:
            mtm = ((c[4] - entry) / entry * 100) if is_long else ((entry - c[4]) / entry * 100)
            pnl = tp1_pct * 0.5 + mtm * 0.5 if pos["partial"] else mtm
            return _close(pos, pnl, c[0], "time")
    return None


def _close(pos, pnl_pct, ts, reason) -> dict:
    pnl = BT.apply_costs(pnl_pct, pos["bars"], 1.0, realism=True)
    R = abs(pos["entry"] - pos["sl"]) / pos["entry"] * 100
    return {"symbol": pos["symbol"], "dir": pos["dir"], "lights": pos["lights"],
            "opened_ts": pos.get("opened_ts"), "closed_ts": ts, "reason": reason,
            "pnl": round(float(pnl), 2), "rr": round(float(pnl) / R, 2) if R else 0.0,
            "win": bool(pnl > 0)}


def _fetch_universe(progress=lambda *a: None):
    """{symbol: {'oh1h': [...], 'ema4h': [(ts,val)...]}} for today's tradeable
    top-N, plus BTC's regime series."""
    candidates = _drop_tradfi_perps(BT.top_symbols(N_CANDIDATES))[:TOP_N_TRADE]
    btc1h = BT.fetch_ohlcv("BTC/USDT:USDT", "1h", FETCH_DAYS + 5)
    btc_reg = BT.btc_regime_series(btc1h)
    data = {}
    for sym in candidates:
        try:
            oh1h = BT.fetch_ohlcv(sym, "1h", FETCH_DAYS)
            oh4h = BT.fetch_ohlcv(sym, "4h", FETCH_DAYS + 10)
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the tick
            progress(f"[paper-tracker] {sym} fetch failed: {exc}")
            continue
        if len(oh1h) < BT.WINDOW + 2:
            continue
        data[sym] = {"oh1h": oh1h, "ema4h": BT.ema_series_4h(oh4h)}
    return data, btc_reg


def tick(force=False, progress=lambda *a: None) -> bool:
    """Self-throttled to TICK_SEC. Returns True if it actually ran."""
    global _last_tick
    now = time.time()
    if not force and now - _last_tick < TICK_SEC:
        return False
    _last_tick = now

    state = _load()
    data, btc_reg = _fetch_universe(progress)
    if not data:
        return False

    for variant in VARIANTS:
        book = state["variants"][variant]

        # 1) advance pending (not-yet-filled) entries
        for sym in list(book.get("pending", {}) or {}):
            pos = book["pending"][sym]
            oh1h = data.get(sym, {}).get("oh1h")
            if not oh1h:
                continue
            result = _fill_pending(pos, oh1h)
            if result == "filled":
                book.setdefault("open", {})[sym] = pos
                del book["pending"][sym]
            elif result == "expired":
                del book["pending"][sym]

        # 2) advance open positions
        for sym in list(book.get("open", {}) or {}):
            pos = book["open"][sym]
            oh1h = data.get(sym, {}).get("oh1h")
            if not oh1h:
                continue
            closed = _manage_open(pos, oh1h)
            if closed:
                book["closed"].append(closed)
                del book["open"][sym]

        # 3) look for fresh entries (flat symbols only)
        busy = set(book.get("open", {})) | set(book.get("pending", {}))
        for sym, d in data.items():
            if sym in busy:
                continue
            oh1h, ema4h = d["oh1h"], d["ema4h"]
            oh = oh1h[-BT.WINDOW:]
            if len(oh) < BT.WINDOW:
                continue
            ts = oh1h[-1][0]
            res = BT.evaluate(oh, BT.as_of(ema4h, ts, None), BT.as_of(btc_reg, ts, "neutral"))
            if not res:
                continue
            is_long, entry, sl, tp1, tp2, eff = res
            if not _wants(variant, is_long, oh):
                continue
            entry, sl, tp1, tp2 = float(entry), float(sl), float(tp1), float(tp2)
            exit_style = "single" if variant == "s1_lowvol_3r" else "bracket"
            if exit_style == "single":
                # One wide target at SINGLE_TARGET_R x the trade's own risk.
                # tp1 stays as-is: _fill_pending uses it for the "target
                # printed before the entry filled -> cancel" rule, which is
                # about the resting order, not the exit plan.
                risk = abs(entry - sl)
                tp2 = entry + (risk * SINGLE_TARGET_R if is_long
                               else -risk * SINGLE_TARGET_R)
            book.setdefault("pending", {})[sym] = {
                "symbol": sym.split("/")[0], "dir": "LONG" if is_long else "SHORT",
                "lights": int(eff), "entry": entry, "sl": sl,
                "tp1": tp1, "tp2": tp2, "exit": exit_style,
                "last_ts": ts, "wait_bars": 0, "bars": 0, "partial": False,
            }

    state["last_tick"] = now
    _save(state)
    return True


def stats(variant: str) -> dict:
    """summarize()-shaped stats for one variant's closed trades."""
    closed = _load()["variants"].get(variant, {}).get("closed", [])
    trades = [{"pnl": t["pnl"], "rr": t["rr"], "win": t["win"], "ts": t["closed_ts"],
               "dir": t["dir"], "lights": t["lights"]} for t in closed]
    return BT.summarize(trades, 0.0, 0.0, 0)


def report_tg() -> str:
    """Telegram-formatted status for the /paper command."""
    state = _load()
    lines = ["📝 S1 forward paper-tracker (no real money)"]
    for variant in VARIANTS:
        book = state["variants"].get(variant, {})
        n_open = len(book.get("open", {}))
        n_pending = len(book.get("pending", {}))
        s = stats(variant)
        label = {
            "s1_full": "S1 (full, as live)",
            "s1_longs_only": "S1 longs-only (control)",
            "s1_lowvol": f"S1 low-vol only (<{LOWVOL_MAX_ATR_PCT:g}% ATR)",
            "s1_lowvol_3r": f"S1 low-vol + {SINGLE_TARGET_R:g}R single target",
        }.get(variant, variant)
        if s["total"] == 0:
            lines.append(f"\n{label}: no closed trades yet ({n_open} open, {n_pending} pending)")
            continue
        lines.append(f"\n{label}: {s['total']} closed · WR {s['win_rate']}% · "
                     f"E[R] {s['expectancy_r']:+.3f} · total {s['total_r']:+.2f}R "
                     f"({n_open} open, {n_pending} pending)")
    return "\n".join(lines)
