"""Lower-level building blocks the strategy relies on: the volume gate, ATR/EMA,
the BTC regime classifier, and the no-look-ahead `as_of` lookup.
"""
import backtest as BT
import bot
import config as C
from indicators import check_volume_gate


def test_volume_gate_passes_on_surge():
    vols = [1000.0] * 20 + [2000.0]              # last bar 2× the 20-bar average
    assert check_volume_gate(vols, multiplier=1.5)


def test_volume_gate_blocks_without_surge():
    vols = [1000.0] * 20 + [1000.0]
    assert not check_volume_gate(vols, multiplier=1.5)


def test_volume_gate_needs_enough_history():
    assert not check_volume_gate([1000.0] * 5, multiplier=1.5)


def test_calculate_atr_simple_mean_of_true_range():
    # constant 1.0 range every bar → ATR == 1.0 (bot uses a simple mean of TR)
    oh = [[i, 100, 100.5, 99.5, 100, 1] for i in range(30)]
    assert bot.calculate_atr(oh) == 1.0


def test_calculate_ema_returns_none_when_too_short():
    assert bot.calculate_ema([1, 2, 3], 200) is None
    assert bot.calculate_ema(list(range(250)), 200) is not None


def test_btc_regime_bull_bear_neutral():
    n = C.BTC_REGIME_EMA + C.BTC_REGIME_SLOPE_LOOKBACK + 20
    rising = [[i, 0, 0, 0, 100 + i * 1.0, 0] for i in range(n)]
    falling = [[i, 0, 0, 0, 100 - i * 0.5, 0] for i in range(n)]
    flat = [[i, 0, 0, 0, 100.0, 0] for i in range(n)]
    assert BT.btc_regime_series(rising)[-1][1] == "bull"
    assert BT.btc_regime_series(falling)[-1][1] == "bear"
    assert BT.btc_regime_series(flat)[-1][1] == "neutral"


def test_as_of_has_no_look_ahead():
    series = [(10, "a"), (20, "b"), (30, "c")]
    assert BT.as_of(series, 25, "default") == "b"    # most recent at/<= ts
    assert BT.as_of(series, 30, "default") == "c"    # inclusive of ts
    assert BT.as_of(series, 5, "default") == "default"  # nothing yet → default
