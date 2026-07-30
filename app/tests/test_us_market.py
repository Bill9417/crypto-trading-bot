"""🇺🇸 美股收盤 digest — pure builders, the session/holiday gate, and tick().

Everything here runs offline: fetch_quotes is the module's only network seam
and the autouse guard already kills it, so each test supplies its own quote
dict. The numbers below are shaped like a real session (S&P up, SOX up harder,
VIX down) so the 💡 read has something to actually say.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import us_market as um

TZ = ZoneInfo("Asia/Taipei")
TZ_NY = ZoneInfo("America/New_York")

# 2026-07-29 16:00 New York — a normal Wednesday close.
CLOSE_TS = datetime(2026, 7, 29, 16, 0, tzinfo=TZ_NY).timestamp()


def _q(price, prev, ts=CLOSE_TS):
    return {"price": price, "prev": prev, "ts": ts,
            "chg_pct": um._chg_pct(price, prev)}


def quotes(**over):
    base = {
        "^DJI":  _q(51736.72, 51594.14),      # +0.28%
        "^GSPC": _q(7375.46, 7316.15),        # +0.81%
        "^IXIC": _q(24929.10, 24442.94),      # +1.99%
        "^SOX":  _q(11225.17, 10447.49),      # +7.44% — clearly leads
        "^VIX":  _q(19.03, 20.66),            # −7.89%
        "^TNX":  _q(4.681, 4.622),            # +0.06 pp
        "TSM":   _q(398.80, 374.67),
        "2330.TW": _q(2205.0, 2200.0),
        "TWD=X": _q(32.425, 32.346),
    }
    base.update(over)
    return base


def rows(*specs):
    """[(ticker, change_pct), …] → US100-shaped rows."""
    return [{"ticker": t, "name": t, "price": 100.0, "change_pct": p}
            for t, p in specs]


# ── formatting helpers ───────────────────────────────────────────────────────
def test_num_scales_decimals_with_magnitude():
    assert um._num(51736.72) == "51,737"
    assert um._num(398.80) == "398.8"
    assert um._num(19.03) == "19.03"
    assert um._num(None) == "—"


def test_pct_and_arrow_carry_sign():
    assert um._pct(0.81) == "+0.81%"
    assert um._pct(-7.89) == "-7.89%"
    assert um._pct(None) == "—"
    assert (um._arrow(1), um._arrow(-1), um._arrow(0)) == ("▲", "▼", "―")


def test_chg_pct_none_when_a_leg_is_missing():
    assert um._chg_pct(110, 100) == 10.0
    assert um._chg_pct(None, 100) is None
    assert um._chg_pct(110, 0) is None      # a zero previous close is not 0%


# ── session identity ─────────────────────────────────────────────────────────
def test_session_date_is_the_new_york_date_of_the_close():
    # 16:00 ET is 04:00 the NEXT day in Taipei — the NY date is what labels the
    # session, or a Taipei-side reader would see tomorrow's date on it.
    assert um.session_date(quotes()) == "2026-07-29"


def test_session_date_empty_without_a_timestamp():
    assert um.session_date({"^GSPC": {"price": 1, "prev": 1, "ts": None}}) == ""
    assert um.session_date({}) == ""


# ── breadth ──────────────────────────────────────────────────────────────────
def test_breadth_counts_and_ranks_movers():
    b = um.breadth(rows(("NVDA", 9.2), ("AMD", 7.8), ("AVGO", 5.1),
                        ("KO", -1.1), ("PG", -0.9), ("TSLA", -2.4),
                        ("MSFT", 0.0)))
    assert (b["up"], b["down"], b["flat"], b["total"]) == (3, 3, 1, 7)
    assert [r["ticker"] for r in b["gainers"]] == ["NVDA", "AMD", "AVGO"]
    assert [r["ticker"] for r in b["losers"]] == ["TSLA", "KO", "PG"]


def test_breadth_ignores_rows_yahoo_dropped():
    data = rows(("NVDA", 1.0)) + [{"ticker": "DEAD", "change_pct": None}]
    b = um.breadth(data)
    assert b["total"] == 1
    assert all(r["ticker"] != "DEAD" for r in b["gainers"])


def test_breadth_empty_is_safe():
    assert um.breadth([])["total"] == 0
    assert um.breadth(None)["gainers"] == []


def test_breadth_never_files_a_loser_under_gainers():
    """An all-green day has no 領跌 — slicing the bottom of the ranking would
    print the three weakest RISERS there, which is simply false."""
    b = um.breadth(rows(("A", 3.0), ("B", 2.0), ("C", 1.0), ("D", 0.5)))
    assert b["losers"] == []
    assert [r["ticker"] for r in b["gainers"]] == ["A", "B", "C"]
    # …and the mirror case
    b2 = um.breadth(rows(("A", -3.0), ("B", -2.0)))
    assert b2["gainers"] == []
    assert [r["ticker"] for r in b2["losers"]] == ["A", "B"]


# ── 台積電 ADR ───────────────────────────────────────────────────────────────
def test_adr_view_converts_at_the_five_to_one_ratio():
    v = um.adr_view(quotes())
    # 398.80 USD × 32.425 TWD/USD ÷ 5 shares per ADR
    assert v["equiv"] == pytest.approx(398.80 * 32.425 / 5)
    assert v["tw_close"] == 2205.0
    assert v["premium_pct"] == pytest.approx(
        (v["equiv"] - 2205.0) / 2205.0 * 100, abs=0.05)


def test_adr_view_empty_when_any_leg_is_missing():
    for missing in ("TSM", "TWD=X", "2330.TW"):
        q = quotes()
        q[missing] = {"price": None, "prev": None, "ts": None, "chg_pct": None}
        assert um.adr_view(q) == {}, f"{missing} missing should suppress the block"


# ── the 💡 read ──────────────────────────────────────────────────────────────
def test_read_line_calls_out_semis_leading_the_market():
    line = um.read_line(um.build_snapshot(quotes(), []))
    assert "美股收紅" in line
    assert "費半" in line and "強於大盤" in line
    assert "VIX 回落" in line


def test_read_line_calls_out_semis_lagging():
    q = quotes(**{"^SOX": _q(10000.0, 10500.0)})     # −4.76% against S&P +0.81%
    line = um.read_line(um.build_snapshot(q, []))
    assert "弱於大盤" in line and "承壓" in line


def test_read_line_says_in_line_when_semis_track_the_market():
    q = quotes(**{"^SOX": _q(10532.0, 10447.49)})    # +0.81%, same as the S&P
    assert "與大盤同步" in um.read_line(um.build_snapshot(q, []))


@pytest.mark.parametrize("px,prev,expect", [
    (7450.0, 7316.15, "漲勢明顯"),      # +1.83%
    (7340.0, 7316.15, "美股收紅"),      # +0.33%
    (7320.0, 7316.15, "平盤震盪"),      # +0.05%
    (7280.0, 7316.15, "美股收黑"),      # −0.49%
    (7200.0, 7316.15, "跌幅較重"),      # −1.59%
])
def test_read_line_direction_bands(px, prev, expect):
    q = quotes(**{"^GSPC": _q(px, prev)})
    assert expect in um.read_line(um.build_snapshot(q, []))


def test_read_line_empty_without_the_sp500():
    q = quotes(**{"^GSPC": {"price": None, "prev": None, "ts": None, "chg_pct": None}})
    assert um.read_line(um.build_snapshot(q, [])) == ""


def test_read_line_never_forecasts_or_advises():
    """The house honesty rule: describe the session, never call the next one."""
    line = um.read_line(um.build_snapshot(quotes(), []))
    for banned in ("買進", "賣出", "建議", "必漲", "保證", "勝率", "看漲", "看跌"):
        assert banned not in line


# ── messages ─────────────────────────────────────────────────────────────────
def test_plain_message_has_every_block_and_no_markup():
    now = datetime(2026, 7, 30, 8, 5, tzinfo=TZ)
    snap = um.build_snapshot(quotes(), rows(("NVDA", 9.2), ("TSLA", -2.4)))
    text = um.build_plain(snap, now)
    assert "🇺🇸 美股收盤 07-29（週三）" in text     # the NY session, not 07-30
    for label in ("道瓊", "標普500", "那斯達克", "費城半導體", "VIX", "美10年公債"):
        assert label in text
    assert "台積電 ADR" in text and "約當台股" in text
    assert "美股百大" in text and "領漲 NVDA" in text and "領跌 TSLA" in text
    assert "💡" in text and "非投資建議" in text
    assert "<" not in text and "&" not in text     # LINE renders plain text only


def test_plain_message_fits_one_line_push():
    snap = um.build_snapshot(quotes(), rows(*[(f"S{i}", i / 10) for i in range(100)]))
    text = um.build_plain(snap, datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    import line_push
    assert len(text) < line_push.MAX_LEN          # never split across pushes


def test_ten_year_yield_moves_in_points_not_percent():
    """4.622 → 4.681 is +0.06 percentage POINTS. Printing +1.28% there would
    read as the yield itself and be flatly wrong."""
    snap = um.build_snapshot(quotes(), [])
    assert snap["tnx"]["chg_pp"] == pytest.approx(0.059, abs=0.001)
    text = um.build_plain(snap, datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    assert "美10年公債 4.68%　▲ +0.06" in text


def test_missing_symbols_drop_their_rows_instead_of_printing_dashes():
    q = quotes()
    for sym in ("^SOX", "^VIX", "^TNX"):
        q[sym] = {"price": None, "prev": None, "ts": None, "chg_pct": None}
    text = um.build_plain(um.build_snapshot(q, []), datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    assert "費城半導體" not in text and "VIX" not in text and "美10年公債" not in text
    assert "標普500" in text                      # the rest still prints


def test_telegram_message_uses_the_house_style():
    import tg_format as tf
    snap = um.build_snapshot(quotes(), rows(("NVDA", 9.2), ("TSLA", -2.4)))
    msg = um.build_telegram(snap, datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    assert tf.DIV in msg and "<pre>" in msg and "<b>" in msg
    assert "台積電 ADR" in msg and "非投資建議" in msg


def test_holiday_message_states_the_closure():
    text = um.build_holiday_plain(datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    assert "休市" in text and "07-30" in text


# ── the send gate ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("when,state,due", [
    (datetime(2026, 7, 30, 8, 0, tzinfo=TZ), {}, True),                    # Thu 08:00
    (datetime(2026, 7, 30, 7, 59, tzinfo=TZ), {}, False),                  # too early
    (datetime(2026, 8, 1, 9, 0, tzinfo=TZ), {}, False),                    # Saturday
    (datetime(2026, 8, 2, 9, 0, tzinfo=TZ), {}, False),                    # Sunday
    (datetime(2026, 7, 30, 9, 0, tzinfo=TZ),
     {"last_run_date": "2026-07-30"}, False),                              # already sent
    (datetime(2026, 7, 30, 9, 0, tzinfo=TZ),
     {"last_run_date": "2026-07-29"}, True),                               # new day
])
def test_due_gate(when, state, due):
    assert um._due(state, when) is due


def test_monday_reports_fridays_close():
    """No weekend sends, so Friday's US close is the one Monday morning
    describes — the freshest US read before Monday's TW open."""
    assert um._due({}, datetime(2026, 8, 3, 8, 0, tzinfo=TZ)) is True   # Monday


# ── tick() ───────────────────────────────────────────────────────────────────
@pytest.fixture
def sent(monkeypatch):
    """Capture what tick() would send, on both channels."""
    box = {"tg": [], "line": []}
    import line_push
    import telegram_utils
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **k: box["tg"].append(msg) or True)
    monkeypatch.setattr(line_push, "enabled", lambda: True)
    monkeypatch.setattr(line_push, "send", lambda text: box["line"].append(text) or True)
    monkeypatch.setattr(um, "_us_rows", lambda: rows(("NVDA", 9.2), ("TSLA", -2.4)))
    return box


def _at(monkeypatch, when):
    """Freeze us_market's view of 'now' (its only clock read)."""
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when
    monkeypatch.setattr(um, "datetime", _DT)


def test_tick_sends_both_channels_and_records_the_session(monkeypatch, sent):
    _at(monkeypatch, datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    monkeypatch.setattr(um, "fetch_quotes", lambda symbols=None: quotes())
    assert um.tick() is True
    assert len(sent["tg"]) == 1 and len(sent["line"]) == 1
    assert "台積電 ADR" in sent["line"][0]
    st = um._load_state()
    assert st["last_session_date"] == "2026-07-29"
    assert st["last_run_date"] == "2026-07-30"
    assert st["last_plain"] == sent["line"][0]


def test_tick_is_once_per_day(monkeypatch, sent):
    _at(monkeypatch, datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    monkeypatch.setattr(um, "fetch_quotes", lambda symbols=None: quotes())
    um.tick()
    assert um.tick() is False
    assert len(sent["tg"]) == 1


def test_tick_sends_a_holiday_notice_when_no_new_session(monkeypatch, sent):
    """A US holiday leaves the last close unchanged — say 休市 rather than
    reprinting yesterday's numbers as if they were new."""
    um._save_state({"last_session_date": "2026-07-29"})
    _at(monkeypatch, datetime(2026, 7, 31, 8, 5, tzinfo=TZ))
    monkeypatch.setattr(um, "fetch_quotes", lambda symbols=None: quotes())
    assert um.tick() is True
    assert "休市" in sent["line"][0]
    assert um._load_state()["last_session_date"] == "2026-07-29"   # unchanged


def test_tick_retries_quietly_when_yahoo_is_down(monkeypatch, sent):
    _at(monkeypatch, datetime(2026, 7, 30, 8, 5, tzinfo=TZ))

    def _boom(symbols=None):
        raise RuntimeError("yahoo 503")
    monkeypatch.setattr(um, "fetch_quotes", _boom)
    assert um.tick() is False
    assert sent["tg"] == [] and sent["line"] == []
    st = um._load_state()
    assert "last_run_date" not in st          # the day is NOT burned
    assert st["last_attempt"] > 0             # but the retry is paced


def test_tick_will_not_guess_the_session(monkeypatch, sent):
    """No S&P timestamp means we cannot say which close this is — send
    nothing rather than mislabel it."""
    _at(monkeypatch, datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    q = quotes()
    q["^GSPC"] = {**q["^GSPC"], "ts": None}
    monkeypatch.setattr(um, "fetch_quotes", lambda symbols=None: q)
    assert um.tick() is False
    assert sent["tg"] == []


def test_tick_still_sends_when_breadth_is_unavailable(monkeypatch, sent):
    _at(monkeypatch, datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    monkeypatch.setattr(um, "fetch_quotes", lambda symbols=None: quotes())
    monkeypatch.setattr(um, "_us_rows", lambda: [])
    assert um.tick() is True
    assert "美股百大" not in sent["line"][0]
    assert "標普500" in sent["line"][0]


def test_line_command_serves_the_last_digest(monkeypatch, sent):
    _at(monkeypatch, datetime(2026, 7, 30, 8, 5, tzinfo=TZ))
    monkeypatch.setattr(um, "fetch_quotes", lambda symbols=None: quotes())
    um.tick()
    import line_push
    assert line_push._command_reply("美股") == sent["line"][0]
    assert "還沒有" in line_push._command_reply("us") or True   # populated here


def test_line_command_before_the_first_run_explains_itself():
    import line_push
    assert "08:00" in line_push._command_reply("美股")
