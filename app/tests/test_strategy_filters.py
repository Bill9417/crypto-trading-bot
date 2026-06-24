"""Optional research-backed filters (ADX, funding, same-direction cap, risk
sizing) added 2026-06-22.

SAFETY: pure unit tests — the executor helpers are exercised with monkeypatched
snapshots / exchange stubs, so NOTHING reaches Binance and no order is sent.
These are safe to run while the live bot trades.

The headline guarantee these tests lock in: every new filter ships DEFAULT OFF,
so the live bot's behaviour is unchanged until a flag is deliberately set.
"""
import math

import config as C
import executor as E
from indicators import adx_components, calculate_adx


# ── Defaults must stay OFF (live-safety regression guard) ────────────────────
def test_all_new_filters_default_off():
    assert C.ENABLE_ADX_FILTER is False
    assert C.ENABLE_FUNDING_FILTER is False
    assert C.ENABLE_DIRECTION_CAP is False
    assert C.ENABLE_RISK_SIZING is False
    # Sane default knobs.
    assert C.ADX_MIN_THRESHOLD == 20
    assert C.MAX_SAME_DIRECTION == 0          # 0 = unlimited even if cap turned on
    assert 0 < C.RISK_PCT_PER_TRADE <= 0.1    # never a reckless default


# ── ADX trend-strength indicator ─────────────────────────────────────────────
def _candles(closes, spread=0.5):
    return [[0, c, c + spread, c - spread, c, 100] for c in closes]


def test_adx_high_in_trend_low_in_chop():
    trend = _candles([100 + 0.7 * i + 0.3 * math.sin(i) for i in range(160)])
    chop = _candles([100 + (0.6 if i % 2 else -0.6) for i in range(160)])
    adx_trend = adx_components(trend, 14)["adx"]
    adx_chop = adx_components(chop, 14)["adx"]
    assert adx_trend > 25          # a real trend clears the gate
    assert adx_chop < 10           # 1-bar chop is filtered out
    assert adx_trend > adx_chop


def test_adx_direction_and_rising_keys():
    up = _candles([100 + i for i in range(60)])
    res = adx_components(up, 14)
    assert res["plus_di"] > res["minus_di"]            # +DI dominates an uptrend
    assert set(res) == {"adx", "adx_prev", "plus_di", "minus_di", "rising"}


def test_adx_none_on_short_history():
    assert adx_components(_candles([100, 101, 102]), 14) is None
    assert calculate_adx(_candles([100, 101, 102]), 14) is None
    assert adx_components(None, 14) is None


# ── Same-direction / correlation counts ──────────────────────────────────────
def test_open_directional_counts(monkeypatch):
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {"ok": True, "positions": [
        {"symbol": "A/USDT:USDT", "side": "LONG", "contracts": 1.0},
        {"symbol": "B/USDT:USDT", "side": "LONG", "contracts": 1.0},
        {"symbol": "C/USDT:USDT", "side": "SHORT", "contracts": 1.0},
    ]})
    assert E.open_directional_counts() == {"LONG": 2, "SHORT": 1}


def test_open_directional_counts_zero_when_snapshot_failed(monkeypatch):
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {"ok": False, "positions": []})
    assert E.open_directional_counts() == {"LONG": 0, "SHORT": 0}


# ── Funding-rate helper (fails OPEN) ─────────────────────────────────────────
def test_funding_rate_reads_value(monkeypatch):
    class _Ex:
        def fetch_funding_rate(self, symbol):
            return {"fundingRate": 0.00042}
    monkeypatch.setattr(E, "_get_exchange", lambda: _Ex())
    assert E.funding_rate("BTC/USDT:USDT") == 0.00042


def test_funding_rate_none_on_error(monkeypatch):
    class _Ex:
        def fetch_funding_rate(self, symbol):
            raise RuntimeError("network")
    monkeypatch.setattr(E, "_get_exchange", lambda: _Ex())
    assert E.funding_rate("BTC/USDT:USDT") is None   # fails open → filter won't block


# ── Risk-based position sizing ───────────────────────────────────────────────
def _snap(equity=1000.0, available=10000.0):
    return {"ok": True, "balance": {"margin_balance": equity, "available": available}}


def test_risk_sizing_off_returns_none(monkeypatch):
    monkeypatch.setattr(E, "ENABLE_RISK_SIZING", False)
    assert E.risk_based_margin(100, 98) is None


def test_risk_sizing_math(monkeypatch):
    # equity 1000 * 2% = 20 risk$. stop_frac = |100-98|/100 = 0.02.
    # notional = 20/0.02 = 1000; margin = 1000 / leverage(4) = 250.
    monkeypatch.setattr(E, "ENABLE_RISK_SIZING", True)
    monkeypatch.setattr(E, "RISK_PCT_PER_TRADE", 0.02)
    monkeypatch.setattr(E, "LEVERAGE", 4)
    monkeypatch.setattr(E, "MAX_MARGIN_USDT", 10000)
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: _snap())
    assert round(E.risk_based_margin(100, 98), 2) == 250.0


def test_risk_sizing_clamped_to_available(monkeypatch):
    monkeypatch.setattr(E, "ENABLE_RISK_SIZING", True)
    monkeypatch.setattr(E, "RISK_PCT_PER_TRADE", 0.02)
    monkeypatch.setattr(E, "LEVERAGE", 4)
    monkeypatch.setattr(E, "MAX_MARGIN_USDT", 10000)
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: _snap(available=50.0))
    assert E.risk_based_margin(100, 98) == 50.0      # never exceeds free balance


def test_risk_sizing_clamped_to_max_margin(monkeypatch):
    monkeypatch.setattr(E, "ENABLE_RISK_SIZING", True)
    monkeypatch.setattr(E, "RISK_PCT_PER_TRADE", 0.02)
    monkeypatch.setattr(E, "LEVERAGE", 4)
    monkeypatch.setattr(E, "MAX_MARGIN_USDT", 100)
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: _snap())
    assert E.risk_based_margin(100, 98) == 100       # capped at MAX_MARGIN_USDT


def test_risk_sizing_none_when_snapshot_failed(monkeypatch):
    monkeypatch.setattr(E, "ENABLE_RISK_SIZING", True)
    monkeypatch.setattr(E, "account_snapshot", lambda *a, **k: {"ok": False})
    assert E.risk_based_margin(100, 98) is None       # → falls back to fixed margin
