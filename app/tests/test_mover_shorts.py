"""🚀 Pump Radar — only a PUMP is alert-grade.

Momentum shorts (short the coin that just dumped) are the one signal this
repo has measured as confidently negative: −0.211R per trade with the
interval clear of zero (top_picks.MOVER_SHORT_NOTE). Until 2026-09-27 a dump
crossing both thresholds fired the same Telegram alert as a pump, with a 📉
in place of the 🚀 — which reads as "short this". These pin the retirement:
dumps stay on the dashboard strip as information and never become an alert.
"""
import strategy2_scanner as S


def test_a_pump_with_volume_is_alert_grade():
    mv = {"chg_1h": S.MOVER_1H_PCT + 1, "vol_mult": S.MOVER_VOL_MULT + 1}
    assert S._mover_hot(mv) is True


def test_a_dump_is_never_alert_grade_however_violent():
    mv = {"chg_1h": -(S.MOVER_1H_PCT * 5), "vol_mult": S.MOVER_VOL_MULT * 5}
    assert S._mover_hot(mv) is False


def test_thresholds_still_apply_to_pumps():
    assert S._mover_hot({"chg_1h": S.MOVER_1H_PCT - 0.1, "vol_mult": 99}) is False
    assert S._mover_hot({"chg_1h": 99, "vol_mult": S.MOVER_VOL_MULT - 0.1}) is False


def test_dumps_still_reach_the_dashboard_strip():
    """Information, not a signal: the strip ranks by |move| so a dump is
    visible; it just never carries the hot flag."""
    S.LATEST_MOVERS.clear()
    import time
    now = time.time()
    S.LATEST_MOVERS["A/USDT:USDT"] = {"symbol": "A/USDT:USDT", "base": "A", "hot": False,
                                      "ts": now, "chg_1h": -8.0, "vol_mult": 5.0,
                                      "chg_24h": -3.0, "price": 1.0, "tv_url": ""}
    S.LATEST_MOVERS["B/USDT:USDT"] = {"symbol": "B/USDT:USDT", "base": "B", "hot": True,
                                      "ts": now, "chg_1h": 5.0, "vol_mult": 4.0,
                                      "chg_24h": 1.0, "price": 1.0, "tv_url": ""}
    rows = S._top_movers()
    assert [r["base"] for r in rows] == ["A", "B"]      # biggest |move| first
    assert not rows[0]["hot"] and rows[1]["hot"]
    S.LATEST_MOVERS.clear()


def test_the_alert_text_has_no_dump_branch_left():
    import inspect
    src = inspect.getsource(S.scan_once)
    assert "急殺" not in src and "📉" not in src
