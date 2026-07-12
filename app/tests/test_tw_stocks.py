"""tw_stocks — pure signal/regime/digest/scheduling logic."""
from datetime import datetime

import tw_stocks


def _rows_from_closes(closes):
    """Flat bars (o=h=l=c) — enough for regime(), which only reads closes."""
    return [(86400 * i, c, c, c, c, 1000) for i, c in enumerate(closes)]


def _uptrend_with_pullback(n=80):
    """Rising closes 100,101,… whose LAST bar dips to the 20-day SMA and
    closes back above its open — the exact setup pattern."""
    rows = []
    for i in range(n - 1):
        c = 100.0 + i
        rows.append((86400 * i, c - 0.5, c + 1.0, c - 1.0, c, 1000))
    c = 100.0 + (n - 1)                      # 179
    rows.append((86400 * (n - 1), c - 0.5, c + 1.0, c - 10.0, c, 1000))
    return rows


# ── regime ───────────────────────────────────────────────────────────────────
def test_regime_bull():
    reg = tw_stocks.regime(_rows_from_closes([100.0 + i for i in range(130)]))
    assert reg["ok"] is True
    assert reg["close"] > reg["sma100"]
    assert reg["mom20"] > 0


def test_regime_bear_below_sma100():
    reg = tw_stocks.regime(_rows_from_closes([230.0 - i for i in range(130)]))
    assert reg["ok"] is False


def test_regime_needs_positive_momentum_too():
    # above the 100-day SMA but drifting DOWN for the last 21 sessions
    closes = [100.0 + i for i in range(109)] + [208.0 - 0.5 * i for i in range(1, 22)]
    reg = tw_stocks.regime(_rows_from_closes(closes))
    assert reg["close"] > reg["sma100"]
    assert reg["mom20"] < 0
    assert reg["ok"] is False


def test_regime_insufficient_data():
    assert tw_stocks.regime(_rows_from_closes([100.0] * 50))["ok"] is False


# ── setup ────────────────────────────────────────────────────────────────────
def test_setup_fires_on_pullback_and_levels_match():
    rows = _uptrend_with_pullback()
    s = tw_stocks.setup(rows)
    assert s is not None
    ref, atr = s["ref"], s["atr"]
    assert ref == rows[-1][4]
    assert abs(s["sl"] - (ref - tw_stocks.SL_ATR * atr)) < 1e-9
    assert abs(s["tp"] - (ref + tw_stocks.TP_ATR * atr)) < 1e-9
    assert s["sl"] < ref < s["tp"]


def test_setup_rejects_downtrend():
    rows = []
    for i in range(80):
        c = 200.0 - i
        rows.append((86400 * i, c + 0.5, c + 1.0, c - 10.0, c, 1000))
    assert tw_stocks.setup(rows) is None


def test_setup_rejects_no_pullback():
    # uptrend but the last low never reaches the 20-day SMA
    rows = _uptrend_with_pullback()
    t, o, h, l, c, v = rows[-1]
    rows[-1] = (t, o, h, c - 1.0, c, v)
    assert tw_stocks.setup(rows) is None


def test_setup_rejects_red_close():
    # tags the SMA20 but closes below the open (sellers won the bar)
    rows = _uptrend_with_pullback()
    t, o, h, l, c, v = rows[-1]
    rows[-1] = (t, c + 0.5, h, l, c, v)
    assert tw_stocks.setup(rows) is None


def test_setup_insufficient_data():
    assert tw_stocks.setup(_uptrend_with_pullback(50)) is None


# ── digest ───────────────────────────────────────────────────────────────────
def _now():
    return datetime(2026, 7, 10, 14, 5, tzinfo=tw_stocks.TZ)   # a Friday


def test_digest_bull_with_setup():
    reg = {"ok": True, "close": 28000.0, "sma100": 27000.0, "mom20": 0.03}
    s = {"ref": 2415.0, "sl": 2255.0, "tp": 2680.0, "atr": 53.0, "turnover": 1e9}
    msg = tw_stocks.build_digest(_now(), reg, [("2330", "台積電", s)])
    assert "✅" in msg and "2330" in msg and "台積電" in msg
    assert "進場參考 2,415" in msg
    assert "停損 2,255" in msg and "目標 2,680" in msg
    assert "勝率≠賺錢" in msg          # the honesty footer is not optional


def test_digest_bull_no_setups():
    reg = {"ok": True, "close": 28000.0, "sma100": 27000.0, "mom20": 0.03}
    msg = tw_stocks.build_digest(_now(), reg, [])
    assert "今日無符合條件" in msg


def test_digest_bear_blocks_setups():
    reg = {"ok": False, "close": 26000.0, "sma100": 27000.0, "mom20": -0.02}
    msg = tw_stocks.build_digest(_now(), reg, [], pattern_blocked=3)
    assert "⛔" in msg and "觀望" in msg
    assert "3 檔符合型態" in msg
    assert "進場參考" not in msg


def test_digest_caps_listed_setups():
    reg = {"ok": True, "close": 28000.0, "sma100": 27000.0, "mom20": 0.03}
    s = {"ref": 100.0, "sl": 94.0, "tp": 110.0, "atr": 2.0, "turnover": 1.0}
    many = [(f"{1000 + i}", f"股票{i}", dict(s)) for i in range(14)]
    msg = tw_stocks.build_digest(_now(), reg, many)
    assert f"…另有 {14 - tw_stocks.MAX_SHOW} 檔未列出" in msg


# ── scheduling ───────────────────────────────────────────────────────────────
def test_due_after_close_on_weekday():
    assert tw_stocks._due({}, datetime(2026, 7, 10, 14, 1, tzinfo=tw_stocks.TZ))


def test_not_due_before_send_hour():
    assert not tw_stocks._due({}, datetime(2026, 7, 10, 13, 59, tzinfo=tw_stocks.TZ))


def test_not_due_on_weekend():
    assert not tw_stocks._due({}, datetime(2026, 7, 11, 15, 0, tzinfo=tw_stocks.TZ))


def test_not_due_twice_same_day():
    state = {"last_run_date": "2026-07-10"}
    assert not tw_stocks._due(state, datetime(2026, 7, 10, 15, 0, tzinfo=tw_stocks.TZ))


def test_tw_command_reads_state(monkeypatch):
    import tg_commands
    monkeypatch.setattr(tw_stocks, "_load_state",
                        lambda: {"last_digest_text": "🇹🇼 台股掃描 · test"})
    assert tg_commands.handle("tw") == "🇹🇼 台股掃描 · test"


def test_tw_command_no_state_yet(monkeypatch):
    import tg_commands
    monkeypatch.setattr(tw_stocks, "_load_state", lambda: {})
    assert "尚未有台股掃描" in tg_commands.handle("tw")


def test_clean_command_parses_hours(monkeypatch):
    import telegram_utils
    import tg_commands
    called = {}

    def fake_clean(hours):
        called["hours"] = hours
        return {"deleted": 2, "too_old": 1, "kept": 3, "failed": 0}

    monkeypatch.setattr(telegram_utils, "clean_old_messages", fake_clean)
    out = tg_commands.handle("clean", "6")
    assert called["hours"] == 6.0
    assert "已刪除 2 則" in out and "超過48h" in out and "3 則未到時限" in out

    tg_commands.handle("clean", "")          # default
    assert called["hours"] == 24.0
    tg_commands.handle("clean", "999")       # clamped under Telegram's 48h wall
    assert called["hours"] == 47.0
    tg_commands.handle("clean", "abc")       # junk → default
    assert called["hours"] == 24.0
