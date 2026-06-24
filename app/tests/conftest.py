"""Shared fixtures: synthetic OHLCV generators tuned to drive Strategy 4 through
its gates. Each candle is [ts, open, high, low, close, volume].

The shape is deliberate: a choppy base (low/flat ADX) followed by a clean
directional leg (so ADX is high AND *rising* into the last bar), with a decisive
breakout + volume surge on the final candle. This satisfies every S4 filter:
Donchian breakout + ATR buffer, EMA200 side, ADX≥25 & rising, volume gate.
"""
import math

import pytest

HOUR_MS = 3_600_000


def _uptrend(n=230, base=100.0):
    oh = []
    for i in range(n):
        if i < 190:                                  # choppy base → low ADX
            mid, vol = base + 3.0 * math.sin(i / 3.0), 1000.0
        else:                                        # clean strong climb → ADX rises
            mid, vol = base + 3.0 + 1.6 * (i - 189), 1100.0
        oh.append([i * HOUR_MS, mid - 0.3, mid + 0.5, mid - 0.8, mid, vol])
    prev_close = oh[-2][4]                            # decisive breakout + volume surge
    bo = prev_close + 5.0
    oh[-1] = [oh[-1][0], prev_close, bo + 0.3, prev_close - 0.2, bo, 4000.0]
    return oh


def _downtrend(n=230, base=300.0):
    oh = []
    for i in range(n):
        if i < 190:
            mid, vol = base + 3.0 * math.sin(i / 3.0), 1000.0
        else:
            mid, vol = base + 3.0 - 1.6 * (i - 189), 1100.0
        oh.append([i * HOUR_MS, mid + 0.3, mid + 0.8, mid - 0.5, mid, vol])
    prev_close = oh[-2][4]
    bd = prev_close - 5.0
    oh[-1] = [oh[-1][0], prev_close, prev_close + 0.2, bd - 0.3, bd, 4000.0]
    return oh


def _flat(n=230, base=100.0):
    return [[i * HOUR_MS, base, base + 0.5, base - 0.5, base, 1000.0] for i in range(n)]


@pytest.fixture
def uptrend():
    """OHLCV that produces a qualifying S4 LONG (in a BTC bull regime)."""
    return _uptrend()


@pytest.fixture
def downtrend():
    """OHLCV that would produce a SHORT breakdown (blocked by longs-only)."""
    return _downtrend()


@pytest.fixture
def flat():
    """Sideways OHLCV — no breakout, no signal."""
    return _flat()
