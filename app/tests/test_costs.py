"""Realism cost model — backtest.apply_costs.

The backtest's headline numbers are flattered by exact-price fills and ignored
funding. `apply_costs` is the pessimistic adjustment: fees always, plus slippage
on both fills and funding per 8h held when realism is on. These tests pin that
math so the "realistic vs fees-only" gap can't silently drift.

Expected values are computed from the module constants (BT.FEE_PCT etc.) rather
than hard-coded, so the tests stay correct if you retune the defaults.
"""
import pytest

import backtest as BT


def test_fees_only_just_deducts_the_fee():
    # realism off → only the round-trip fee comes off, nothing else
    net = BT.apply_costs(10.0, bars_held=50, tf_hours=1.0, realism=False)
    assert net == pytest.approx(10.0 - BT.FEE_PCT)


def test_realism_adds_slippage_and_funding():
    net = BT.apply_costs(10.0, bars_held=16, tf_hours=1.0, realism=True)
    expected = (10.0
                - BT.FEE_PCT
                - 2 * BT.SLIPPAGE_PCT
                - BT.FUNDING_PCT_PER_8H * (16 * 1.0 / 8.0))
    assert net == pytest.approx(expected)


def test_realism_is_never_cheaper_than_fees_only():
    raw, held, tf = 5.0, 30, 1.0
    assert BT.apply_costs(raw, held, tf, realism=True) <= BT.apply_costs(raw, held, tf, realism=False)


def test_funding_scales_with_hold_time():
    # holding twice as long pays more funding → lower net pnl
    short_hold = BT.apply_costs(0.0, bars_held=8, tf_hours=1.0, realism=True)
    long_hold = BT.apply_costs(0.0, bars_held=80, tf_hours=1.0, realism=True)
    assert long_hold < short_hold


def test_higher_timeframe_accrues_more_funding_per_bar():
    # 10 bars of 4h = 40h held vs 10h on 1h → more funding on the 4h run
    one_h = BT.apply_costs(0.0, bars_held=10, tf_hours=1.0, realism=True)
    four_h = BT.apply_costs(0.0, bars_held=10, tf_hours=4.0, realism=True)
    assert four_h < one_h


def test_zero_hold_charges_slippage_but_no_funding():
    net = BT.apply_costs(0.0, bars_held=0, tf_hours=1.0, realism=True)
    assert net == pytest.approx(0.0 - BT.FEE_PCT - 2 * BT.SLIPPAGE_PCT)


def test_negative_hold_is_clamped_no_funding_credit():
    # a degenerate bars_held must never *add* profit via negative funding
    net = BT.apply_costs(0.0, bars_held=-5, tf_hours=1.0, realism=True)
    assert net == pytest.approx(0.0 - BT.FEE_PCT - 2 * BT.SLIPPAGE_PCT)
