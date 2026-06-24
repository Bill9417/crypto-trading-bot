"""Trade-management simulators — backtest.simulate_trade_trailing (Chandelier ATR
trailing, used by S3/S4) and backtest.simulate_trade (1R/2R bracket, used by S1/S2).

Candles are [ts, open, high, low, close, volume]; signal bar index is passed as `i`.
A flat 21-bar base gives ATR≈1 → trail≈3 (CHANDELIER_ATR_MULT) so the ratchet
levels are predictable.
"""
import pytest

import backtest as BT


def _flat_base(level=100.0, n=21):
    return [[i, level, level + 0.5, level - 0.5, level, 1000] for i in range(n)]


def test_trailing_winner_exits_on_ratcheted_stop():
    oh = _flat_base()
    oh += [
        [21, 100, 101, 99.9, 101, 1000],   # fills (low ≤ entry 100)
        [22, 101, 105, 101, 105, 1000],    # peak 105 → stop 102
        [23, 105, 110, 104, 110, 1000],    # peak 110 → stop 107
        [24, 110, 110, 106, 108, 1000],    # low 106 ≤ stop 107 → exit at 107
        [25, 108, 109, 107, 108, 1000],
    ]
    pnl, close_bar = BT.simulate_trade_trailing(oh, 20, True, 100.0, 98.0, 101.0, 102.0)
    assert pnl == pytest.approx(7.0)        # (107-100)/100*100
    assert close_bar == 24


def test_trailing_loser_hits_initial_stop():
    oh = _flat_base()
    oh += [[21, 100, 100.2, 99.0, 99.5, 1000], [22, 99.5, 99.6, 97.0, 97.5, 1000]]
    pnl, close_bar = BT.simulate_trade_trailing(oh, 20, True, 100.0, 98.0, 101.0, 102.0)
    assert pnl == pytest.approx(-2.0)       # stopped at 98
    assert close_bar == 22


def test_trailing_never_fills_returns_none():
    oh = _flat_base(n=28)
    assert BT.simulate_trade_trailing(oh, 20, True, 90.0, 88.0, 91.0, 92.0) is None


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
