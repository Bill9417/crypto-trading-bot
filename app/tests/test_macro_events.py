"""
The daily report printed 「無 — 平靜的總經日」every single day, because its only
macro feed had been answering HTTP 429 and an empty fetch was rendered as
"nothing scheduled". These tests pin the distinction the report has to make:
NO EVENTS and CANNOT READ THE CALENDAR are different claims.
"""
from datetime import datetime, timedelta, timezone

import macro_events as ME

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 8, 10, 0, tzinfo=TZ)


def _ev(title, when, forecast=None, previous=None):
    return {"when": when, "title": title, "forecast": forecast,
            "previous": previous, "source": "tradingview"}


# ── the bug ──────────────────────────────────────────────────────────────────

def test_an_unreadable_calendar_is_never_reported_as_a_quiet_day(monkeypatch):
    import market_intel
    monkeypatch.setattr(market_intel, "upcoming_macro",
                        lambda *a, **k: {"events": [], "ok": False, "stale": False})
    out = "\n".join(ME.lines(NOW, TZ))
    assert "讀不到" in out
    # the wording must not ASSERT emptiness; "這不代表沒有事件" is the opposite
    assert "沒有高影響" not in out and "平靜" not in out


def test_a_genuinely_empty_week_says_so(monkeypatch):
    import market_intel
    monkeypatch.setattr(market_intel, "upcoming_macro",
                        lambda *a, **k: {"events": [], "ok": True, "stale": False})
    assert "沒有高影響美國數據" in "\n".join(ME.lines(NOW, TZ, days=7))


def test_stale_data_is_labelled_not_passed_off_as_current(monkeypatch):
    import market_intel
    monkeypatch.setattr(market_intel, "upcoming_macro", lambda *a, **k: {
        "events": [_ev("Inflation Rate YoY", NOW + timedelta(days=2), "3.4", "3.5")],
        "ok": True, "stale": True})
    out = "\n".join(ME.lines(NOW, TZ))
    assert "CPI" in out and "無法更新" in out


def test_a_broken_calendar_module_still_returns_a_line(monkeypatch):
    import market_intel

    def boom(*a, **k):
        raise RuntimeError("upstream on fire")
    monkeypatch.setattr(market_intel, "upcoming_macro", boom)
    assert "讀取失敗" in "\n".join(ME.lines(NOW, TZ))


# ── collapsing ───────────────────────────────────────────────────────────────

def test_one_cpi_release_is_one_line_with_the_headline_number(monkeypatch):
    """CPI arrives as four rows at the same minute. Four near-identical lines
    is less readable, not more — and the four disagree on the numbers, so the
    group must be labelled by whichever member it quotes."""
    when = NOW + timedelta(days=4)
    import market_intel
    monkeypatch.setattr(market_intel, "upcoming_macro", lambda *a, **k: {
        "events": [_ev("Core Inflation Rate MoM", when, "0.2", "0"),
                   _ev("Inflation Rate YoY", when, "3.4", "3.5"),
                   _ev("Core Inflation Rate YoY", when, "2.5", "2.6"),
                   _ev("Inflation Rate MoM", when, "0.1", "-0.4")],
        "ok": True, "stale": False})
    out = ME.lines(NOW, TZ, days=7)
    assert len(out) == 1
    assert "CPI 通膨年增" in out[0]
    assert "預估 3.4" in out[0] and "前值 3.5" in out[0]   # the YoY member's own pair


def test_a_group_without_its_headline_member_keeps_its_own_numbers(monkeypatch):
    when = NOW + timedelta(days=1)
    import market_intel
    monkeypatch.setattr(market_intel, "upcoming_macro", lambda *a, **k: {
        "events": [_ev("Core Inflation Rate MoM", when, "0.2", "0")],
        "ok": True, "stale": False})
    out = ME.lines(NOW, TZ, days=7)[0]
    assert "核心 CPI" in out and "預估 0.2" in out


def test_two_releases_at_the_same_minute_stay_separate(monkeypatch):
    when = NOW + timedelta(days=1)
    import market_intel
    monkeypatch.setattr(market_intel, "upcoming_macro", lambda *a, **k: {
        "events": [_ev("Inflation Rate YoY", when, "3.4"),
                   _ev("Retail Sales MoM", when, "0.2")],
        "ok": True, "stale": False})
    assert len(ME.lines(NOW, TZ, days=7)) == 2


# ── wording ──────────────────────────────────────────────────────────────────

def test_the_big_two_are_flagged_and_the_rest_are_not(monkeypatch):
    import market_intel
    monkeypatch.setattr(market_intel, "upcoming_macro", lambda *a, **k: {
        "events": [_ev("Inflation Rate YoY", NOW + timedelta(days=1)),
                   _ev("Existing Home Sales", NOW + timedelta(days=2))],
        "ok": True, "stale": False})
    out = ME.lines(NOW, TZ, days=7)
    assert out[0].strip().startswith("🔴")          # CPI
    assert not out[1].strip().startswith("🔴")      # home sales


def test_today_and_tomorrow_read_as_words_not_dates(monkeypatch):
    import market_intel
    monkeypatch.setattr(market_intel, "upcoming_macro", lambda *a, **k: {
        "events": [_ev("Non-Farm Payrolls", NOW + timedelta(hours=6)),
                   _ev("Retail Sales MoM", NOW + timedelta(days=1))],
        "ok": True, "stale": False})
    out = ME.lines(NOW, TZ, days=7)
    assert "今天" in out[0] and "明天" in out[1]


def test_unknown_titles_are_shown_verbatim_not_dropped():
    zh, family, weight = ME.classify("Wholesale Inventories MoM")
    assert zh == "Wholesale Inventories MoM" and family == ""


def test_classify_knows_the_market_movers():
    assert ME.classify("FOMC rate decision")[1] == "fomc"
    assert ME.classify("Non-Farm Payrolls")[1] == "nfp"
    assert ME.classify("Core PCE Price Index YoY")[1] == "pce"
