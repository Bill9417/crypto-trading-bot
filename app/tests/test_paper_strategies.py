"""Multi-strategy dry-run engine (paper_s4): per-strategy state, active-strategy
selection, legacy migration, and the bracket vs trailing management dispatch.

All file I/O is redirected to tmp_path so these never touch the live state files.
"""
import json

import pytest

import backtest as BT
import paper_s4 as P

H = 3_600_000


def _base(n=21, lvl=100.0):
    return [[i * H, lvl, lvl + 0.5, lvl - 0.5, lvl, 1000] for i in range(n)]


def test_manage_mapping():
    assert P._manage_for("default") == "bracket"
    assert P._manage_for("trend_breakout") == "bracket"
    assert P._manage_for("trend_trailing") == "trailing"


def test_active_strategy_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "ACTIVE_FILE", str(tmp_path / "active.json"))
    assert P.get_active() == P.DEFAULT_STRATEGY          # absent → default
    assert P.set_active("trend_breakout") == "trend_breakout"
    assert P.get_active() == "trend_breakout"
    with pytest.raises(ValueError):
        P.set_active("bogus")
    assert P.get_active() == "trend_breakout"            # unchanged after a bad set


def test_bracket_advance_matches_simulate_trade_winner():
    oh = _base() + [[21*H, 100, 101, 99.9, 101, 1000],
                    [22*H, 101, 103, 101, 103, 1000],    # TP1 (102) → partial
                    [24*H, 103, 106, 103, 106, 1000]]    # TP2 (104) → full
    t = P._new_trade("AAA/USDT:USDT", (True, 100.0, 98.0, 102.0, 104.0, 5), 20*H, oh[:21], "bracket")
    P._advance(t, oh)
    assert t["status"] == "CLOSED"
    assert t["pnl_pct"] == round(3.0 - BT.FEE_PCT, 3)    # half +2% + half +4%
    assert t["win"] is True


def test_bracket_advance_loser_hits_stop():
    oh = _base() + [[21*H, 100, 101, 99.9, 100.5, 1000],
                    [22*H, 100.5, 100.6, 97, 97.5, 1000]]
    t = P._new_trade("AAA/USDT:USDT", (True, 100.0, 98.0, 102.0, 104.0, 5), 20*H, oh[:21], "bracket")
    P._advance(t, oh)
    assert t["status"] == "CLOSED"
    assert t["pnl_pct"] == round(-2.0 - BT.FEE_PCT, 3)
    assert t["win"] is False


def test_bracket_advance_breakeven_runner():
    oh = _base() + [[21*H, 100, 101, 99.9, 101, 1000],
                    [22*H, 101, 103, 101, 103, 1000],    # TP1 → partial
                    [23*H, 103, 103, 99.5, 100, 1000]]   # back to breakeven (entry)
    t = P._new_trade("AAA/USDT:USDT", (True, 100.0, 98.0, 102.0, 104.0, 5), 20*H, oh[:21], "bracket")
    P._advance(t, oh)
    assert t["status"] == "CLOSED"
    assert t["pnl_pct"] == round(1.0 - BT.FEE_PCT, 3)    # only the TP1 half is booked


def test_trailing_advance_unchanged_winner():
    oh = _base() + [[21*H, 100, 101, 99.9, 101, 1000],
                    [22*H, 101, 105, 101, 105, 1000],    # peak 105 → stop 102
                    [23*H, 105, 110, 104, 110, 1000],    # peak 110 → stop 107
                    [24*H, 110, 110, 106, 108, 1000]]    # low 106 ≤ 107 → exit 107
    t = P._new_trade("AAA/USDT:USDT", (True, 100.0, 98.0, 101.0, 102.0, 5), 20*H, oh[:21], "trailing")
    P._advance(t, oh)
    assert t["status"] == "CLOSED"
    assert t["pnl_pct"] == round(7.0 - BT.FEE_PCT, 3)
