"""walk_forward.py — the NEW pure-logic pieces only (fold-slicing, point-in-
time volume ranking). backtest.py's own network functions (fetch_ohlcv,
top_symbols) have no isolated unit tests either — only real usage — so this
follows the same precedent rather than inventing network mocking that
nothing else in the codebase does.
"""
import walk_forward as WF

DAY = 86400000


def _candles(n, start_ts=0, step_ms=3600000, close=100.0, vol=10.0):
    """[ts, o, h, l, c, v] rows, one per hour by default."""
    return [[start_ts + i * step_ms, close, close, close, close, vol] for i in range(n)]


# ── fold_bounds_by_time ──────────────────────────────────────────────────────
def test_fold_bounds_are_contiguous_and_cover_the_full_window():
    now = 1_000 * DAY
    ref = [[now - 400 * DAY, 0, 0, 0, 0, 0], [now, 0, 0, 0, 0, 0]]   # only need [-1][0]=now
    bounds = WF.fold_bounds_by_time(ref, days=360, n_folds=6)
    assert len(bounds) == 6
    assert bounds[0][0] == now - 360 * DAY
    assert bounds[-1][1] == now
    for k in range(5):
        assert bounds[k][1] == bounds[k + 1][0]        # no gaps, no overlap


def test_fold_bounds_empty_reference_is_safe():
    assert WF.fold_bounds_by_time([], days=360, n_folds=6) == []


# ── _ts_index ────────────────────────────────────────────────────────────────
def test_ts_index_finds_first_at_or_after():
    rows = _candles(5, start_ts=0)          # ts = 0, 3.6M, 7.2M, 10.8M, 14.4M
    assert WF._ts_index(rows, 0) == 0
    assert WF._ts_index(rows, 3_600_001) == 2       # first ts >= that is index 2
    assert WF._ts_index(rows, 3_600_000) == 1       # exact match
    assert WF._ts_index(rows, 999_999_999) == 5     # past the end


# ── trailing_dollar_volume ───────────────────────────────────────────────────
def test_trailing_dollar_volume_sums_close_times_volume_in_the_window():
    rows = _candles(10, close=100.0, vol=2.0)        # each bar = 200 dollar-volume
    # last 5 bars (indices 5..9) ending at end_idx=10
    assert WF.trailing_dollar_volume(rows, end_idx=10, lookback_bars=5) == 200.0 * 5


def test_trailing_dollar_volume_never_looks_past_end_idx():
    """THE POINT of this function: a huge future volume spike must not leak
    into a point-in-time rank computed as of an earlier bar."""
    rows = _candles(10, close=100.0, vol=1.0)
    rows[8] = [rows[8][0], 100.0, 100.0, 100.0, 100.0, 1_000_000.0]   # spike AFTER end_idx
    vol_asof_bar5 = WF.trailing_dollar_volume(rows, end_idx=5, lookback_bars=30)
    assert vol_asof_bar5 == 100.0 * 1.0 * 5          # only bars 0..4, spike excluded


def test_trailing_dollar_volume_zero_with_no_history_yet():
    rows = _candles(10)
    assert WF.trailing_dollar_volume(rows, end_idx=0) == 0.0


# ── simulate_fold wiring (stubbed backtest engine, no network) ───────────────
def test_simulate_fold_only_evaluates_within_the_fold_window(monkeypatch):
    """A symbol's data spans the whole history; simulate_fold must only look
    at bars inside [lo_ts, hi_ts) for entries — proves the fold boundary is
    actually applied, not just decorative."""
    import backtest as BT

    n = BT.WINDOW + 20
    rows = _candles(n, start_ts=0)
    data = {"FAKE/USDT:USDT": {"oh1h": rows, "ema4h": []}}

    seen_ts = []

    def fake_evaluate(oh, ema, btc_reg):
        seen_ts.append(oh[-1][0])
        return None    # never actually trade — just record what got evaluated

    monkeypatch.setattr(BT, "evaluate", fake_evaluate)

    lo_ts = rows[BT.WINDOW + 5][0]
    hi_ts = rows[BT.WINDOW + 10][0]
    WF.simulate_fold(data, ["FAKE/USDT:USDT"], lo_ts, hi_ts, btc_reg=[])

    assert seen_ts, "evaluate() was never called"
    assert min(seen_ts) >= lo_ts
    assert max(seen_ts) < hi_ts
