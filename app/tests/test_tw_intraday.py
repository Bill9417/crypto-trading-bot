"""tw_intraday — session gate, mover detection, SL/TP level hits, formatting."""
from datetime import datetime

import tw_intraday


def _t(hour, minute, day=13):
    # 2026-07-13 is a Monday
    return datetime(2026, 7, day, hour, minute, tzinfo=tw_intraday.TZ)


# ── session gate ─────────────────────────────────────────────────────────────
def test_in_session_trading_hours():
    assert tw_intraday.in_session(_t(9, 0))
    assert tw_intraday.in_session(_t(12, 30))
    assert tw_intraday.in_session(_t(13, 34))


def test_not_in_session_outside_hours():
    assert not tw_intraday.in_session(_t(8, 59))
    assert not tw_intraday.in_session(_t(13, 35))
    assert not tw_intraday.in_session(_t(9, 30, day=12))     # Sunday


# ── movers ───────────────────────────────────────────────────────────────────
def _row(code, pct, price=100.0, lots=10_000, name="測試"):
    return {"code": code, "name": name, "price": price, "change_pct": pct,
            "volume_lots": lots}


def test_detect_movers_threshold_and_order():
    rows = [_row("2330", 4.2, 1180.0), _row("2317", -5.0, 200.0),
            _row("2412", 1.0)]
    events, hits = tw_intraday.detect_movers(rows, {}, "2026-07-13", threshold=3.5)
    assert set(hits) == {"2330", "2317"}
    assert len(events) == 2
    assert "2317" in events[0]                    # biggest |move| first
    assert "📉" in events[0] and "🚀" in events[1]
    assert "-5.0%" in events[0] and "成交" in events[0]


def test_detect_movers_once_per_day():
    rows = [_row("2330", 4.2)]
    events, hits = tw_intraday.detect_movers(rows, {"2330": "2026-07-13"},
                                             "2026-07-13", threshold=3.5)
    assert events == [] and hits == {}
    # a new day re-arms it
    events, hits = tw_intraday.detect_movers(rows, {"2330": "2026-07-10"},
                                             "2026-07-13", threshold=3.5)
    assert len(events) == 1


# ── setup level hits ─────────────────────────────────────────────────────────
SETUP = {"code": "2330", "name": "台積電", "date": "2026-07-11",
         "ref": 1150.0, "sl": 1080.0, "tp": 1260.0}


def test_level_hit_stop_loss():
    events, hits = tw_intraday.check_levels(
        {"2330": _row("2330", -6.0, price=1075.0)}, [SETUP], {}, "2026-07-13")
    assert len(events) == 1
    assert "⚠️" in events[0] and "跌破停損" in events[0] and "1,080" in events[0]
    assert "2330:2026-07-11:sl" in hits


def test_level_hit_take_profit():
    events, hits = tw_intraday.check_levels(
        {"2330": _row("2330", 5.0, price=1265.0)}, [SETUP], {}, "2026-07-13")
    assert "🎯" in events[0] and "到達目標" in events[0]
    assert "2330:2026-07-11:tp" in hits


def test_level_between_levels_is_silent():
    events, hits = tw_intraday.check_levels(
        {"2330": _row("2330", 0.5, price=1150.0)}, [SETUP], {}, "2026-07-13")
    assert events == [] and hits == {}


def test_level_hit_fires_once_ever():
    prior = {"2330:2026-07-11:sl": "2026-07-12"}
    events, hits = tw_intraday.check_levels(
        {"2330": _row("2330", -6.0, price=1075.0)}, [SETUP], prior, "2026-07-13")
    assert events == [] and hits == {}


def test_setup_dated_today_not_live_yet():
    fresh = dict(SETUP, date="2026-07-13")
    events, hits = tw_intraday.check_levels(
        {"2330": _row("2330", -6.0, price=1075.0)}, [fresh], {}, "2026-07-13")
    assert events == [] and hits == {}


# ── formatting ───────────────────────────────────────────────────────────────
def test_opening_text_with_setups():
    taiex = {"price": 45120.0, "prev": 44950.0, "pct": 0.38}
    msg = tw_intraday.opening_text(_t(9, 1), taiex, [SETUP])
    assert "🔔 台股開盤" in msg and "(週一)" in msg
    assert "TAIEX 45,120 (+0.38%" in msg
    assert "2330" in msg
    assert "進 1,150" in msg and "損 1,080" in msg and "標 1,260" in msg
    assert "<pre>" in msg                       # aligned setups table


def test_opening_text_no_setups():
    msg = tw_intraday.opening_text(_t(9, 1), None, [])
    assert "目前無追蹤中的設定" in msg
