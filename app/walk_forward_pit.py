"""Survivorship-corrected walk-forward validation of Strategy 1 (point-in-time).

walk_forward.py / rank_strategies.py / backtest.py all draw the universe from
top_symbols() = TODAY's top-volume coins replayed backward. That is survivorship-
biased: it only ever tests coins that survived and are popular NOW, so every number
is optimistic.

This fixes the tractable half of that bias — selection-by-current-popularity. From a
large candidate pool of currently-listed perps, each fold uses the top-N coins ranked
by their trailing quote-volume AS OF THAT FOLD'S START (computed from the candles),
not today's ranking. A coin not yet listed / illiquid back then is naturally excluded
from that fold; a coin that was big then but has faded IS included.

Residual bias (documented, not fixed): the candidate pool is still drawn from
currently-LISTED symbols, so coins fully delisted before today can't be included —
Binance won't serve their klines. Fully removing that needs an external historical
symbol list (incl. delistings).

Usage:   python walk_forward_pit.py [total_days] [n_folds] [universe_n] [pool] [tf]
Default: 360 days, 6 folds, 30 per fold, pool 70, 1h.
"""
import sys
import time
from datetime import datetime, timezone

import backtest as BT

total_days = int(sys.argv[1]) if len(sys.argv) > 1 else 360
n_folds    = int(sys.argv[2]) if len(sys.argv) > 2 else 6
uni_n      = int(sys.argv[3]) if len(sys.argv) > 3 else 30
pool_n     = int(sys.argv[4]) if len(sys.argv) > 4 else 70
tf         = sys.argv[5] if len(sys.argv) > 5 else "1h"

eval_fn = BT.STRATEGIES["default"]["fn"]      # Strategy 1
sim_fn  = BT.STRATEGIES["default"]["sim"]
tf_hours = BT.ex.parse_timeframe(tf) / 3600.0
VOL_LOOKBACK_MS = 30 * 24 * 3600 * 1000       # trailing window for the point-in-time rank
MIN_BARS_LISTED = 24 * 7                       # need ≥~7d of hourly bars before a date to qualify

def dstr(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

print("Survivorship-corrected walk-forward — Strategy 1 (point-in-time universe)")
print(f"{total_days}d / {n_folds} folds · PIT top-{uni_n} from a {pool_n}-coin pool · {tf} · realism ON\n")

pool = BT.top_symbols(pool_n)
print(f"Candidate pool ({pool_n}): {', '.join(s.split('/')[0] for s in pool)}\n")
print("Fetching history for the whole pool + replaying S1 (heavy — be patient)…", flush=True)

btc1h = BT.fetch_ohlcv("BTC/USDT:USDT", "1h", total_days + 5)
btc_reg = BT.btc_regime_series(btc1h)
now_ms = btc1h[-1][0]
span_ms = total_days * 24 * 3600 * 1000
start_ms = now_ms - span_ms
fold_ms = span_ms / n_folds

sym_trades = {}      # base -> [trades]
sym_candles = {}     # base -> ohlcv (for the point-in-time volume rank)
t0 = time.time()
for si, sym in enumerate(pool, 1):
    base = sym.split("/")[0]
    try:
        ohlcv = BT.fetch_ohlcv(sym, tf, total_days)
        oh4h  = BT.fetch_ohlcv(sym, "4h", total_days + 10)
    except Exception:
        print(f"  [{si}/{pool_n}] {base}: fetch failed", flush=True)
        continue
    sym_candles[base] = ohlcv
    ema4h = BT.ema_series_4h(oh4h)
    n = len(ohlcv)
    i = BT.WINDOW
    trades = []
    while i < n - 1:
        oh = ohlcv[i - BT.WINDOW + 1:i + 1]
        ts = ohlcv[i][0]
        res = eval_fn(oh, BT.as_of(ema4h, ts, None), BT.as_of(btc_reg, ts, "neutral"))
        if res:
            is_long, entry, sl, tp1, tp2, eff = res
            out = sim_fn(ohlcv, i, is_long, entry, sl, tp1, tp2)
            if out:
                pnl, cb = out
                pnl = BT.apply_costs(pnl, int(cb) - i, tf_hours, True)
                R = float(abs(entry - sl) / entry * 100)
                trades.append({"symbol": base, "dir": "LONG" if is_long else "SHORT", "lights": int(eff),
                               "pnl": round(pnl, 2), "R": round(R, 2), "rr": round(pnl / R, 2) if R else 0.0,
                               "win": bool(pnl > 0), "ts": int(ts)})
                i = cb + 1
                continue
        i += 1
    sym_trades[base] = trades
    print(f"  [{si}/{pool_n}] {base}: {len(trades)} trades", flush=True)


def pit_universe(rank_ts):
    """Top-uni_n bases by trailing quote-volume (close×vol) as of rank_ts."""
    vols = []
    for base, oh in sym_candles.items():
        v = 0.0
        bars = 0
        for c in oh:
            if c[0] > rank_ts:
                break
            if c[0] >= rank_ts - VOL_LOOKBACK_MS:
                v += c[4] * c[5]
                bars += 1
        if bars >= MIN_BARS_LISTED:
            vols.append((base, v))
    vols.sort(key=lambda kv: kv[1], reverse=True)
    return [b for b, _ in vols[:uni_n]]


print("\n" + "=" * 92)
print(f"  SURVIVORSHIP-CORRECTED WALK-FORWARD — S1 · {total_days}d / {n_folds} folds · PIT top-{uni_n}")
print("=" * 92)
print(f"  {'Fold':<5} {'Window (UTC)':<25} {'Trades':>6} {'Win%':>6} {'Exp/R':>7} {'TotalR':>7} {'MaxDD':>7}")
print("  " + "-" * 88)
fold_sums = []
all_pit_trades = []
universes = []
for k in range(n_folds):
    fs = start_ms + k * fold_ms
    fe = fs + fold_ms
    uni = pit_universe(fs)
    universes.append(uni)
    ft = [t for b in uni for t in sym_trades.get(b, []) if fs <= t["ts"] < fe]
    all_pit_trades += ft
    r = BT.summarize(ft, 0, total_days / n_folds, uni_n)
    fold_sums.append(r)
    win = f"{r['win_rate']}" if r["total"] else "—"
    exp = f"{r['expectancy_r']:+}" if r["total"] else "—"
    tot = f"{r['total_r']:+}" if r["total"] else "—"
    dd = f"{r['max_drawdown_r']:+}" if r["total"] else "—"
    print(f"  {k+1:<5} {dstr(fs)+' → '+dstr(fe):<25} {r['total']:>6} {win:>6} {exp:>7} {tot:>7} {dd:>7}")
print("  " + "-" * 88)
full = BT.summarize(all_pit_trades, time.time() - t0, total_days, uni_n)
print(f"  {'ALL':<5} {dstr(start_ms)+' → '+dstr(now_ms):<25} {full['total']:>6} "
      f"{full['win_rate']:>6} {full['expectancy_r']:>+7} {full['total_r']:>+7} {full['max_drawdown_r']:>+7}")
print("=" * 92)

# Proof the universe is genuinely point-in-time (it drifts fold to fold).
print("\n  Point-in-time universe per fold (first 16 shown) — it CHANGES across folds:")
for k, uni in enumerate(universes, 1):
    print(f"   Fold {k} ({dstr(start_ms + (k-1)*fold_ms)}): {', '.join(uni[:16])}")
today = [s.split('/')[0] for s in pool[:uni_n]]
print(f"   Today's top-{uni_n} (biased): {', '.join(today[:16])}")

graded = [r for r in fold_sums if r["total"] > 0]
pos = [r for r in graded if r["expectancy_r"] > 0]
print("\n  VERDICT — S1 with survivorship correction:")
if graded:
    exps = sorted(r["expectancy_r"] for r in graded)
    med = exps[len(exps) // 2]
    print(f"   • Positive-edge folds: {len(pos)}/{len(graded)}")
    print(f"   • Per-fold expectancy: worst {exps[0]:+}R · median {med:+}R · best {exps[-1]:+}R")
    print(f"   • Full-period: {full['total']} trades · {full['win_rate']}% win · "
          f"exp {full['expectancy_r']:+}R · total {full['total_r']:+}R · maxDD {full['max_drawdown_r']:+}R")
print("\n  Compare to the BIASED walk_forward.py (same period) to size the survivorship gap.")
print("  Residual: pool is currently-listed only; fully-delisted coins still excluded.")
