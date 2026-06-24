"""Walk-forward / rolling out-of-sample validation of Strategy 1 (the live bot).

WHY: S1's thresholds in config.py are hand-tuned on recent data, so the headline
backtest edge (e.g. +0.219R/90d) may be OVERFIT to one favourable window. This
splits a long history into N consecutive, NON-OVERLAPPING out-of-sample folds and
measures S1's edge in each fold INDEPENDENTLY, using S1's CURRENT locked params.
If the edge is positive across most folds → it generalises across time/regimes
(robust). If it's positive in only one fold → it's a fluke / overfit.

No look-ahead: every entry bar still sees its full 260-bar lookback and the as-of
4H-trend + 1H-BTC-regime context, exactly like run_backtest / the live bot. Folds
only restrict which bars may INITIATE a trade; a trade may close in a later fold
(it's booked to the fold it was entered in). Read-only — touches no live state.

Usage:   python walk_forward.py [total_days] [n_folds] [n_symbols] [timeframe]
Default: 360 days, 6 folds (~60d each), 30 symbols, 1h.

⚠ Survivorship caveat still applies: top_symbols() is TODAY's top-volume universe
replayed backward, so even the OOS folds run on today's winners (optimistic). This
validates TEMPORAL stability of the edge; it does not fix survivorship bias.
"""
import sys
import time
from datetime import datetime, timezone

import backtest as BT

total_days = int(sys.argv[1]) if len(sys.argv) > 1 else 360
n_folds    = int(sys.argv[2]) if len(sys.argv) > 2 else 6
nsym       = int(sys.argv[3]) if len(sys.argv) > 3 else 30
tf         = sys.argv[4] if len(sys.argv) > 4 else "1h"

eval_fn = BT.STRATEGIES["default"]["fn"]      # Strategy 1
sim_fn  = BT.STRATEGIES["default"]["sim"]
tf_hours = BT.ex.parse_timeframe(tf) / 3600.0

def dstr(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

print(f"Walk-forward validation — Strategy 1 (live bot), locked config params")
print(f"{total_days}d total → {n_folds} OOS folds (~{total_days // n_folds}d each) · "
      f"{nsym} symbols · {tf} · realism ON\n")

symbols = BT.top_symbols(nsym)
print(f"Universe: {', '.join(s.split('/')[0] for s in symbols)}\n")
print("Fetching history + replaying S1 (this pulls ~1y of candles, be patient)…", flush=True)

# BTC regime over the whole span (as-of per bar).
btc1h = BT.fetch_ohlcv("BTC/USDT:USDT", "1h", total_days + 5)
btc_reg = BT.btc_regime_series(btc1h)
now_ms = btc1h[-1][0]
span_ms = total_days * 24 * 3600 * 1000
start_ms = now_ms - span_ms
fold_ms = span_ms / n_folds

# Single forward pass per symbol over the whole history; trades tagged by ENTRY ts.
trades = []
t0 = time.time()
for si, sym in enumerate(symbols, 1):
    try:
        ohlcv = BT.fetch_ohlcv(sym, tf, total_days)
        oh4h  = BT.fetch_ohlcv(sym, "4h", total_days + 10)
    except Exception:
        print(f"  [{si}/{nsym}] {sym.split('/')[0]}: fetch failed", flush=True)
        continue
    ema4h = BT.ema_series_4h(oh4h)
    n = len(ohlcv)
    i = BT.WINDOW
    cnt = 0
    while i < n - 1:
        oh = ohlcv[i - BT.WINDOW + 1:i + 1]
        ts = ohlcv[i][0]
        res = eval_fn(oh, BT.as_of(ema4h, ts, None), BT.as_of(btc_reg, ts, "neutral"))
        if res:
            is_long, entry, sl, tp1, tp2, eff = res
            out = sim_fn(ohlcv, i, is_long, entry, sl, tp1, tp2)
            if out:
                pnl, close_bar = out
                pnl = BT.apply_costs(pnl, int(close_bar) - i, tf_hours, True)
                R = float(abs(entry - sl) / entry * 100)
                trades.append({"symbol": sym.split("/")[0], "dir": "LONG" if is_long else "SHORT",
                               "lights": int(eff), "pnl": round(pnl, 2), "R": round(R, 2),
                               "rr": round(pnl / R, 2) if R else 0.0, "win": bool(pnl > 0),
                               "ts": int(ts)})           # ENTRY ts → fold bucketing
                cnt += 1
                i = close_bar + 1
                continue
        i += 1
    print(f"  [{si}/{nsym}] {sym.split('/')[0]}: {cnt} trades", flush=True)

# Bucket trades into consecutive OOS folds by entry ts and summarise each.
print("\n" + "=" * 86)
print(f"  WALK-FORWARD (rolling out-of-sample) — Strategy 1 · {total_days}d / {n_folds} folds")
print("=" * 86)
hdr = f"  {'Fold':<5} {'Window (UTC)':<25} {'Trades':>6} {'Win%':>6} {'Exp/R':>7} {'TotalR':>7} {'MaxDD':>7}"
print(hdr)
print("  " + "-" * 82)
fold_summaries = []
for k in range(n_folds):
    f_start = start_ms + k * fold_ms
    f_end = f_start + fold_ms
    ft = [t for t in trades if f_start <= t["ts"] < f_end]
    r = BT.summarize(ft, 0, total_days / n_folds, nsym)
    fold_summaries.append(r)
    win = f"{r['win_rate']}" if r["total"] else "—"
    exp = f"{r['expectancy_r']:+}" if r["total"] else "—"
    tot = f"{r['total_r']:+}" if r["total"] else "—"
    dd = f"{r['max_drawdown_r']:+}" if r["total"] else "—"
    print(f"  {k+1:<5} {dstr(f_start)+' → '+dstr(f_end):<25} {r['total']:>6} "
          f"{win:>6} {exp:>7} {tot:>7} {dd:>7}")
print("  " + "-" * 82)

full = BT.summarize(trades, time.time() - t0, total_days, nsym)
print(f"  {'ALL':<5} {dstr(start_ms)+' → '+dstr(now_ms):<25} {full['total']:>6} "
      f"{full['win_rate']:>6} {full['expectancy_r']:>+7} {full['total_r']:>+7} {full['max_drawdown_r']:>+7}")
print("=" * 86)

# ── Verdict ──────────────────────────────────────────────────────────────────
graded = [r for r in fold_summaries if r["total"] > 0]          # folds that had trades
pos = [r for r in graded if r["expectancy_r"] > 0]
n_graded = len(graded)
print(f"\n  VERDICT — Strategy 1 robustness across time:")
if n_graded == 0:
    print("   • No fold produced any trades — widen the window or symbol set.")
else:
    print(f"   • Positive-edge folds: {len(pos)}/{n_graded}  "
          f"(expectancy > 0 in {round(100*len(pos)/n_graded)}% of folds that traded).")
    exps = sorted(r["expectancy_r"] for r in graded)
    median = exps[len(exps) // 2]
    print(f"   • Per-fold expectancy: worst {exps[0]:+}R · median {median:+}R · best {exps[-1]:+}R")
    print(f"   • Full-period: {full['total']} trades · {full['win_rate']}% win · "
          f"exp {full['expectancy_r']:+}R · total {full['total_r']:+}R · maxDD {full['max_drawdown_r']:+}R")
    if len(pos) == n_graded and n_graded >= 3:
        print("   ✅ ROBUST: positive in EVERY fold that traded → the edge generalises across time.")
    elif len(pos) >= 0.7 * n_graded:
        print("   🟡 MOSTLY ROBUST: positive in most folds, negative in some → regime-sensitive but real.")
    else:
        print("   🔴 FRAGILE / OVERFIT: positive in a minority of folds → the headline edge is likely "
              "a fluke of one window, not a durable edge.")
print("   ⚠ Survivorship bias remains (today's top-volume universe replayed back) — read as an")
print("     UPPER bound on the true edge. Few trades/fold for S1 → each fold is noisy.")
