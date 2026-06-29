"""Trade-management simulator — backtest.simulate_trade (the 1R/2R bracket used by
Strategy 1). The Chandelier-trailing simulator was removed with Strategies 2–5 on
2026-06-28.

Candles are [ts, open, high, low, close, volume]; the signal bar index is passed
as `i`. A flat 21-bar base gives ATR≈1 so the fill/TP/SL levels are predictable.
"""
import pytest

import backtest as BT


def _flat_base(level=100.0, n=21):
    return [[i, level, level + 0.5, level - 0.5, level, 1000] for i in range(n)]


def test_bracket_winner_books_tp1_then_tp2():
    oh = _flat_base()
    oh += [
        [21, 100, 101, 99.9, 101, 1000],    # fills
        [22, 101, 103, 101, 103, 1000],     # hits TP1 (102) → partial
        [24, 103, 106, 103, 106, 1000],     # hits TP2 (104) → full
    ]
    pnl, close_bar = BT.simulate_trade(oh, 20, True, 100.0, 98.0, 102.0, 104.0)
    # half at +2% (TP1) + half at +4% (TP2) = 3.0%
    assert pnl == pytest.approx(3.0)
    assert close_bar == 23


def test_bracket_loser_hits_stop_before_tp1():
    oh = _flat_base()
    oh += [[21, 100, 101, 99.9, 100.5, 1000], [22, 100.5, 100.6, 97.0, 97.5, 1000]]
    pnl, close_bar = BT.simulate_trade(oh, 20, True, 100.0, 98.0, 102.0, 104.0)
    assert pnl == pytest.approx(-2.0)
