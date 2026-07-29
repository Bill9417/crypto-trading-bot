"""
s1_exit_lab.py — is S1's problem the SIGNAL or the EXIT?

THE DIAGNOSIS THAT PROMPTED THIS (2026-07-29, 124 walk-forward trades):
S1 wins 54.0% of the time, yet still loses money, because its payoff is
structurally lopsided:

    full stop                    46%  ~-1.07R
    TP1 hit -> breakeven stop    31%  ~+0.42R   <- gave it all back
    TP1 + TP2 both hit           23%  ~+1.41R
    anything above +1.6R          0%   never happens, not once

Every winner is capped at 1.5R gross; every loser costs a full 1.07R. So
the average win (+0.849R) is SMALLER than the average loss (-1.067R), and
break-even would need a 55.7% win rate against the 54.0% actually achieved.
S1 misses by 1.7 points — on exit management, not signal quality.

So: hold the ENTRY signal completely fixed (same bot.py logic, same bars,
same fills) and vary ONLY what happens after the fill. Any difference is
attributable to exit rules alone.

Same anti-overfit discipline as s1_regime_lab: the headline metric is FOLD
CONSISTENCY, not best aggregate. Testing ~8 exit rules against ~124 trades
will produce a flattering winner by chance alone.

    python s1_exit_lab.py [days] [n_folds] [n_candidates] [top_n]
    python s1_exit_lab.py --cached
"""
import json
import os
import sys
import time

import backtest as BT
import s1_regime_lab as RL
import walk_forward as WF

TF = "1h"
# The entry filter s1_regime_lab found (symbol's own ATR% at entry). Captured
# here too so exits can be scored on the trades that filter KEEPS — the two
# findings are orthogonal (one picks trades, one manages them) and the real
# question is whether they compound.
LOWVOL_MAX = 1.0
CACHE_FILE = os.path.join(os.path.dirname(__file__), "s1_exit_lab_trades.json")
MAX_WAIT = BT.MAX_WAIT_BARS
MAX_HOLD = BT.MAX_HOLD_BARS


def _atr_at(oh, idx, period=14):
    """Plain ATR in PRICE units over the `period` bars ending at idx."""
    if idx < period + 1:
        return None
    trs = []
    for k in range(idx - period + 1, idx + 1):
        h, low, pc = oh[k][2], oh[k][3], oh[k - 1][4]
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    return sum(trs) / len(trs) if trs else None


def _fill_bar(oh, i, is_long, entry, tp1):
    """Shared resting-limit fill — identical for every exit rule, so the
    comparison isolates exits. Mirrors backtest.simulate_trade."""
    n = len(oh)
    for j in range(i + 1, min(i + 1 + MAX_WAIT, n)):
        hi, lo = oh[j][2], oh[j][3]
        if (is_long and hi >= tp1) or (not is_long and lo <= tp1):
            return None                      # target printed before entry -> cancelled
        if (is_long and lo <= entry) or (not is_long and hi >= entry):
            return j
    return None


def _fav(px, entry, is_long):
    """Signed % move in the trade's favour."""
    return ((px - entry) / entry * 100.0) if is_long else ((entry - px) / entry * 100.0)


def exit_bracket(oh, fill, is_long, entry, sl, risk, *, tp1_r, tp2_r, use_be, partial):
    """The family S1 already lives in: optional 50% partial at tp1_r, optional
    breakeven stop on the runner, hard target at tp2_r."""
    n = len(oh)
    tp1 = entry + (risk * tp1_r if is_long else -risk * tp1_r)
    tp2 = entry + (risk * tp2_r if is_long else -risk * tp2_r)
    tp1_pct = _fav(tp1, entry, is_long)
    took_partial = False
    stop = sl
    for k in range(fill, min(fill + MAX_HOLD, n)):
        hi, lo = oh[k][2], oh[k][3]
        hit_stop = (lo <= stop) if is_long else (hi >= stop)
        hit_tp1 = (hi >= tp1) if is_long else (lo <= tp1)
        hit_tp2 = (hi >= tp2) if is_long else (lo <= tp2)
        if not took_partial:
            if hit_stop:
                return _fav(stop, entry, is_long), k
            if hit_tp1:
                if not partial:              # single-target mode: tp1 IS the exit
                    if tp1_r >= tp2_r:
                        return tp1_pct, k
                else:
                    took_partial = True
                    if use_be:
                        stop = entry
                    continue
        else:
            if hit_stop:                     # breakeven (or original SL if use_be=False)
                return tp1_pct * 0.5 + _fav(stop, entry, is_long) * 0.5, k
            if hit_tp2:
                return tp1_pct * 0.5 + _fav(tp2, entry, is_long) * 0.5, k
        if not partial and hit_tp2:
            return _fav(tp2, entry, is_long), k
    last = oh[min(fill + MAX_HOLD, n) - 1][4]
    mtm = _fav(last, entry, is_long)
    return (tp1_pct * 0.5 + mtm * 0.5) if took_partial else mtm, min(fill + MAX_HOLD, n) - 1


def exit_trail(oh, fill, is_long, entry, sl, risk, *, tp1_r, partial, atr_mult, atr0):
    """Original SL governs until the trade reaches tp1_r; only THEN does an
    ATR trail take over (optionally after booking 50%). No fixed upper
    target, so a real trend can pay far more than 2R — the only family here
    that can produce the >1.6R outcomes S1 has literally never had.

    The trail deliberately does NOT engage before tp1_r. An always-on ATR
    trail silently tightens the stop from bar one whenever ATR is small
    relative to S1's stop distance, which would test 'a tighter stop' rather
    than 'let winners run' and make this comparison meaningless."""
    n = len(oh)
    if not atr0:
        return None
    trigger = entry + (risk * tp1_r if is_long else -risk * tp1_r)
    trig_pct = _fav(trigger, entry, is_long)
    took_partial = False
    trailing = False
    stop = sl
    peak = entry
    for k in range(fill, min(fill + MAX_HOLD, n)):
        hi, lo = oh[k][2], oh[k][3]
        if (lo <= stop) if is_long else (hi >= stop):
            exit_pct = _fav(stop, entry, is_long)
            return (trig_pct * 0.5 + exit_pct * 0.5) if took_partial else exit_pct, k
        if not trailing and ((hi >= trigger) if is_long else (lo <= trigger)):
            trailing = True
            took_partial = bool(partial)
            peak = trigger
        if trailing:
            peak = max(peak, hi) if is_long else min(peak, lo)
            trail = peak - atr_mult * atr0 if is_long else peak + atr_mult * atr0
            stop = max(stop, trail) if is_long else min(stop, trail)
    last = oh[min(fill + MAX_HOLD, n) - 1][4]
    mtm = _fav(last, entry, is_long)
    return (trig_pct * 0.5 + mtm * 0.5) if took_partial else mtm, min(fill + MAX_HOLD, n) - 1


# name -> callable(oh, fill, is_long, entry, sl, risk, atr0) -> (pnl_pct, bar)
RULES = {
    "current (50%@1R, BE, 2R)":
        lambda *a, atr0=None: exit_bracket(*a, tp1_r=1, tp2_r=2, use_be=True, partial=True),
    "no breakeven (50%@1R, 2R)":
        lambda *a, atr0=None: exit_bracket(*a, tp1_r=1, tp2_r=2, use_be=False, partial=True),
    "no BE + TP2 3R":
        lambda *a, atr0=None: exit_bracket(*a, tp1_r=1, tp2_r=3, use_be=False, partial=True),
    "no BE + TP2 4R":
        lambda *a, atr0=None: exit_bracket(*a, tp1_r=1, tp2_r=4, use_be=False, partial=True),
    "BE + TP2 3R":
        lambda *a, atr0=None: exit_bracket(*a, tp1_r=1, tp2_r=3, use_be=True, partial=True),
    "single target 2R (no partial)":
        lambda *a, atr0=None: exit_bracket(*a, tp1_r=2, tp2_r=2, use_be=False, partial=False),
    "single target 3R (no partial)":
        lambda *a, atr0=None: exit_bracket(*a, tp1_r=3, tp2_r=3, use_be=False, partial=False),
    "trail 2xATR, no partial":
        lambda *a, atr0=None: exit_trail(*a, tp1_r=1, partial=False, atr_mult=2.0, atr0=atr0),
    "trail 3xATR, no partial":
        lambda *a, atr0=None: exit_trail(*a, tp1_r=1, partial=False, atr_mult=3.0, atr0=atr0),
    "50%@1R then trail 2xATR":
        lambda *a, atr0=None: exit_trail(*a, tp1_r=1, partial=True, atr_mult=2.0, atr0=atr0),
    "50%@1R then trail 3xATR":
        lambda *a, atr0=None: exit_trail(*a, tp1_r=1, partial=True, atr_mult=3.0, atr0=atr0),
}


def collect(days, n_folds, n_cand, top_n, progress=print):
    candidates = WF._drop_tradfi_perps(BT.top_symbols(n_cand), progress)
    progress(f"Candidate pool: {len(candidates)} symbols")
    btc1h = BT.fetch_ohlcv("BTC/USDT:USDT", TF, days + 5)
    btc_reg = BT.btc_regime_series(btc1h)
    data = WF.fetch_universe_data(candidates, days, progress)
    bounds = WF.fold_bounds_by_time(btc1h, days, n_folds)

    out = []
    for fi, (lo_ts, hi_ts) in enumerate(bounds, 1):
        ranked = sorted(((s, WF.trailing_dollar_volume(d["oh1h"], WF._ts_index(d["oh1h"], lo_ts)))
                         for s, d in data.items()), key=lambda kv: kv[1], reverse=True)
        for sym in [s for s, v in ranked[:top_n] if v > 0]:
            d = data[sym]
            oh, ema4h = d["oh1h"], d["ema4h"]
            sym_vol = RL.atr_pct_series(oh)
            i = max(WF._ts_index(oh, lo_ts), BT.WINDOW)
            n = WF._ts_index(oh, hi_ts)
            while i < n - 1:
                ts = oh[i][0]
                res = BT.evaluate(oh[i - BT.WINDOW + 1:i + 1],
                                  BT.as_of(ema4h, ts, None), BT.as_of(btc_reg, ts, "neutral"))
                if res:
                    is_long, entry, sl, tp1, _tp2, eff = res
                    fill = _fill_bar(oh, i, is_long, entry, tp1)
                    if fill is not None:
                        risk = abs(entry - sl)
                        R = risk / entry * 100.0
                        atr0 = _atr_at(oh, fill)
                        rec = {"fold": fi, "symbol": sym.split("/")[0],
                               "dir": "LONG" if is_long else "SHORT", "R": R,
                               "sym_vol": BT.as_of(sym_vol, ts, None), "rr": {}}
                        last_bar = fill
                        for name, fn in RULES.items():
                            r = fn(oh, fill, is_long, entry, sl, risk, atr0=atr0)
                            if r is None:
                                continue
                            pnl, bar = r
                            pnl = BT.apply_costs(pnl, bar - fill, 1.0, realism=True)
                            rec["rr"][name] = round(pnl / R, 4) if R else 0.0
                            last_bar = max(last_bar, bar)
                        out.append(rec)
                        i = last_bar + 1
                        continue
                i += 1
        progress(f"Fold {fi}: {sum(1 for t in out if t['fold'] == fi)} trades")
    return out


def report(trades, n_folds):
    print("\n" + "=" * 100)
    print("  S1 EXIT LAB — same entries, different exits. Which management actually pays?")
    print("=" * 100)
    print(f"  {len(trades)} trades · entry signal held FIXED · costs+slippage+funding on every rule\n")
    print(f"  {'exit rule':<32}{'E[R]':>8}{'totR':>9}{'WR':>7}{'avgW':>7}{'avgL':>7}{'max':>7}{'+folds':>8}")
    print("  " + "-" * 96)
    rows = []
    for name in RULES:
        rs = [t["rr"][name] for t in trades if name in t["rr"]]
        if not rs:
            continue
        w = [r for r in rs if r > 0]
        loss = [r for r in rs if r <= 0]
        pf = sum(1 for f in range(1, n_folds + 1)
                 if sum(t["rr"][name] for t in trades
                        if t["fold"] == f and name in t["rr"]) > 0)
        rows.append({"name": name, "exp": sum(rs) / len(rs), "tot": sum(rs),
                     "wr": len(w) / len(rs) * 100,
                     "aw": sum(w) / len(w) if w else 0.0,
                     "al": sum(loss) / len(loss) if loss else 0.0,
                     "mx": max(rs), "pf": pf})
    for r in rows:
        print(f"  {r['name']:<32}{r['exp']:>+8.3f}{r['tot']:>+9.2f}{r['wr']:>6.1f}%"
              f"{r['aw']:>+7.2f}{r['al']:>+7.2f}{r['mx']:>+7.2f}{r['pf']:>5}/{n_folds}")

    base = next((r for r in rows if r["name"].startswith("current")), None)
    print("\n  'max' = the single best trade under that rule. S1's current max is ~1.44R —")
    print("  no rule can beat it without letting winners run past the 2R cap.\n")

    # ── do the two findings COMPOUND? entry filter x exit rule ──
    lo = [t for t in trades if (t.get("sym_vol") or 99) < LOWVOL_MAX]
    if lo:
        print("-" * 100)
        print(f"  COMBINED — each exit rule scored ONLY on the {len(lo)} trades the low-vol")
        print(f"  entry filter (<{LOWVOL_MAX:g}% ATR) keeps. The two findings are independent:")
        print("  one picks WHICH trades, the other manages them. Do they compound?\n")
        print(f"  {'exit rule':<32}{'n':>5}{'E[R]':>9}{'totR':>9}{'WR':>7}{'max':>7}{'+folds':>8}")
        print("  " + "-" * 96)
        combo = []
        for name in RULES:
            rs = [t["rr"][name] for t in lo if name in t["rr"]]
            if not rs:
                continue
            w = [r for r in rs if r > 0]
            pf = sum(1 for f in range(1, n_folds + 1)
                     if sum(t["rr"][name] for t in lo
                            if t["fold"] == f and name in t["rr"]) > 0)
            combo.append({"name": name, "n": len(rs), "exp": sum(rs) / len(rs),
                          "tot": sum(rs), "wr": len(w) / len(rs) * 100,
                          "mx": max(rs), "pf": pf})
        for r in sorted(combo, key=lambda r: -r["exp"]):
            print(f"  {r['name']:<32}{r['n']:>5}{r['exp']:>+9.3f}{r['tot']:>+9.2f}"
                  f"{r['wr']:>6.1f}%{r['mx']:>+7.2f}{r['pf']:>5}/{n_folds}")
        print()
    print("-" * 100)
    print("  CANDIDATES — beat the current rule AND profitable in a majority of folds:")
    good = [r for r in rows if base and r["exp"] > base["exp"]
            and r["pf"] > n_folds / 2 and r["exp"] > 0]
    if not good:
        print("    NONE — no exit rule tested rescues S1. The problem is not (only) the exit.")
    else:
        for r in sorted(good, key=lambda r: -r["exp"]):
            lift = r["exp"] - base["exp"]
            print(f"    {r['name']:<32} E[R]={r['exp']:+.3f} (vs {base['exp']:+.3f}, "
                  f"{lift:+.3f}) · {r['pf']}/{n_folds} folds · maxwin {r['mx']:+.2f}R")
        print("\n    ⚠️ ~11 rules tested on ~124 trades — some of this spread is chance.")
        print("    A winner here is a HYPOTHESIS for paper_tracker, not a live change.")
    print("=" * 100 + "\n")


if __name__ == "__main__":
    if "--cached" in sys.argv:
        blob = json.load(open(CACHE_FILE, encoding="utf-8"))
        print(f"Re-analysing {len(blob['trades'])} cached trades ({blob.get('collected_at')})")
        report(blob["trades"], blob["n_folds"])
        sys.exit(0)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 360
    nf = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    nc = int(sys.argv[3]) if len(sys.argv) > 3 else 120
    tn = int(sys.argv[4]) if len(sys.argv) > 4 else 50
    t0 = time.time()
    tr = collect(days, nf, nc, tn)
    print(f"\nCollected {len(tr)} trades in {time.time() - t0:.0f}s")
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"trades": tr, "n_folds": nf,
                   "collected_at": time.strftime("%Y-%m-%d %H:%M")}, fh)
    report(tr, nf)
