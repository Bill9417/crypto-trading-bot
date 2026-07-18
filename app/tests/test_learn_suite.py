"""signal outcomes + ETH-MOM forward test + engine verdicts + balance history."""
from datetime import datetime
from zoneinfo import ZoneInfo

import daily_report
import eth_mom
import signal_outcomes as SO

TS = 1_783_900_000.0


def _sig(direction="long", entry=100.0, sl=95.0, tp1=105.0, tp2=110.0):
    return {"symbol": "ETH/USDT:USDT", "base": "ETH", "direction": direction,
            "ts": TS, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "score": 90}


def _candle(hours_after, o=100.0, h=100.0, l=100.0, c=100.0):  # noqa: E741
    return (int((TS + hours_after * 3600) * 1000), o, h, l, c, 1.0)


# ── outcome evaluation ───────────────────────────────────────────────────────
def test_outcome_tp2_before_stop():
    candles = [_candle(1, h=106.0), _candle(2, h=111.0)]
    res = SO.evaluate(_sig(), candles)
    assert res["outcome"] == "tp2" and res["hours"] == 2.0


def test_outcome_stop_first_is_pessimistic():
    # same candle touches BOTH tp2 and sl → the stop wins the tie
    candles = [_candle(1, h=111.0, l=94.0)]
    assert SO.evaluate(_sig(), candles)["outcome"] == "sl"


def test_outcome_partial_winner_then_stop():
    candles = [_candle(1, h=106.0), _candle(3, l=94.0)]
    assert SO.evaluate(_sig(), candles)["outcome"] == "tp1→sl"


def test_outcome_short_direction_mirrors():
    sig = _sig("short", entry=100.0, sl=105.0, tp1=95.0, tp2=90.0)
    candles = [_candle(1, l=89.0)]
    assert SO.evaluate(sig, candles)["outcome"] == "tp2"


def test_outcome_nothing_hit_in_window():
    candles = [_candle(1), _candle(47), _candle(60, h=120.0)]  # 60h > window
    assert SO.evaluate(_sig(), candles)["outcome"] == "none"


def test_outcome_candles_before_signal_ignored():
    candles = [_candle(-1, l=90.0), _candle(1, h=111.0)]
    assert SO.evaluate(_sig(), candles)["outcome"] == "tp2"


def test_summarize_counts_and_small_sample_honesty():
    outs = [{"outcome": "tp2"}, {"outcome": "sl"}, {"outcome": "tp1→sl"}]
    txt = SO.summarize(outs)
    assert "🎯 到 TP2" in txt and "33%" in txt
    assert "⚠️ 停損" in txt and "67%" in txt
    assert "樣本只有 3 個" in txt          # small-n warning is mandatory


# ── ETH momentum rule ────────────────────────────────────────────────────────
def test_mom_direction_long_short_insufficient():
    up = [100.0 + i * 0.1 for i in range(200)]
    dn = [200.0 - i * 0.1 for i in range(200)]
    assert eth_mom.direction(up) == "long"
    assert eth_mom.direction(dn) == "short"
    assert eth_mom.direction(up[:100]) is None          # < N+1 bars


def test_mom_closed_bars_drops_forming_candle():
    now = TS
    bars = [(int((now - 7200 * 2) * 1000),) + (1, 1, 1, 1, 1),
            (int((now - 3600) * 1000),) + (1, 1, 1, 1, 1)]   # opened 1h ago → forming
    assert len(eth_mom.closed_bars(bars, now)) == 1
    assert len(eth_mom.closed_bars(bars, now + 7200)) == 2


def test_mom_leg_pnl_signs():
    assert abs(eth_mom.leg_pnl_pct("long", 100.0, 110.0) - 10.0) < 1e-9
    assert abs(eth_mom.leg_pnl_pct("short", 100.0, 110.0) + 10.0) < 1e-9


# ── engine verdicts + balance history ────────────────────────────────────────
def test_engine_verdict_flags_losing_engine():
    s = {"ok": True, "n_trades": 40, "profit_factor": 0.34,
         "daily": [{"net": -5.0}] * 7}
    lines = daily_report._engine_verdict("Bybit S3", s)
    assert "沒有支付它的風險" in lines[0]
    good = {"ok": True, "n_trades": 40, "profit_factor": 1.5, "daily": []}
    assert "✅" in daily_report._engine_verdict("X", good)[0]
    assert daily_report._engine_verdict("Y", None) == ["  Y: 無成交紀錄"]


def test_monday_report_includes_verdicts():
    data = {"binance_pnl": {"ok": True, "n_trades": 12, "profit_factor": 0.8,
                            "daily": []},
            "bybit_pnl": None}
    monday = datetime(2026, 7, 13, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    txt = daily_report.build_report(data, monday)
    assert "🧪 引擎週檢" in txt
    tuesday = datetime(2026, 7, 14, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    assert "🧪" not in daily_report.build_report(data, tuesday)


def test_record_balance_appends_and_replaces(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_report, "BALANCE_FILE", str(tmp_path / "bal.json"))
    now = datetime(2026, 7, 13, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    data = {"binance_snap": {"balance": {"total": 21.5}},
            "bybit_snap": {"balance": {"total": 231.0}}}
    assert daily_report.record_balance(data, now) is True
    data["bybit_snap"]["balance"]["total"] = 240.0      # same day → replace
    assert daily_report.record_balance(data, now) is True
    import json
    with open(tmp_path / "bal.json") as f:
        hist = json.load(f)
    assert len(hist) == 1
    assert hist[0] == {"date": "2026-07-13", "binance": 21.5,
                       "bybit": 240.0, "total": 261.5}


def test_record_balance_skips_when_both_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_report, "BALANCE_FILE", str(tmp_path / "bal.json"))
    now = datetime(2026, 7, 13, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    assert daily_report.record_balance({"binance_snap": None,
                                        "bybit_snap": None}, now) is False
