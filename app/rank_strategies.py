"""Rank ALL strategies (S1–S5) over the last N days on an identical universe.

Fair apples-to-apples: same symbols, same window, same costs, realism ON
(fees + slippage + funding → live-like). Read-only — does NOT touch the live
bot, DB, or any state. fetch_ohlcv is memoized so every strategy sees identical
candles and the exchange is hit only ONCE per (symbol,timeframe).

Usage:  python rank_strategies.py [days] [n_symbols] [timeframe]
Default: 120 days, 30 symbols, 1h (the live S1 timeframe).
"""
import sys, json, time
import backtest
from backtest import STRATEGIES, run_backtest, top_symbols

days = int(sys.argv[1]) if len(sys.argv) > 1 else 120
nsym = int(sys.argv[2]) if len(sys.argv) > 2 else 30
tf   = sys.argv[3] if len(sys.argv) > 3 else "1h"
tickers = sys.argv[4] if len(sys.argv) > 4 else None   # explicit comma list → crypto-only

# Memoize fetches: first strategy fetches everything, the other four reuse it.
_orig = backtest.fetch_ohlcv
_cache = {}
def _cached(symbol, timeframe, d):
    k = (symbol, timeframe, d)
    if k not in _cache:
        _cache[k] = _orig(symbol, timeframe, d)
    return _cache[k]
backtest.fetch_ohlcv = _cached

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

if tickers:
    symbols = [f"{t.strip().upper()}/USDT:USDT" for t in tickers.split(",") if t.strip()]
    log(f"Using explicit {len(symbols)}-symbol crypto universe…")
else:
    log(f"Fetching the same {nsym}-symbol universe for all strategies…")
    symbols = top_symbols(nsym)
log(f"Universe: {', '.join(s.split('/')[0] for s in symbols)}")
log(f"Window: {days}d × {len(symbols)} symbols @ {tf}, realism ON\n")

order = ["default", "trend_breakout", "trend_trailing"]  # S4/S5 removed 2026-06-22
results = []
for key in order:
    name = STRATEGIES[key]["name"]
    t0 = time.time()
    log(f"▶ {name} …")
    try:
        r = run_backtest(days, symbols, timeframe=tf, strategy=key, realism=True)
    except Exception as e:  # keep going; record the failure
        log(f"   !! FAILED: {type(e).__name__}: {e}\n")
        results.append({"_key": key, "_error": f"{type(e).__name__}: {e}"})
        continue
    r["_key"] = key
    results.append(r)
    log(f"   {r['total']} trades · exp {r['expectancy_r']:+}R · win {r['win_rate']}% · "
        f"total {r['total_r']:+}R · DD {r['max_drawdown_r']:+}R  ({time.time()-t0:.0f}s)\n")

# JSON for downstream analysis
with open("/tmp/strategy_rank.json", "w") as f:
    json.dump({"days": days, "n_symbols": len(symbols), "tf": tf,
               "symbols": [s.split('/')[0] for s in symbols], "results": results},
              f, default=float)

# Rank by expectancy per trade (the edge), then total R as a tiebreak.
ok = [r for r in results if "_error" not in r]
ranked = sorted(ok, key=lambda r: (r["expectancy_r"], r["total_r"]), reverse=True)

print("=" * 92)
print(f"  RANKING — last {days}d · {len(symbols)} symbols · {tf} · realism ON (fees+slip+funding)")
print("=" * 92)
print(f"  {'#':<2} {'Strategy':<30} {'Trades':>6} {'Win%':>6} {'Exp/R':>7} {'TotalR':>8} {'exTop3R':>8} {'MaxDD':>7}")
print("  " + "-" * 88)
for rank, r in enumerate(ranked, 1):
    short = STRATEGIES[r["_key"]]["name"].split("—")[-1].strip()
    ex3 = r.get("total_r_ex_top3")
    ex3s = "—" if ex3 is None else f"{ex3:+.1f}"
    print(f"  {rank:<2} {short[:30]:<30} {r['total']:>6} {r['win_rate']:>6} "
          f"{r['expectancy_r']:>+7.3f} {r['total_r']:>+8.1f} {ex3s:>8} {r['max_drawdown_r']:>+7.1f}")
for r in results:
    if "_error" in r:
        print(f"  -- {STRATEGIES[r['_key']]['name'][:30]:<30}  ERROR: {r['_error']}")
print("=" * 92)
print("  Ranked best→worst by expectancy/trade (per-trade edge after costs).")
print("  Exp/R=avg R per trade · TotalR=sum of R · exTop3R=TotalR minus 3 best winners (robustness) · MaxDD=worst equity drawdown in R.")
if ranked and ranked[0]["expectancy_r"] <= 0:
    print("  ⚠ Even the top strategy has a NON-POSITIVE edge this window — none profitable here.")
print("  ⚠ Caveat: top_symbols() = today's top-volume coins → survivorship-biased (optimistic); single window.")
log("DONE — wrote /tmp/strategy_rank.json")
