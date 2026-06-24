"""
paper_s4.py — live FORWARD paper-trade of any backtester strategy.

Historically this only forward-tested Strategy 4; it now drives ANY strategy in
`backtest.STRATEGIES` (S1 Wolf Confluence, S2 Squeeze Breakout, S3 Trend+Trailing —
S4/S5 were removed 2026-06-23). The active strategy is chosen on the /paper page
and stored in `paper_active.json`; each strategy keeps its OWN forward track record
in `paper_state_<key>.json`, so switching back and forth never mixes histories.

It reuses each strategy's exact backtest decision engine (`STRATEGIES[key]["fn"]`)
and replicates its trade management incrementally as new 1h candles close:
  • trailing strategies (sim is simulate_trade_trailing) → Chandelier ATR trail.
  • bracket  strategies (sim is simulate_trade)          → TP1(50%)+breakeven
    runner + TP2(50%), mirroring backtest.simulate_trade.
So the live forward numbers track the backtest while running in real time.

No real orders are ever placed and nothing touches the live bot's database. All
state lives in the per-strategy JSON files next to this file.

A background thread in app.py calls `tick()` on a timer; the /paper page reads
`get_state()`.
"""
import os
import json
import time
import threading
from datetime import datetime, timezone, timedelta

import backtest as BT
import bot
import config as C

# ── config (env-overridable) ────────────────────────────────────────────────
TF = "1h"                                                  # scan/entry timeframe
UNIVERSE = int(os.getenv("PAPER_S4_UNIVERSE", "30"))        # top-N by volume to scan
ENTRY_DAYS = int(os.getenv("PAPER_S4_ENTRY_DAYS", "20"))    # 1h history to pull/manage on
REGIME_DAYS = int(os.getenv("PAPER_S4_REGIME_DAYS", "20"))  # BTC 1h history for the regime

# Which strategy the /paper page forward-tests when none is explicitly chosen
# (paper_active.json missing/unknown). S4 was the old default but was REMOVED
# 2026-06-22 (lost the bake-off), so the dry-run now falls back to S1 ("default").
DEFAULT_STRATEGY = "default"
ACTIVE_FILE = os.path.join(os.path.dirname(__file__), "paper_active.json")

# ── fixed-notional account model (mirrors the real Binance plan) ─────────────
# Each trade uses a FIXED margin × leverage = fixed notional, so the dollar P&L
# is simply notional × pnl%. This is exactly what makes a real account track the
# backtest's "sum of pnl%": equal size on every trade. Leverage only sets how
# much of the notional is locked as margin — the pnl% (and thus the dollar
# result) is the same. All env-overridable.
START_BALANCE = float(os.getenv("PAPER_S4_START_BALANCE", "100"))  # account seed (USDT)
MARGIN_USDT   = float(os.getenv("PAPER_S4_MARGIN", "3"))           # margin locked per trade
LEVERAGE      = float(os.getenv("PAPER_S4_LEVERAGE", "3"))         # isolated leverage
NOTIONAL_USDT = MARGIN_USDT * LEVERAGE                             # 9 USDT — pnl% applies here
MAX_CONCURRENT = int(START_BALANCE // MARGIN_USDT) if MARGIN_USDT else 0  # margin ceiling

_TW = timezone(timedelta(hours=8))   # match the rest of the app (Taiwan time)
_tick_lock = threading.Lock()
_io_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, _TW).strftime("%Y-%m-%d %H:%M:%S")


def _tf_ms() -> int:
    return BT.ex.parse_timeframe(TF) * 1000


# ── active-strategy selection ────────────────────────────────────────────────
def _manage_for(key: str) -> str:
    """'trailing' if the strategy's backtest simulator is the Chandelier trail,
    otherwise 'bracket' (TP1/breakeven runner/TP2). Drives _advance/_new_trade."""
    sim = BT.STRATEGIES.get(key, {}).get("sim")
    return "trailing" if sim is BT.simulate_trade_trailing else "bracket"


def get_active() -> str:
    """The strategy key the /paper page is currently forward-testing."""
    try:
        with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
            key = json.load(f).get("strategy")
        if key in BT.STRATEGIES:
            return key
    except Exception:
        pass
    return DEFAULT_STRATEGY


def set_active(key: str) -> str:
    """Switch which strategy the dry-run engine ticks/displays. Raises on unknown
    key. Each strategy's own state file is preserved, so switching is reversible."""
    if key not in BT.STRATEGIES:
        raise ValueError(f"unknown strategy {key!r}")
    with _io_lock:
        tmp = ACTIVE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"strategy": key}, f)
        os.replace(tmp, ACTIVE_FILE)
    return key


def _state_file(key: str) -> str:
    return os.path.join(os.path.dirname(__file__), f"paper_state_{key}.json")


# ── persistence (atomic, per-strategy) ───────────────────────────────────────
def _blank_state() -> dict:
    return {
        "open": [],            # PENDING or OPEN trades
        "closed": [],          # CLOSED or CANCELLED trades (newest appended)
        "last_signal": {},     # symbol -> signal-candle ts (dedupe re-entries)
        "tick_count": 0,
        "started": _iso(_now_ms()),
        "last_tick": None,
        "last_tick_ms": None,
        "btc_regime": "neutral",
        "universe": UNIVERSE,
        "last_error": None,
    }


def _load_state(key: str) -> dict:
    path = _state_file(key)
    with _io_lock:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    s = json.load(f)
                for k, v in _blank_state().items():
                    s.setdefault(k, v)
                return s
            except Exception:
                pass
        return _blank_state()


def _save_state(key: str, state: dict) -> None:
    path = _state_file(key)
    with _io_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)


# ── trade lifecycle ──────────────────────────────────────────────────────────
def _new_trade(sym: str, res, sig_ts: int, window, manage: str) -> dict:
    is_long, entry, sl, tp1, tp2, eff = res
    t = {
        "id": f"{sym.split('/')[0]}-{sig_ts}",
        "symbol": sym,
        "sym": sym.split("/")[0],
        "dir": "LONG" if is_long else "SHORT",
        "manage": manage,
        "entry": float(entry), "sl": float(sl),
        "tp1": float(tp1), "tp2": float(tp2),
        "status": "PENDING",
        "signal_ts": int(sig_ts), "signal_time": _iso(sig_ts),
        "wait_deadline_ts": int(sig_ts) + BT.MAX_WAIT_BARS * _tf_ms(),
        "fill_ts": None, "fill_time": None,
        "stop": float(sl), "peak": float(entry), "trough": float(entry),
        "last_bar_ts": int(sig_ts),     # candles up to & incl. signal are "seen"
        "exit": None, "exit_ts": None, "exit_time": None,
        "pnl_pct": None, "R": None, "rr": None, "win": None,
        "exit_reason": None,
    }
    if manage == "trailing":
        # Trailing distance is set from the ATR as of the signal bar — exactly what
        # simulate_trade_trailing does at the fill bar (same candles available here).
        atr = bot.calculate_atr(window)
        if not atr or atr <= 0:
            atr = abs(entry - sl) / max(C.ATR_SL_MULTIPLIER, 0.5)
        t["trail"] = float(atr * BT.CHANDELIER_ATR_MULT)
    else:
        # Bracket runner state (mirrors backtest.simulate_trade's partial logic).
        t["partial"] = False
        t["tp1_pct"] = None
    return t


def _close(t: dict, price: float, ts: int, reason: str, gross_pct: float = None) -> None:
    """Close a trade. gross_pct overrides the price-derived gross % — used by the
    bracket runner whose realised P&L is a 50/50 blend (TP1 half + TP2/breakeven
    half), not a single exit price. The round-trip fee is always applied once."""
    is_long = t["dir"] == "LONG"
    entry = t["entry"]
    if gross_pct is None:
        gross_pct = ((price - entry) / entry * 100) if is_long else ((entry - price) / entry * 100)
    pnl = gross_pct - BT.FEE_PCT                         # realistic round-trip cost
    R = abs(entry - t["sl"]) / entry * 100
    t["status"] = "CLOSED"
    t["exit"] = round(float(price), 8)
    t["exit_ts"] = int(ts)
    t["exit_time"] = _iso(ts)
    t["pnl_pct"] = round(pnl, 3)
    t["pnl_usd"] = round(NOTIONAL_USDT * pnl / 100, 4)   # fixed-notional dollar result
    t["R"] = round(R, 3)
    t["rr"] = round(pnl / R, 3) if R else 0.0
    t["win"] = bool(pnl > 0)
    t["exit_reason"] = reason


def _advance(t: dict, ohlcv) -> None:
    """Walk an open/pending trade forward over any candles newer than it has seen.
    Mutates the trade in place. Dispatches on t['manage']: 'trailing' mirrors
    simulate_trade_trailing (ratcheting ATR stop), 'bracket' mirrors simulate_trade
    (TP1 50% + breakeven runner + TP2), one closed candle at a time."""
    if t.get("manage") == "bracket":
        _advance_bracket(t, ohlcv)
    else:
        _advance_trailing(t, ohlcv)


def _advance_trailing(t: dict, ohlcv) -> None:
    is_long = t["dir"] == "LONG"
    trail = t["trail"]
    tf_ms = _tf_ms()
    for c in ohlcv:
        ts = int(c[0])
        if ts <= t["last_bar_ts"]:
            continue
        hi, lo, cl = c[2], c[3], c[4]

        if t["status"] == "PENDING":
            if ts > t["wait_deadline_ts"]:
                t["status"] = "CANCELLED"
                t["exit_ts"] = ts; t["exit_time"] = _iso(ts)
                t["exit_reason"] = "entry expired (never filled)"
                t["last_bar_ts"] = ts
                return
            filled = (is_long and lo <= t["entry"]) or ((not is_long) and hi >= t["entry"])
            if filled:
                t["status"] = "OPEN"
                t["fill_ts"] = ts; t["fill_time"] = _iso(ts)
                t["stop"] = t["sl"]; t["peak"] = t["entry"]; t["trough"] = t["entry"]
                # fall through: the fill candle is also checked for a stop, exactly
                # as backtest's `for k in range(fill, end)` starts at the fill bar.
            else:
                t["last_bar_ts"] = ts
                continue

        if t["status"] == "OPEN":
            if is_long:
                if lo <= t["stop"]:
                    _close(t, t["stop"], ts, "trailing stop"); return
                t["peak"] = max(t["peak"], hi)
                t["stop"] = max(t["stop"], t["peak"] - trail)
            else:
                if hi >= t["stop"]:
                    _close(t, t["stop"], ts, "trailing stop"); return
                t["trough"] = min(t["trough"], lo)
                t["stop"] = min(t["stop"], t["trough"] + trail)
            if (ts - t["fill_ts"]) >= BT.MAX_HOLD_BARS_TRAIL * tf_ms:
                _close(t, cl, ts, "max hold reached"); return
        t["last_bar_ts"] = ts


def _advance_bracket(t: dict, ohlcv) -> None:
    """Incremental port of backtest.simulate_trade: resting-limit fill, then a
    50% TP1 partial, after which the runner is a breakeven stop + hard TP2 at 2R.
    Same conservative same-candle tie-breaks (SL beats TP1; breakeven beats TP2)."""
    is_long = t["dir"] == "LONG"
    entry, sl, tp1, tp2 = t["entry"], t["sl"], t["tp1"], t["tp2"]
    tp1_pct = ((tp1 - entry) / entry * 100) if is_long else ((entry - tp1) / entry * 100)
    tf_ms = _tf_ms()
    for c in ohlcv:
        ts = int(c[0])
        if ts <= t["last_bar_ts"]:
            continue
        hi, lo, cl = c[2], c[3], c[4]

        if t["status"] == "PENDING":
            if ts > t["wait_deadline_ts"]:
                t["status"] = "CANCELLED"
                t["exit_ts"] = ts; t["exit_time"] = _iso(ts)
                t["exit_reason"] = "entry expired (never filled)"
                t["last_bar_ts"] = ts
                return
            # target printed before the entry filled → the move already happened.
            if C.CANCEL_QUEUE_IF_TARGET_HIT and (
                    (is_long and hi >= tp1) or ((not is_long) and lo <= tp1)):
                t["status"] = "CANCELLED"
                t["exit_ts"] = ts; t["exit_time"] = _iso(ts)
                t["exit_reason"] = "target reached before entry"
                t["last_bar_ts"] = ts
                return
            filled = (is_long and lo <= entry) or ((not is_long) and hi >= entry)
            if filled:
                t["status"] = "OPEN"
                t["fill_ts"] = ts; t["fill_time"] = _iso(ts)
                t["stop"] = sl; t["partial"] = False; t["tp1_pct"] = round(tp1_pct, 4)
                # fall through: manage the fill candle too.
            else:
                t["last_bar_ts"] = ts
                continue

        if t["status"] == "OPEN":
            if not t.get("partial"):
                sl_hit = (lo <= sl) if is_long else (hi >= sl)
                tp1_hit = (hi >= tp1) if is_long else (lo <= tp1)
                if sl_hit:                                   # SL wins ambiguous bar
                    _close(t, sl, ts, "stop loss"); return
                if tp1_hit:
                    t["partial"] = True
                    t["stop"] = entry                        # runner stop → breakeven
                    t["last_bar_ts"] = ts
                    continue
            else:
                be_hit = (lo <= entry) if is_long else (hi >= entry)
                tp2_hit = (hi >= tp2) if is_long else (lo <= tp2)
                if be_hit:                                   # breakeven wins ambiguous bar
                    _close(t, entry, ts, "runner stopped at breakeven",
                           gross_pct=tp1_pct * 0.5); return
                if tp2_hit:
                    tp2_pct = ((tp2 - entry) / entry * 100) if is_long else ((entry - tp2) / entry * 100)
                    _close(t, tp2, ts, "TP2 hit",
                           gross_pct=tp1_pct * 0.5 + tp2_pct * 0.5); return
            if (ts - t["fill_ts"]) >= BT.MAX_HOLD_BARS * tf_ms:
                # held to the cap — close at the current close (mark-to-market).
                if t.get("partial"):
                    runner_pct = ((cl - entry) / entry * 100) if is_long else ((entry - cl) / entry * 100)
                    _close(t, cl, ts, "max hold reached", gross_pct=tp1_pct * 0.5 + runner_pct * 0.5)
                else:
                    _close(t, cl, ts, "max hold reached")
                return
        t["last_bar_ts"] = ts


def _closed_candles(ohlcv):
    """Drop the still-forming candle so we only act on closed bars (backtest semantics)."""
    now = _now_ms()
    tf_ms = _tf_ms()
    return [c for c in ohlcv if int(c[0]) + tf_ms <= now]


# ── one scan cycle ───────────────────────────────────────────────────────────
def tick(progress=None) -> dict:
    """Run one paper-trading cycle for the ACTIVE strategy: manage open trades,
    then look for new signals across the universe. Returns a short status dict.
    Safe to call from multiple threads — a second concurrent call is skipped."""
    if not _tick_lock.acquire(blocking=False):
        return {"skipped": "tick already running"}
    try:
        key = get_active()
        strat = BT.STRATEGIES[key]
        eval_fn = strat["fn"]
        manage = _manage_for(key)
        state = _load_state(key)
        syms = BT.top_symbols(UNIVERSE)

        # BTC regime (latest closed bar) gates every direction, like the live bot.
        try:
            btc1h = BT.fetch_ohlcv("BTC/USDT:USDT", "1h", REGIME_DAYS)
            reg_series = BT.btc_regime_series(btc1h)
            btc_regime = reg_series[-1][1] if reg_series else "neutral"
        except Exception:
            btc_regime = state.get("btc_regime", "neutral")

        open_by_sym = {}
        for t in state["open"]:
            open_by_sym.setdefault(t["symbol"], []).append(t)

        opened = 0
        total = len(syms)
        for si, sym in enumerate(syms, 1):
            try:
                oh1 = _closed_candles(BT.fetch_ohlcv(sym, TF, ENTRY_DAYS))
                oh4 = BT.fetch_ohlcv(sym, "4h", ENTRY_DAYS + 10)
            except Exception:
                if progress:
                    progress(si, total, f"{sym.split('/')[0]}: fetch failed")
                continue
            if len(oh1) < BT.WINDOW:
                if progress:
                    progress(si, total, f"{sym.split('/')[0]}: thin history")
                continue
            ema4 = BT.ema_series_4h(oh4)

            # 1) manage trades already on the books for this symbol
            for t in open_by_sym.get(sym, []):
                _advance(t, oh1)

            # 2) look for a NEW entry — one active trade per symbol at a time,
            #    and never more concurrent trades than the account's margin funds.
            active = any(t["status"] in ("PENDING", "OPEN") for t in open_by_sym.get(sym, []))
            live_now = sum(1 for tt in state["open"] if tt["status"] in ("PENDING", "OPEN"))
            has_slot = MAX_CONCURRENT <= 0 or live_now < MAX_CONCURRENT
            if not active and has_slot:
                window = oh1[-BT.WINDOW:]
                sig_ts = int(window[-1][0])
                # dedupe: don't re-fire on a signal candle we've already acted on
                if state["last_signal"].get(sym, 0) < sig_ts:
                    res = eval_fn(window, BT.as_of(ema4, sig_ts, None), btc_regime)
                    if res:
                        trade = _new_trade(sym, res, sig_ts, window, manage)
                        # the brand-new trade may fill/exit within candles already
                        # closed since its signal bar — walk it forward immediately
                        _advance(trade, oh1)
                        state["open"].append(trade)
                        open_by_sym.setdefault(sym, []).append(trade)
                        state["last_signal"][sym] = sig_ts
                        opened += 1
            if progress:
                progress(si, total, f"{sym.split('/')[0]} ✓")

        # sweep finished trades out of the open list into closed
        still_open, newly_closed = [], 0
        for t in state["open"]:
            if t["status"] in ("CLOSED", "CANCELLED"):
                state["closed"].append(t)
                newly_closed += 1
            else:
                still_open.append(t)
        state["open"] = still_open

        state["tick_count"] += 1
        state["last_tick_ms"] = _now_ms()
        state["last_tick"] = _iso(state["last_tick_ms"])
        state["btc_regime"] = btc_regime
        state["universe"] = UNIVERSE
        state["strategy"] = key
        state["last_error"] = None
        _save_state(key, state)
        return {"opened": opened, "closed": newly_closed,
                "open_now": len(state["open"]), "regime": btc_regime, "strategy": key}
    finally:
        _tick_lock.release()


def record_error(msg: str) -> None:
    key = get_active()
    state = _load_state(key)
    state["last_error"] = msg
    state["last_tick_ms"] = _now_ms()
    state["last_tick"] = _iso(state["last_tick_ms"])
    _save_state(key, state)


def reset() -> None:
    """Wipe the ACTIVE strategy's forward record only (other strategies untouched)."""
    _save_state(get_active(), _blank_state())


# ── read model for the web page ──────────────────────────────────────────────
def _summary(closed) -> dict:
    real = [t for t in closed if t.get("status") == "CLOSED"]
    n = len(real)
    wins = [t for t in real if t.get("win")]
    rrs = [t["rr"] for t in real if t.get("rr") is not None]
    pnls = [t["pnl_pct"] for t in real if t.get("pnl_pct") is not None]
    avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else 0.0
    cancelled = sum(1 for t in closed if t.get("status") == "CANCELLED")
    # Fixed-notional dollar accounting: net USD P&L drives the running balance.
    usds = [t.get("pnl_usd", 0.0) for t in real]
    net_usd = round(sum(usds), 4)
    balance = round(START_BALANCE + net_usd, 4)
    return {
        "closed": n,
        "cancelled": cancelled,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "expectancy_r": avg(rrs),
        "total_r": round(sum(rrs), 2),
        "total_return_pct": round(sum(pnls), 2),
        "avg_win_r": avg([t["rr"] for t in wins]),
        "avg_loss_r": avg([t["rr"] for t in real if not t.get("win")]),
        # ── dollar view (what a fixed NOTIONAL_USDT-per-trade account does) ──
        "net_pnl_usd": net_usd,
        "balance": balance,
        "return_pct": round(net_usd / START_BALANCE * 100, 2) if START_BALANCE else 0.0,
        "avg_win_usd": round(avg([t.get("pnl_usd", 0.0) for t in wins]), 4),
        "avg_loss_usd": round(avg([t.get("pnl_usd", 0.0) for t in real if not t.get("win")]), 4),
        "best_usd": round(max(usds), 4) if usds else 0.0,
        "worst_usd": round(min(usds), 4) if usds else 0.0,
    }


def get_state() -> dict:
    key = get_active()
    strat = BT.STRATEGIES[key]
    s = _load_state(key)
    # open trades: newest signal first; closed: newest exit first
    open_sorted = sorted(s["open"], key=lambda t: t.get("signal_ts", 0), reverse=True)
    closed_sorted = sorted(s["closed"], key=lambda t: (t.get("exit_ts") or 0), reverse=True)
    return {
        "running": True,
        "interval": int(os.getenv("PAPER_S4_INTERVAL", "300")),
        "timeframe": TF,
        "strategy": key,
        "strategy_name": strat["name"],
        "strategy_desc": strat["desc"],
        "manage": _manage_for(key),
        "strategies": [{"key": k, "name": v["name"]} for k, v in BT.STRATEGIES.items()],
        "account": {
            "start_balance": START_BALANCE,
            "margin_usdt": MARGIN_USDT,
            "leverage": LEVERAGE,
            "notional_usdt": NOTIONAL_USDT,
            "max_concurrent": MAX_CONCURRENT,
        },
        "universe": s.get("universe", UNIVERSE),
        "tick_count": s.get("tick_count", 0),
        "started": s.get("started"),
        "last_tick": s.get("last_tick"),
        "btc_regime": s.get("btc_regime", "neutral"),
        "last_error": s.get("last_error"),
        "open": open_sorted,
        "closed": closed_sorted[:200],
        "summary": _summary(s["closed"]),
        "open_count": len(s["open"]),
    }
