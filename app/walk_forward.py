"""
walk_forward.py — non-overlapping out-of-sample re-validation of the LIVE S1
strategy, against a point-in-time symbol universe so today's top-volume
coins can't inflate historical folds with hindsight.

WHY THIS EXISTS: on 2026-06-22 a walk-forward test (this file's predecessor,
deleted in the 2026-06-28 strategy-registry consolidation) found the live
strategy net-negative over 360 days — positive in only 2 of 6 folds, both
the most recent (classic tuned-to-recent-regime overfit). Survivorship bias
was ruled out separately (a point-in-time-universe rebuild, also deleted,
confirmed the same verdict). Since then the live signal engine has grown
from 5 "lights" to 7 (added Volume Profile + Order Flow) plus several new
hard gates (4h trend filter, EMA200 macro filter, hidden-divergence check,
volume gate, RSI-cross-after-extreme) — the old verdict may not still apply.
This measures the CURRENT bot, not the one that failed a year ago.

Reuses backtest.py's engine exactly as the Performance page's
run_backtest() does — fetch_ohlcv / evaluate (which calls bot.py's OWN live
signal functions) / simulate_trade / apply_costs / ema_series_4h /
btc_regime_series / as_of / summarize — and does NOT modify backtest.py.
The only new logic here is fold-slicing and a point-in-time symbol universe
(each fold trades only the top-N symbols by TRAILING dollar volume as of
that fold's own start, computed from each symbol's own fetched candles —
not backtest.top_symbols()'s live ticker snapshot, which is today's ranking
applied blindly across all of history).

Read-only measurement. Does not touch bot.py, backtest.py, or any live
trading path.

Usage:
    python walk_forward.py [days] [n_folds] [n_candidates] [top_n_per_fold]
    python walk_forward.py 360 6 100 30
"""
import bisect
import sys
import time

import backtest as BT
import market_data

TF = "1h"
VOL_LOOKBACK_BARS = 24 * 30    # trailing 30 days of volume for the point-in-time rank


def fold_bounds_by_time(reference_ohlcv, days, n_folds):
    """N equal-duration, non-overlapping (lo_ts, hi_ts) windows spanning the
    requested `days`, anchored on a reference series' own timestamps (BTC —
    fetched with full history and no listing gaps) so every symbol's fold N
    means the same calendar period, not the same bar index."""
    if not reference_ohlcv:
        return []
    now_ts = reference_ohlcv[-1][0]
    start_ts = now_ts - days * 86400 * 1000
    step = (now_ts - start_ts) // n_folds
    bounds = []
    for k in range(n_folds):
        lo = start_ts + k * step
        hi = now_ts if k == n_folds - 1 else start_ts + (k + 1) * step
        bounds.append((lo, hi))
    return bounds


def _ts_index(ohlcv, ts):
    """First index whose timestamp is >= ts (bisect on the sorted ts column)."""
    return bisect.bisect_left([c[0] for c in ohlcv], ts)


def trailing_dollar_volume(ohlcv, end_idx, lookback_bars=VOL_LOOKBACK_BARS):
    """close*volume summed over `lookback_bars` ending at end_idx (exclusive)
    — a point-in-time popularity proxy from the symbol's OWN history, not a
    live ticker snapshot. Zero if there isn't enough history yet."""
    start = max(0, end_idx - lookback_bars)
    window = ohlcv[start:end_idx]
    return sum(c[4] * c[5] for c in window) if window else 0.0


def fetch_universe_data(candidates, days, progress=print):
    """{symbol: {'oh1h': [...], 'ema4h': [(ts,val)...]}} for every candidate
    with enough history, fetched ONCE (not per fold)."""
    data = {}
    for i, sym in enumerate(candidates, 1):
        try:
            oh1h = BT.fetch_ohlcv(sym, TF, days)
            oh4h = BT.fetch_ohlcv(sym, "4h", days + 10)
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the run
            progress(f"[{i}/{len(candidates)}] {sym}: fetch failed ({exc})")
            continue
        if len(oh1h) < BT.WINDOW + 24:
            progress(f"[{i}/{len(candidates)}] {sym}: too little history, skipped")
            continue
        data[sym] = {"oh1h": oh1h, "ema4h": BT.ema_series_4h(oh4h)}
        progress(f"[{i}/{len(candidates)}] {sym}: {len(oh1h)} 1h candles")
    return data


def simulate_fold(data, fold_universe, lo_ts, hi_ts, btc_reg):
    """Run backtest.evaluate/simulate_trade/apply_costs over ONE fold's
    (lo_ts, hi_ts) window, for symbols in fold_universe only. Returns the raw
    trade list (feed to backtest.summarize)."""
    trades = []
    for sym in fold_universe:
        d = data[sym]
        oh1h, ema4h = d["oh1h"], d["ema4h"]
        idx_lo = max(_ts_index(oh1h, lo_ts), BT.WINDOW)
        idx_hi = _ts_index(oh1h, hi_ts)
        i = idx_lo
        n = idx_hi
        while i < n - 1:
            oh = oh1h[i - BT.WINDOW + 1:i + 1]
            ts = oh1h[i][0]
            res = BT.evaluate(oh, BT.as_of(ema4h, ts, None), BT.as_of(btc_reg, ts, "neutral"))
            if res:
                is_long, entry, sl, tp1, tp2, eff = res
                out = BT.simulate_trade(oh1h, i, is_long, entry, sl, tp1, tp2)
                if out:
                    pnl, close_bar = out
                    pnl = BT.apply_costs(pnl, close_bar - i, 1.0, realism=True)
                    R = abs(entry - sl) / entry * 100
                    trades.append({
                        "symbol": sym.split("/")[0],
                        "dir": "LONG" if is_long else "SHORT",
                        "lights": int(eff), "pnl": round(float(pnl), 2), "R": round(float(R), 2),
                        "rr": round(float(pnl) / R, 2) if R else 0.0, "win": bool(pnl > 0),
                        "ts": int(oh1h[close_bar][0]),
                    })
                    i = close_bar + 1
                    continue
            i += 1
    return trades


def _drop_tradfi_perps(symbols, progress=print):
    """The live bot never trades Binance's TradFi stock/ETF/commodity perps
    (SKHYNIX, XAU, CL, ...) — orders there are a guaranteed -4411 rejection
    without a separately-signed agreement (config.EXCLUDE_TRADFI_PERPS). If
    they're left in the candidate pool here, a fold could "trade" symbols S1
    structurally cannot touch live, corrupting the whole point of this test."""
    markets = getattr(BT.ex, "markets", None) or {}
    if not markets:
        progress("(markets not loaded — cannot filter TradFi perps, leaving pool as-is)")
        return symbols
    kept = [s for s in symbols if not market_data.is_tradfi_market(markets.get(s))]
    dropped = len(symbols) - len(kept)
    if dropped:
        progress(f"Dropped {dropped} TradFi stock/commodity perp(s) from the candidate pool "
                 f"(S1 can't structurally trade these)")
    return kept


def run(days=360, n_folds=6, n_candidates=100, top_n_per_fold=30, progress=print):
    candidates = _drop_tradfi_perps(BT.top_symbols(n_candidates), progress)
    progress(f"Candidate pool: {len(candidates)} symbols (today's top-{n_candidates} by "
             f"volume, TradFi perps excluded — the one residual bias: coins delisted "
             f"since can't appear here)")

    progress("Fetching BTC regime history...")
    btc1h = BT.fetch_ohlcv("BTC/USDT:USDT", TF, days + 5)
    btc_reg = BT.btc_regime_series(btc1h)

    data = fetch_universe_data(candidates, days, progress)
    if not data:
        raise RuntimeError("no symbols fetched — check network/ccxt access")

    bounds = fold_bounds_by_time(btc1h, days, n_folds)
    fold_results = []
    for fi, (lo_ts, hi_ts) in enumerate(bounds, 1):
        ranked = sorted(
            ((sym, trailing_dollar_volume(d["oh1h"], _ts_index(d["oh1h"], lo_ts)))
             for sym, d in data.items()),
            key=lambda kv: kv[1], reverse=True,
        )
        fold_universe = [sym for sym, vol in ranked[:top_n_per_fold] if vol > 0]

        t0 = time.time()
        trades = simulate_fold(data, fold_universe, lo_ts, hi_ts, btc_reg)
        summary = BT.summarize(trades, time.time() - t0,
                                round((hi_ts - lo_ts) / 86400000, 1), len(fold_universe))
        summary["fold"] = fi
        summary["universe"] = fold_universe
        fold_results.append(summary)
        progress(f"Fold {fi}: {summary['total']} trades, WR {summary['win_rate']}%, "
                 f"E[R] {summary['expectancy_r']:+.3f}, total R {summary['total_r']:+.2f}, "
                 f"universe={len(fold_universe)} syms")

    return fold_results


def print_report(fold_results):
    print("\n" + "=" * 78)
    print("  S1 WALK-FORWARD — CURRENT LIVE LOGIC, POINT-IN-TIME UNIVERSE")
    print("=" * 78)
    profitable = 0
    all_trades_n = 0
    all_r = 0.0
    for r in fold_results:
        tag = "  " if r["total_r"] > 0 else "⚠ "
        profitable += 1 if r["total_r"] > 0 else 0
        all_trades_n += r["total"]
        all_r += r["total_r"]
        print(f"{tag}Fold {r['fold']}  n={r['total']:4d}  WR={r['win_rate']:5.1f}%  "
              f"E[R]={r['expectancy_r']:+.3f}  totalR={r['total_r']:+7.2f}  "
              f"maxDD(R)={r['max_drawdown_r']:+.2f}")
    print("-" * 78)
    n_folds = len(fold_results)
    print(f"  {profitable}/{n_folds} folds profitable — {all_trades_n} trades total, "
          f"{all_r:+.2f}R aggregate")
    print("\n  2026-06-22 finding (old Binance S1, since evolved): 2/6 folds profitable "
          "(both most recent), −0.086R/trade biased, −0.099R/trade PIT-corrected.")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 360
    n_folds = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    n_candidates = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    top_n_per_fold = int(sys.argv[4]) if len(sys.argv) > 4 else 30
    results = run(days, n_folds, n_candidates, top_n_per_fold)
    print_report(results)
