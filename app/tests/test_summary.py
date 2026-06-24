"""Aggregation + the outlier-dependence metrics — backtest.summarize.

The outlier metrics exist to flag a fragile edge (a few monster winners carrying
the whole result), so they need to be correct.
"""
import backtest as BT


def _t(rr, ts):
    return {"rr": rr, "pnl": rr * 10, "win": rr > 0, "dir": "LONG", "lights": 5, "ts": ts}


def test_outlier_concentration_flags_few_big_winners():
    # +5,+3,+2 from the 3 best, two -1 losers → net +8R, but top-3 = +10R
    trades = [_t(5, 1), _t(3, 2), _t(2, 3), _t(-1, 4), _t(-1, 5)]
    r = BT.summarize(trades, 1.0, 30, 5)
    assert r["total_r"] == 8.0
    assert r["top3_r"] == 10.0
    assert r["top3_share_pct"] == 125.0          # >100% → the rest is net-negative
    assert r["total_r_ex_top3"] == -2.0          # without the outliers, no edge


def test_basic_stats():
    trades = [_t(2, 1), _t(2, 2), _t(-1, 3), _t(-1, 4)]
    r = BT.summarize(trades, 1.0, 30, 4)
    assert r["total"] == 4
    assert r["wins"] == 2 and r["losses"] == 2
    assert r["win_rate"] == 50.0
    assert r["expectancy_r"] == 0.5              # (2+2-1-1)/4
    assert r["avg_win_r"] == 2.0 and r["avg_loss_r"] == -1.0


def test_all_losses_have_no_top3_share():
    trades = [_t(-1, i) for i in range(4)]
    r = BT.summarize(trades, 1.0, 30, 4)
    assert r["total_r"] == -4.0
    assert r["top3_share_pct"] is None           # undefined when net R ≤ 0
    assert r["max_drawdown_r"] == -4.0           # monotonic underwater


def test_equity_curve_is_chronological_and_complete():
    trades = [_t(1, 30), _t(2, 10), _t(-1, 20)]  # out of order on purpose
    r = BT.summarize(trades, 1.0, 30, 3)
    assert len(r["curve"]) == 3
    cum = [pt["cum_r"] for pt in r["curve"]]
    assert cum == [2.0, 1.0, 2.0]                # sorted by ts: +2, -1, +1
