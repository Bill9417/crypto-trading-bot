"""
factor_lab.py — a factor matrix over real candles, with the honesty checks
wired into the core rather than bolted on afterwards.

WHY THIS EXISTS, AND WHY IT LOOKS PARANOID
------------------------------------------
S1 is already a "hyper matrix": seven confluence lights (EMA50, EMA200, RSI,
volume, 4h trend, SMC, order flow) combined into a score. It was tuned until
it looked excellent. Then walk_forward.py measured it out-of-sample twice —
2026-06-22 and again on 2026-07-27 after the light count grew 5 → 7 — and
both times it came back NET NEGATIVE, profitable in only the two most recent
folds. Classic tuned-to-recent-regime overfit.

Separately, 48 mean-reversion configs on 400 days of real ETH produced win
rates of 55–78% and 46 of 48 were still net negative.

So the failure mode here is not "we lack a scoring matrix". It is that
searching a big combination space against one dataset ALWAYS yields something
that looks good, and the good-looking thing does not survive contact with
unseen data. A tool that reports the best in-sample combination is a machine
for manufacturing that mistake faster.

This module therefore refuses to report an in-sample optimum as a result:

  · every factor weight is fitted on PAST data only and scored on a later,
    untouched fold (expanding-window walk-forward);
  · a shuffled-target null run says what "edge" looks like when there is
    provably none, so the real number has something to be compared against;
  · per-factor buckets are checked for MONOTONICITY, because a factor that
    only works in its 3rd quintile is noise wearing a suit;
  · sample size and a t-stat travel with every number.

Read-only research. Imports nothing from the live trading path and cannot
place, size or modify an order.

Usage:
    python factor_lab.py                       # BTC+ETH+SOL, 180d, 1h
    python factor_lab.py 240 1h BTC,ETH,SOL,BNB
"""
import sys

import numpy as np
import pandas as pd

# ── configuration ────────────────────────────────────────────────────────────
HORIZON_BARS = 12          # forward window the factors are asked to predict
N_BUCKETS = 5              # quintiles per factor
N_FOLDS = 5                # expanding-window walk-forward folds
TOP_FRACTION = 0.20        # "take the strongest 20% of bars" as the trade rule
MIN_TRAIN_ROWS = 400       # below this a fitted weight is noise; fold is skipped
NULL_RUNS = 100            # rotated-target repetitions for the noise baseline


def rank_corr(a: pd.Series, b: pd.Series) -> float:
    """Spearman without scipy — rank both, then Pearson.

    pandas' method="spearman" imports scipy, which this project does not
    install; adding a dependency for one correlation would be a poor trade.
    Rank correlation (not Pearson on raw values) because technical factors
    are heavy-tailed and a handful of outliers otherwise decide the answer.
    """
    ok = a.notna() & b.notna()
    if ok.sum() < 3:
        return 0.0
    ra = a[ok].rank()
    rb = b[ok].rank()
    if ra.std(ddof=0) == 0 or rb.std(ddof=0) == 0:
        return 0.0
    c = float(ra.corr(rb))          # Pearson on ranks == Spearman
    return 0.0 if pd.isna(c) else c


# ── factor panel ─────────────────────────────────────────────────────────────
# Every factor reads bar i using data up to and including bar i. The target
# reads i+1 .. i+H. Nothing here may peek across that line.
def build_factors(df: pd.DataFrame) -> pd.DataFrame:
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
    out = pd.DataFrame(index=df.index)

    # momentum / mean-reversion
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi14"] = 100 - 100 / (1 + rs)
    out["rsi_slope"] = out["rsi14"].diff(5)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    # normalised by price, or BTC's absolute MACD would dwarf every alt's
    out["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / close

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    out["ema_dist"] = close / ema50 - 1
    out["ema_slope"] = ema50.pct_change(10)
    out["ema_stack"] = (np.sign(ema20 - ema50) + np.sign(ema50 - ema200)) / 2

    # volatility
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    out["atr_pct"] = atr / close

    # participation
    out["vol_ratio"] = vol / vol.rolling(20).mean()

    # position in range
    sma20 = close.rolling(20).mean()
    sd20 = close.rolling(20).std()
    out["bb_pos"] = (close - sma20) / (2 * sd20).replace(0, np.nan)

    # trailing return
    out["ret_24"] = close.pct_change(24)

    return out, atr


def forward_target(df: pd.DataFrame, atr: pd.Series, horizon: int) -> pd.Series:
    """Return over the NEXT `horizon` bars expressed in ATR units.

    ATR-normalised because a 2% move in BTC and a 2% move in a small alt are
    not the same event, and an un-normalised target quietly turns the study
    into "which symbol was most volatile".
    """
    fwd = df["close"].shift(-horizon) / df["close"] - 1
    return fwd / (atr / df["close"]).replace(0, np.nan)


# ── per-factor edge: the matrix ──────────────────────────────────────────────
def factor_buckets(f: pd.Series, y: pd.Series, n_buckets: int = N_BUCKETS) -> dict:
    """Mean forward-R per quantile bucket, plus whether the relationship is
    actually ordered. A factor whose best bucket is in the middle is not a
    signal you can trade a threshold on."""
    ok = f.notna() & y.notna()
    f, y = f[ok], y[ok]
    if len(f) < n_buckets * 20:
        return {"n": len(f), "buckets": [], "monotonic": 0.0, "spread": 0.0,
                "ic": 0.0, "t": 0.0}
    try:
        q = pd.qcut(f, n_buckets, labels=False, duplicates="drop")
    except ValueError:
        return {"n": len(f), "buckets": [], "monotonic": 0.0, "spread": 0.0,
                "ic": 0.0, "t": 0.0}

    means, counts = [], []
    for b in sorted(pd.unique(q.dropna())):
        sel = y[q == b]
        means.append(float(sel.mean()))
        counts.append(int(len(sel)))

    # Spearman of bucket index vs bucket mean: +1 = cleanly ordered
    idx = np.arange(len(means))
    mono = rank_corr(pd.Series(idx, dtype=float), pd.Series(means)) if len(means) > 2 else 0.0
    ic = rank_corr(f, y)
    t = ic * np.sqrt(max(len(f) - 2, 1)) / np.sqrt(max(1 - ic ** 2, 1e-9))
    return {"n": int(len(f)), "buckets": means, "counts": counts,
            "monotonic": 0.0 if np.isnan(mono) else mono,
            "spread": float(means[-1] - means[0]) if means else 0.0,
            "ic": 0.0 if np.isnan(ic) else ic,
            "t": 0.0 if np.isnan(t) else float(t)}


# ── combination + walk-forward ───────────────────────────────────────────────
def _zscore(frame: pd.DataFrame) -> pd.DataFrame:
    return (frame - frame.mean()) / frame.std(ddof=0).replace(0, np.nan)


def fit_weights(fx: pd.DataFrame, y: pd.Series) -> pd.Series:
    """Weight each factor by its rank correlation with the target ON THIS
    SLICE ONLY. Deliberately not a regression: with ten collinear technical
    factors, least squares fits the noise and produces enormous offsetting
    weights that invert out-of-sample."""
    w = {}
    for c in fx.columns:
        ok = fx[c].notna() & y.notna()
        if ok.sum() < 50:
            w[c] = 0.0
            continue
        w[c] = rank_corr(fx.loc[ok, c], y[ok])
    return pd.Series(w)


def score_rows(fx: pd.DataFrame, weights: pd.Series) -> pd.Series:
    z = _zscore(fx)
    return (z * weights).sum(axis=1, skipna=True)


def walk_forward(panel: pd.DataFrame, n_folds: int = N_FOLDS,
                 top_fraction: float = TOP_FRACTION) -> dict:
    """Expanding window: fold k fits on everything before it and is scored on
    fold k only. Fold 0 is training-only, so it is never reported — an
    out-of-sample claim needs a slice the weights have never seen.
    """
    panel = panel.sort_values("ts").reset_index(drop=True)
    fcols = [c for c in panel.columns if c not in ("ts", "y", "symbol")]
    edges = np.linspace(0, len(panel), n_folds + 1).astype(int)

    folds = []
    for k in range(1, n_folds):
        tr = panel.iloc[: edges[k]]
        te = panel.iloc[edges[k]: edges[k + 1]]
        if len(tr) < MIN_TRAIN_ROWS or len(te) < 50:
            continue
        w = fit_weights(tr[fcols], tr["y"])
        te_score = score_rows(te[fcols], w)
        cut = te_score.quantile(1 - top_fraction)
        picked = te["y"][te_score >= cut].dropna()
        rest = te["y"][te_score < cut].dropna()
        if len(picked) < 20:
            continue
        base = float(rest.mean()) if len(rest) else float("nan")
        folds.append({
            "fold": k,
            "train_rows": int(len(tr)),
            "test_rows": int(len(te)),
            "n_picked": int(len(picked)),
            "mean_R": float(picked.mean()),
            "baseline_R": base,
            # THE metric. Absolute return of a long-only subset is mostly the
            # market's own drift: in an up-trending series ANY 20% of bars
            # shows a positive mean, and random-walk data then reads as an
            # edge in every fold. Picked MINUS unpicked cancels the drift and
            # measures the only thing the score is claiming — selection skill.
            "edge_R": float(picked.mean() - base) if len(rest) else float("nan"),
            "hit_rate": float((picked > 0).mean()),
            "weights": w.to_dict(),
        })
    oos = [f["edge_R"] for f in folds if not np.isnan(f["edge_R"])]
    return {
        "folds": folds,
        "oos_edge_R": float(np.mean(oos)) if oos else float("nan"),
        "oos_mean_R": float(np.mean([f["mean_R"] for f in folds])) if folds else float("nan"),
        "oos_positive_folds": int(sum(1 for r in oos if r > 0)),
        "n_folds_scored": len(oos),
    }


def _rotate(y: np.ndarray, k: int) -> np.ndarray:
    """Circularly shift the target by k bars.

    This is the null that finally behaves. Two weaker ones failed first:

      · a row-wise shuffle destroys the target's autocorrelation entirely.
        Forward windows overlap, so real targets are strongly autocorrelated
        and a selected subset drifts far from zero on luck alone. The null
        came out far too tight and pure random-walk data "cleared" it.
      · a block shuffle preserves autocorrelation only INSIDE a block and
        still destroys the low-frequency structure that actually drives the
        spurious relationships — better, but still under-dispersed.

    Rotation preserves y's autocorrelation at EVERY lag exactly, while
    destroying its alignment with the factors. That is the comparison we
    want: same statistical texture, provably no relationship.
    """
    return np.roll(y, k)


def null_baseline(panel: pd.DataFrame, runs: int = NULL_RUNS, seed: int = 7,
                  **kw) -> dict:
    """Re-run the whole walk-forward against a target that has been rotated
    away from its factors. Any relationship is destroyed by construction, but
    the target keeps its own statistical texture — so whatever score comes
    back is what this pipeline produces on structured noise."""
    rng = np.random.default_rng(seed)
    n = len(panel)
    scores = []
    # keep well clear of both ends so no rotation is near-identity
    offsets = rng.integers(n // 10, n - n // 10, size=runs)
    for k in offsets:
        p = panel.copy()
        p["y"] = _rotate(p["y"].to_numpy(), int(k))
        r = walk_forward(p, **kw)
        if not np.isnan(r["oos_edge_R"]):
            scores.append(r["oos_edge_R"])
    if not scores:
        return {"runs": 0}
    a = np.array(scores)
    return {"runs": len(a), "mean": float(a.mean()), "sd": float(a.std(ddof=1)),
            "p95": float(np.percentile(a, 95)), "max": float(a.max()),
            "dist": a}


def empirical_p(real: float, null: dict) -> float:
    """Fraction of null runs that matched or beat the real result. The +1s are
    the standard correction — with a finite null you can never honestly claim
    p = 0."""
    a = null.get("dist")
    if a is None or not len(a) or np.isnan(real):
        return 1.0
    return float((np.sum(a >= real) + 1) / (len(a) + 1))


# ── panel assembly ───────────────────────────────────────────────────────────
def panel_for(symbol: str, ohlcv: list, horizon: int = HORIZON_BARS) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.astype({c: float for c in ("open", "high", "low", "close", "volume")})
    fx, atr = build_factors(df)
    y = forward_target(df, atr, horizon)
    panel = fx.copy()
    panel["y"] = y
    panel["ts"] = df["ts"]
    panel["symbol"] = symbol
    # the last `horizon` bars have no complete future yet — dropping them is
    # what stops a partially-known outcome leaking in as if it were settled
    return panel.iloc[:-horizon].dropna(subset=["y"])


def analyse(panels: dict, horizon: int = HORIZON_BARS) -> dict:
    all_p = pd.concat(panels.values(), ignore_index=True) if panels else pd.DataFrame()
    if all_p.empty:
        return {"error": "no data"}
    fcols = [c for c in all_p.columns if c not in ("ts", "y", "symbol")]
    matrix = {c: factor_buckets(all_p[c], all_p["y"]) for c in fcols}
    wf = walk_forward(all_p)
    null = null_baseline(all_p)
    return {"rows": int(len(all_p)), "symbols": list(panels),
            "horizon": horizon, "matrix": matrix, "walk_forward": wf, "null": null}


def verdict(wf: dict, null: dict) -> tuple:
    """Two independent hurdles, both required.

    Beating the noise band alone is not enough: p95 is a 5% false-positive
    rate BY CONSTRUCTION, and on random-walk data this pipeline does clear it
    roughly that often. A real edge should also show up in MOST folds — the
    2026-06-22 and 07-27 walk-forwards both failed precisely on fold
    consistency (2 of 6 positive, and only the most recent two), while the
    headline average still looked survivable. Consistency is the check that
    catches tuned-to-recent-regime.
    """
    reasons, ok = [], True
    r = wf.get("oos_edge_R", float("nan"))
    n_ok, n_tot = wf.get("oos_positive_folds", 0), wf.get("n_folds_scored", 0)

    if np.isnan(r) or not n_tot:
        return False, ["no out-of-sample fold could be scored"]

    if not null.get("runs"):
        return False, ["no null baseline — nothing to compare against"]

    pv = empirical_p(r, null)
    if pv >= 0.05:
        ok = False
        reasons.append(f"selection edge {r:+.3f}R is not distinguishable from noise "
                       f"(p = {pv:.3f}; {null['runs']} rotated-target runs)")
    else:
        reasons.append(f"selection edge {r:+.3f}R beats the noise distribution "
                       f"(p = {pv:.3f})")

    # EVERY fold, not a majority. Deliberately strict: with 4-6 folds a bare
    # majority happens by coin-flip often enough to wave through random-walk
    # data, and this repo's history is of strategies that passed a loose bar
    # and then lost money. It will also reject some genuine weak edges — that
    # is the trade being made on purpose.
    if n_ok < n_tot:
        ok = False
        reasons.append(f"{n_ok}/{n_tot} folds positive — every fold must be, "
                       "or it is regime-dependent rather than an edge")
    else:
        reasons.append(f"all {n_tot} folds positive")
    return ok, reasons


# ── report ───────────────────────────────────────────────────────────────────
def print_report(res: dict) -> None:
    if res.get("error"):
        print("no data:", res["error"])
        return
    print(f"\n{'=' * 78}")
    print(f"FACTOR MATRIX — {res['rows']:,} bars · {len(res['symbols'])} symbols "
          f"· target = next {res['horizon']} bars in ATR units")
    print("=" * 78)
    print(f"\n{'factor':<12}{'IC':>8}{'t':>8}{'mono':>8}{'lo→hi':>9}   buckets (mean R by quintile)")
    print("-" * 78)
    rows = sorted(res["matrix"].items(), key=lambda kv: -abs(kv[1]["ic"]))
    for name, m in rows:
        if not m["buckets"]:
            continue
        bars = " ".join(f"{b:+.2f}" for b in m["buckets"])
        print(f"{name:<12}{m['ic']:>+8.3f}{m['t']:>8.1f}{m['monotonic']:>+8.2f}"
              f"{m['spread']:>+9.2f}   {bars}")
    print("\n  IC   = rank correlation with forward return (|IC|>0.03 is already notable)")
    print("  t    = t-stat of that IC. |t| < 2 means it is not distinguishable from noise")
    print("  mono = are the quintiles ORDERED? near +1/-1 real, near 0 = noise wearing a suit")

    wf = res["walk_forward"]
    print(f"\n{'-' * 78}\nWALK-FORWARD (weights fitted on the past, scored on unseen folds)")
    print("-" * 78)
    if not wf["folds"]:
        print("  not enough data to score a single out-of-sample fold.")
    else:
        print(f"{'fold':<6}{'train':>8}{'test':>7}{'picked':>8}{'picked R':>10}{'rest R':>9}{'EDGE':>9}{'hit%':>7}")
        for f in wf["folds"]:
            print(f"{f['fold']:<6}{f['train_rows']:>8,}{f['test_rows']:>7,}{f['n_picked']:>8,}"
                  f"{f['mean_R']:>+10.3f}{f['baseline_R']:>+9.3f}{f['edge_R']:>+9.3f}"
                  f"{f['hit_rate'] * 100:>7.1f}")
        print("\n  EDGE = picked minus unpicked. The other columns move with the market;")
        print("         only this one measures whether the SCORE picked better bars.")
        print(f"  out-of-sample edge    : {wf['oos_edge_R']:+.4f} R")
        print(f"  folds positive        : {wf['oos_positive_folds']}/{wf['n_folds_scored']}")

    n = res["null"]
    if n.get("runs"):
        print(f"\n{'-' * 78}\nNULL BASELINE — same pipeline, target shuffled ({n['runs']} runs)")
        print("-" * 78)
        print(f"  noise edge            : {n['mean']:+.4f}  (sd {n['sd']:.4f})")
        print(f"  noise 95th percentile : {n['p95']:+.4f}")
        print(f"  noise best of {n['runs']:<3}     : {n['max']:+.4f}")
    ok, reasons = verdict(wf, n)
    print(f"\n{'-' * 78}\nVERDICT: {'✓ survives both checks' if ok else '✗ NOT an edge'}")
    print("-" * 78)
    for r in reasons:
        print(f"  {'✓' if ok else '·'} {r}")
    if ok:
        print("\n  Both hurdles cleared. That earns a FORWARD test on time this ")
        print("  study never saw — not live size. Every strategy in this repo that")
        print("  later failed also looked fine at exactly this stage.")
    else:
        print("\n  Do not trade this. Adding factors or retuning until it passes is")
        print("  the search that produces overfit — the honest move is a different")
        print("  hypothesis, or more data.")
    print()


# ── entry point ──────────────────────────────────────────────────────────────
def main(argv) -> int:
    days = int(argv[1]) if len(argv) > 1 else 180
    tf = argv[2] if len(argv) > 2 else "1h"
    syms = (argv[3].split(",") if len(argv) > 3 else ["BTC", "ETH", "SOL"])
    symbols = [s if "/" in s else f"{s.strip().upper()}/USDT:USDT" for s in syms]

    import backtest as BT
    panels = {}
    for s in symbols:
        try:
            oh = BT.fetch_ohlcv(s, tf, days)
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the run
            print(f"{s}: fetch failed ({exc})")
            continue
        if len(oh) < 300:
            print(f"{s}: only {len(oh)} candles, skipped")
            continue
        panels[s] = panel_for(s, oh)
        print(f"{s}: {len(oh)} {tf} candles → {len(panels[s]):,} usable rows")

    if not panels:
        print("no usable data — nothing to measure.")
        return 1
    print_report(analyse(panels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
