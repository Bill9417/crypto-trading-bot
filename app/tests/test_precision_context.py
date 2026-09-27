"""🎯 Precision, the way this repo defines it: more of the things that were
MEASURED to matter, one implementation each, and nothing that votes without
a number behind it.

2026-09-27. Three things pinned here:
  · the BTC regime rule exists ONCE (indicators) and the bot, the S2 scanner
    and the backtester all read it — three copies of the single biggest gate
    in the system had drifted apart once already;
  · S1_MACD_GATE ships OFF and is read by bot.py and backtest.py through the
    same helper, like every other gate;
  · the S2 outcome tracker cohorts signals by 1h ATR% so /reality can say
    whether S1's low-volatility finding holds for S2 — measured, not assumed.
"""
import inspect

import config as C
import indicators as I


# ── one regime rule ──────────────────────────────────────────────────────────
def _closes(n, step):
    return [100.0 + i * step for i in range(n)]


def test_the_shared_regime_rule_reads_bull_bear_neutral():
    n = C.BTC_REGIME_EMA + C.BTC_REGIME_SLOPE_LOOKBACK + 20
    assert I.btc_regime_from_closes(_closes(n, 1.0)) == "bull"
    assert I.btc_regime_from_closes(_closes(n, -0.5)) == "bear"
    assert I.btc_regime_from_closes([100.0] * n) == "neutral"
    assert I.btc_regime_from_closes(_closes(10, 1.0)) == "neutral"     # too short
    assert I.btc_regime_from_closes([]) == "neutral"


def test_the_series_and_the_scalar_agree_on_every_bar():
    n = C.BTC_REGIME_EMA + C.BTC_REGIME_SLOPE_LOOKBACK + 30
    closes = _closes(n, 0.8)
    series = I.btc_regime_series_from_closes(closes)
    assert len(series) == n
    for i in range(1, n):
        assert I.btc_regime_from_closes(closes[:i]) == series[i - 1]


def test_every_regime_reader_uses_the_one_rule():
    import backtest
    import bot
    import strategy2_scanner
    for mod, fn in ((bot, bot.check_btc_regime),
                    (strategy2_scanner, strategy2_scanner._btc_regime),
                    (backtest, backtest.btc_regime_series)):
        src = inspect.getsource(fn)
        assert "btc_regime" in src and "ewm(" not in src, \
            f"{mod.__name__} still carries its own copy of the regime rule"


def test_the_backtest_series_matches_the_bot_rule_bar_for_bar():
    import backtest
    n = C.BTC_REGIME_EMA + C.BTC_REGIME_SLOPE_LOOKBACK + 25
    bars = [[i, 0, 0, 0, 100 + i * 0.7, 0] for i in range(n)]
    series = backtest.btc_regime_series(bars)
    assert series[-1][1] == "bull"
    assert series[-1][1] == I.btc_regime_from_closes([b[4] for b in bars])


# ── MACD gate ────────────────────────────────────────────────────────────────
def test_macd_gate_defaults_off():
    assert C.S1_MACD_GATE is False


def test_macd_hist_sign_follows_momentum():
    up = [100 + i * 1.0 for i in range(80)]
    down = [200 - i * 1.0 for i in range(80)]
    assert I.macd_hist_last(up) > 0
    assert I.macd_hist_last(down) < 0
    assert I.macd_hist_last([1.0, 2.0, 3.0]) is None          # too short


def test_bot_and_backtest_read_the_same_macd_gate():
    import backtest
    import bot
    for mod in (bot, backtest):
        src = inspect.getsource(mod)
        assert "S1_MACD_GATE" in src, f"{mod.__name__} does not read the gate"
        assert "macd_hist_last(" in src, f"{mod.__name__} re-implements the MACD read"


# ── resampling for the 1h ATR cohort ─────────────────────────────────────────
def test_resample_aggregates_and_drops_the_incomplete_tail():
    bars = [[i, 10 + i, 12 + i, 9 + i, 11 + i, 5] for i in range(10)]
    out = I.resample_ohlcv(bars, 4)
    assert len(out) == 2                                     # 10 // 4
    assert out[0] == [0, 10.0, 15.0, 9.0, 14.0, 20.0]        # o=first, h=max, l=min, c=last, v=sum
    assert out[1][0] == 4 and out[1][4] == 18.0
    assert I.resample_ohlcv(bars, 1) == bars


# ── the low-volatility cohort ────────────────────────────────────────────────
def test_lowvol_is_a_cohort_keyed_on_the_shared_ceiling():
    import reality
    import signal_outcomes as SO
    assert "lowvol" in reality.COHORT_LABEL
    assert "lowvol" in SO.cohorts_of({"direction": "long", "atr_pct_1h": C.LOWVOL_ATR_PCT})
    assert "lowvol" not in SO.cohorts_of({"direction": "long", "atr_pct_1h": C.LOWVOL_ATR_PCT + 0.5})
    assert "lowvol" not in SO.cohorts_of({"direction": "long"})          # unknown ≠ low
    assert "atr_pct_1h" in SO._SNAP_KEYS, "the snapshot would drop the reading before evaluation"


def test_every_lowvol_reader_shares_one_number():
    import paper_tracker
    assert paper_tracker.LOWVOL_MAX_ATR_PCT == C.LOWVOL_ATR_PCT
    assert C.LOWVOL_ATR_PCT > 0
