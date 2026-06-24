"""Backtest-ONLY experiment: does an ADX and/or volatility-regime filter
strengthen S1's edge? Runs S1 in several filter configs on an identical clean
crypto universe across two windows, realism ON. Read-only; touches nothing live.

Usage: python s1_filters.py
"""
import sys, json, time
import backtest as BT
from backtest import run_backtest, _atr_pct

CRYPTO = ["BTC","ETH","SOL","XRP","BNB","DOGE","ADA","AVAX","LINK","LTC","DOT","TRX",
          "NEAR","SUI","APT","ARB","OP","INJ","ATOM","FIL","UNI","AAVE","TIA","SEI","BCH","ETC"]
SYMS = [f"{t}/USDT:USDT" for t in CRYPTO]
WINDOWS = [120, 270]

# memoize fetches so every config in a window shares identical candles
_orig = BT.fetch_ohlcv
_cache = {}
def _cached(symbol, timeframe, d):
    k = (symbol, timeframe, d)
    if k not in _cache:
        _cache[k] = _orig(symbol, timeframe, d)
    return _cache[k]
BT.fetch_ohlcv = _cached

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

# config matrix: (label, adx, vol)
CONFIGS = [
    ("baseline",          "off", None),
    ("ADX>=20",           "on",  None),
    ("vol floor 0.4%",    "off", {"min": 0.4}),
    ("vol floor 0.7%",    "off", {"min": 0.7}),
    ("vol band 0.4-3.0%", "off", {"min": 0.4, "max": 3.0}),
    ("ADX + vol 0.4-3.0", "on",  {"min": 0.4, "max": 3.0}),
]

def atr_context(days):
    """Rough ATR%-of-price distribution across the universe (last ~500 bars each)."""
    vals = []
    for s in SYMS:
        oh = _cache.get((s, "1h", days))
        if not oh:
            continue
        for i in range(max(15, len(oh) - 500), len(oh)):
            v = _atr_pct(oh[max(0, i - 60):i + 1])
            if v is not None:
                vals.append(v)
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    pct = lambda p: round(vals[min(n - 1, int(p * n))], 3)
    return {"p10": pct(.10), "p25": pct(.25), "median": pct(.50), "p75": pct(.75), "p90": pct(.90)}

results = {}
for days in WINDOWS:
    log(f"==== window {days}d ({len(SYMS)} crypto perps, 1h, realism ON) ====")
    rows = []
    for label, adx, vol in CONFIGS:
        t0 = time.time()
        r = run_backtest(days, SYMS, timeframe="1h", strategy="default", realism=True, adx=adx, vol=vol)
        rows.append({"label": label, "trades": r["total"], "win": r["win_rate"],
                     "exp": r["expectancy_r"], "totR": r["total_r"],
                     "exTop3": r.get("total_r_ex_top3"), "dd": r["max_drawdown_r"]})
        log(f"  {label:20s} trades={r['total']:3d} win={r['win_rate']:5.1f}% "
            f"exp={r['expectancy_r']:+.3f}R totR={r['total_r']:+.1f} ({time.time()-t0:.0f}s)")
    results[days] = {"rows": rows, "atr_pct_context": atr_context(days)}

with open("/tmp/s1_filters.json", "w") as f:
    json.dump(results, f, default=float)

# ---- print tables ----
for days in WINDOWS:
    print("\n" + "=" * 86)
    print(f"  S1 FILTER A/B — {days}d · {len(SYMS)} crypto perps · 1h · realism ON")
    ctx = results[days]["atr_pct_context"]
    if ctx:
        print(f"  ATR%-of-price across universe:  p25={ctx['p25']}  median={ctx['median']}  p75={ctx['p75']}  p90={ctx['p90']}")
    print("=" * 86)
    print(f"  {'config':20s} {'trades':>6} {'win%':>6} {'exp/R':>8} {'totalR':>8} {'exTop3R':>8} {'maxDD':>7}")
    print("  " + "-" * 82)
    base_exp = results[days]["rows"][0]["exp"]
    for x in results[days]["rows"]:
        ex3 = "—" if x["exTop3"] is None else f"{x['exTop3']:+.1f}"
        delta = f"  ({x['exp']-base_exp:+.3f} vs base)" if x["label"] != "baseline" else ""
        print(f"  {x['label']:20s} {x['trades']:>6} {x['win']:>6} {x['exp']:>+8.3f} "
              f"{x['totR']:>+8.1f} {ex3:>8} {x['dd']:>+7.1f}{delta}")
    print("=" * 86)
log("DONE — wrote /tmp/s1_filters.json")
