"""
s1_regime_lab.py — does a measurable market regime separate S1's winning
folds from its losing ones?

THE QUESTION: the 2026-07-27 walk-forward found S1 net negative (-0.097R
over 78 trades), profitable in only folds 5-6 (the two most recent). If
some variable measurable AT ENTRY TIME distinguishes the good periods from
the bad ones, S1 doesn't need better signals — it needs to stop trading in
the regimes where it bleeds. Turning -0.097R into ~0R by simply not trading
is a real improvement.

Every feature is captured at the ENTRY bar, from data the live bot already
has, using BT.as_of() point-in-time lookups — nothing here can see the
trade's own outcome or any future bar.

⚠️ READ THE OUTPUT SKEPTICALLY. This tests several candidate gates against
~80-160 trades. With that few trades and that many comparisons, SOMETHING
will look good by pure chance — that is exactly the trap that produced
S1's original overfit. So the report's headline metric is deliberately NOT
"best aggregate expectancy" but FOLD CONSISTENCY: how many independent
folds does a gate improve, and does it help WITHIN folds rather than just
deleting the bad periods wholesale (a gate that only works by excluding
folds 1-4 is just "trade recently", which is worthless going forward).

Nothing here is live-affecting. A gate that survives this still has to earn
a forward track record in paper_tracker before it goes anywhere near real
money.

    python s1_regime_lab.py [days] [n_folds] [n_candidates] [top_n_per_fold]
"""
import json
import os
import sys
import time

import backtest as BT
import walk_forward as WF

TF = "1h"
# Collecting costs ~25 min of paginated ccxt fetches. Cache the tagged trade
# list so follow-up analysis (threshold sweeps, composition checks) is
# instant — re-analysing must never require re-fetching.
CACHE_FILE = os.path.join(os.path.dirname(__file__), "s1_regime_lab_trades.json")


def atr_pct_series(ohlcv, period=14):
    """[(ts, ATR-as-%-of-close)] per bar — causal, mirrors BT._atr_pct's
    simple-mean form. None until `period` bars exist."""
    out, trs = [], []
    for i, c in enumerate(ohlcv):
        if i == 0:
            trs.append(c[2] - c[3])
            out.append((c[0], None))
            continue
        h, low, pc = c[2], c[3], ohlcv[i - 1][4]
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
        if i + 1 < period:
            out.append((c[0], None))
        else:
            atr = sum(trs[-period:]) / period
            close = c[4]
            out.append((c[0], (atr / close * 100.0) if close else None))
    return out


def collect_trades(days, n_folds, n_candidates, top_n, progress=print):
    """Same walk-forward as walk_forward.py, but every trade is tagged with
    the regime features present at its ENTRY bar."""
    candidates = WF._drop_tradfi_perps(BT.top_symbols(n_candidates), progress)
    progress(f"Candidate pool: {len(candidates)} symbols")

    progress("Fetching BTC context...")
    btc1h = BT.fetch_ohlcv("BTC/USDT:USDT", TF, days + 5)
    btc_reg = BT.btc_regime_series(btc1h)
    btc_vol = atr_pct_series(btc1h)

    data = WF.fetch_universe_data(candidates, days, progress)
    if not data:
        raise RuntimeError("no symbols fetched")

    bounds = WF.fold_bounds_by_time(btc1h, days, n_folds)
    all_trades = []
    for fi, (lo_ts, hi_ts) in enumerate(bounds, 1):
        ranked = sorted(
            ((s, WF.trailing_dollar_volume(d["oh1h"], WF._ts_index(d["oh1h"], lo_ts)))
             for s, d in data.items()),
            key=lambda kv: kv[1], reverse=True)
        universe = [s for s, v in ranked[:top_n] if v > 0]

        for sym in universe:
            d = data[sym]
            oh1h, ema4h = d["oh1h"], d["ema4h"]
            sym_vol = atr_pct_series(oh1h)
            i = max(WF._ts_index(oh1h, lo_ts), BT.WINDOW)
            n = WF._ts_index(oh1h, hi_ts)
            while i < n - 1:
                oh = oh1h[i - BT.WINDOW + 1:i + 1]
                ts = oh1h[i][0]
                res = BT.evaluate(oh, BT.as_of(ema4h, ts, None),
                                  BT.as_of(btc_reg, ts, "neutral"))
                if res:
                    is_long, entry, sl, tp1, tp2, eff = res
                    out = BT.simulate_trade(oh1h, i, is_long, entry, sl, tp1, tp2)
                    if out:
                        pnl, close_bar = out
                        pnl = BT.apply_costs(pnl, close_bar - i, 1.0, realism=True)
                        R = abs(entry - sl) / entry * 100
                        all_trades.append({
                            "fold": fi, "symbol": sym.split("/")[0],
                            "dir": "LONG" if is_long else "SHORT",
                            "lights": int(eff),
                            "rr": float(pnl) / R if R else 0.0,
                            "win": bool(pnl > 0),
                            # ── regime features, all as-of the ENTRY bar ──
                            "btc_regime": BT.as_of(btc_reg, ts, "neutral"),
                            "btc_vol": BT.as_of(btc_vol, ts, None),
                            "sym_vol": BT.as_of(sym_vol, ts, None),
                        })
                        i = close_bar + 1
                        continue
                i += 1
        progress(f"Fold {fi}: {sum(1 for t in all_trades if t['fold'] == fi)} trades")
    return all_trades, n_folds


# ── gates ────────────────────────────────────────────────────────────────────
def _vol_ok(t, key, lo, hi):
    v = t.get(key)
    return v is not None and lo <= v < hi


GATES = [
    ("(baseline — no gate)", lambda t: True),
    ("LONG only", lambda t: t["dir"] == "LONG"),
    ("SHORT only", lambda t: t["dir"] == "SHORT"),
    ("BTC regime = bull", lambda t: t["btc_regime"] == "bull"),
    ("BTC regime = bear", lambda t: t["btc_regime"] == "bear"),
    ("BTC regime decisive (not neutral)", lambda t: t["btc_regime"] in ("bull", "bear")),
    ("BTC vol LOW (<0.35%)", lambda t: _vol_ok(t, "btc_vol", 0.0, 0.35)),
    ("BTC vol MID (0.35-0.7%)", lambda t: _vol_ok(t, "btc_vol", 0.35, 0.7)),
    ("BTC vol HIGH (>=0.7%)", lambda t: _vol_ok(t, "btc_vol", 0.7, 99.0)),
    ("BTC vol < 0.7%", lambda t: _vol_ok(t, "btc_vol", 0.0, 0.7)),
    ("sym vol < 1.0%", lambda t: _vol_ok(t, "sym_vol", 0.0, 1.0)),
    ("sym vol 1-2%", lambda t: _vol_ok(t, "sym_vol", 1.0, 2.0)),
    ("sym vol >= 2%", lambda t: _vol_ok(t, "sym_vol", 2.0, 99.0)),
    ("LONG + BTC bull", lambda t: t["dir"] == "LONG" and t["btc_regime"] == "bull"),
    ("LONG + BTC vol < 0.7%", lambda t: t["dir"] == "LONG" and _vol_ok(t, "btc_vol", 0.0, 0.7)),
    # ── threshold sweep around the one gate that survived the first pass ──
    # A single working value is curve-fit; a PLATEAU across neighbours is a
    # real relationship. Same discipline applied to the BTC .pine strategy.
    ("sym vol < 0.6%", lambda t: _vol_ok(t, "sym_vol", 0.0, 0.6)),
    ("sym vol < 0.8%", lambda t: _vol_ok(t, "sym_vol", 0.0, 0.8)),
    ("sym vol < 1.2%", lambda t: _vol_ok(t, "sym_vol", 0.0, 1.2)),
    ("sym vol < 1.5%", lambda t: _vol_ok(t, "sym_vol", 0.0, 1.5)),
    ("LONG + sym vol < 1.0%", lambda t: t["dir"] == "LONG" and _vol_ok(t, "sym_vol", 0.0, 1.0)),
    ("SHORT + sym vol < 1.0%", lambda t: t["dir"] == "SHORT" and _vol_ok(t, "sym_vol", 0.0, 1.0)),
]


def _agg(trades):
    if not trades:
        return 0, 0.0, 0.0
    rs = [t["rr"] for t in trades]
    return len(rs), sum(rs) / len(rs), sum(rs)


def report(trades, n_folds):
    print("\n" + "=" * 92)
    print("  S1 REGIME LAB — which gate (if any) survives ACROSS folds, not just in aggregate")
    print("=" * 92)

    vols = sorted(t["btc_vol"] for t in trades if t["btc_vol"] is not None)
    if vols:
        def pct(p):
            return vols[min(len(vols) - 1, int(len(vols) * p))]
        print(f"  BTC vol at entry — p10 {pct(.1):.2f}%  median {pct(.5):.2f}%  p90 {pct(.9):.2f}%")
    svols = sorted(t["sym_vol"] for t in trades if t["sym_vol"] is not None)
    if svols:
        def spct(p):
            return svols[min(len(svols) - 1, int(len(svols) * p))]
        print(f"  symbol vol at entry — p10 {spct(.1):.2f}%  median {spct(.5):.2f}%  p90 {spct(.9):.2f}%")

    base_by_fold = {}
    for f in range(1, n_folds + 1):
        _, _, tot = _agg([t for t in trades if t["fold"] == f])
        base_by_fold[f] = tot

    print(f"\n  {'gate':<36}{'n':>5}{'E[R]':>9}{'totR':>9}{'+folds':>8}   per-fold totalR")
    print("  " + "-" * 88)
    rows = []
    for label, fn in GATES:
        kept = [t for t in trades if fn(t)]
        n, exp, tot = _agg(kept)
        per_fold, improved, positive = [], 0, 0
        for f in range(1, n_folds + 1):
            _, _, ftot = _agg([t for t in kept if t["fold"] == f])
            per_fold.append(ftot)
            if ftot > 0:
                positive += 1
            if ftot > base_by_fold[f]:
                improved += 1
        rows.append((label, n, exp, tot, positive, improved, per_fold))
        fold_str = " ".join(f"{v:+5.1f}" for v in per_fold)
        print(f"  {label:<36}{n:>5}{exp:>+9.3f}{tot:>+9.2f}{positive:>5}/{n_folds}   {fold_str}")

    print("\n  '+folds' = how many folds are PROFITABLE under that gate (out of "
          f"{n_folds}).")
    print("  A gate is only interesting if it lifts MOST folds — one huge fold "
          "carrying the total\n  is the same overfit that made S1 look good in the "
          "first place.")

    # ── is "low symbol volatility" just a disguised "only trade majors"? ──
    lo = [t for t in trades if _vol_ok(t, "sym_vol", 0.0, 1.0)]
    if lo:
        counts = {}
        for t in lo:
            counts[t["symbol"]] = counts.get(t["symbol"], 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:12]
        print(f"\n  Low-vol bucket composition ({len(lo)} trades across {len(counts)} symbols) —")
        print("  if this is just BTC/ETH, the 'gate' is really 'only trade majors':")
        print("    " + ", ".join(f"{s}×{n}" for s, n in top))

    print("\n" + "-" * 92)
    print("  CANDIDATES (profitable in a majority of folds AND improve on baseline):")
    good = [r for r in rows if r[0] != "(baseline — no gate)"
            and r[4] > n_folds / 2 and r[5] > n_folds / 2 and r[2] > 0]
    if not good:
        print("    NONE. No tested gate rescues S1 across folds — the honest read is that")
        print("    the losing periods aren't explained by any regime variable tested here.")
    else:
        for label, n, exp, tot, positive, improved, _ in sorted(good, key=lambda r: -r[2]):
            print(f"    {label:<36} n={n:<5} E[R]={exp:+.3f}  profitable {positive}/{n_folds} "
                  f"folds, improved {improved}/{n_folds}")
        print("\n    ⚠️ Small sample + many gates tested = some of this is chance.")
        print("    Anything here must earn a FORWARD record in paper_tracker before it")
        print("    touches real money.")
    print("=" * 92 + "\n")


if __name__ == "__main__":
    # --cached re-analyses the saved trade list without re-fetching (~25 min
    # of ccxt pagination otherwise).
    if "--cached" in sys.argv:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            blob = json.load(fh)
        print(f"Re-analysing {len(blob['trades'])} cached trades "
              f"(collected {blob.get('collected_at', '?')})")
        report(blob["trades"], blob["n_folds"])
        sys.exit(0)

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 360
    n_folds = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    n_cand = int(sys.argv[3]) if len(sys.argv) > 3 else 120
    top_n = int(sys.argv[4]) if len(sys.argv) > 4 else 50
    t0 = time.time()
    tr, nf = collect_trades(days, n_folds, n_cand, top_n)
    print(f"\nCollected {len(tr)} trades in {time.time() - t0:.0f}s")
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"trades": tr, "n_folds": nf, "days": days,
                   "n_candidates": n_cand, "top_n": top_n,
                   "collected_at": time.strftime("%Y-%m-%d %H:%M")}, fh)
    print(f"Cached to {CACHE_FILE} — re-analyse instantly with --cached")
    report(tr, nf)
