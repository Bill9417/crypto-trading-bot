"""Event Radar — pure-logic tests (no network, no state file)."""
import time
from email.utils import formatdate

import event_radar as ER


def _item(title, age_sec=60, source="CoinDesk"):
    return {"title": title, "link": "https://x.test/a", "source": source,
            "published": formatdate(time.time() - age_sec, usegmt=True)}


# ── categorize ───────────────────────────────────────────────────────────────
def test_categorize_fed():
    assert "FED" in ER.categorize("Fed holds interest rates steady as Powell warns on inflation")


def test_categorize_war():
    assert "GEOPOLITICS" in ER.categorize("Missiles strike port city as war escalation feared")


def test_categorize_hack():
    assert "HACK" in ER.categorize("DeFi protocol drained of $80M in exploit")


def test_categorize_whale():
    assert "WHALE" in ER.categorize("Dormant wallet from 2011 moves coins — whale watchers on alert")


def test_categorize_neutral_is_none():
    assert ER.categorize("Top 5 altcoins to watch this week") is None
    assert ER.categorize("Award-winning wallet app adds staking") is None  # 'war' must not match inside words


# ── news detector ────────────────────────────────────────────────────────────
def test_news_first_run_seeds_silently():
    state = {}
    out = ER._news_alerts([_item("Fed cuts interest rates by 50bps")], state, time.time())
    assert out == []                      # first ever run: learn, never alert
    assert state["seeded"] is True
    assert len(state["seen"]) == 1


def test_news_alerts_fresh_high_impact_once():
    now = time.time()
    state = {"seeded": True, "seen": {}}
    items = [_item("Fed cuts interest rates by 50bps")]
    out = ER._news_alerts(items, state, now)
    assert len(out) == 1 and "FED" in out[0]
    # same headline again → deduped
    assert ER._news_alerts(items, state, now) == []


def test_news_skips_stale_and_neutral():
    now = time.time()
    state = {"seeded": True, "seen": {}}
    items = [_item("War escalation fears grow", age_sec=ER.FRESH_SEC + 3600),
             _item("Top 5 altcoins to watch")]
    assert ER._news_alerts(items, state, now) == []


def test_news_storm_cap():
    now = time.time()
    state = {"seeded": True, "seen": {}}
    items = [_item(f"Fed rate decision shocks markets, take {i}") for i in range(10)]
    assert len(ER._news_alerts(items, state, now)) == ER.MAX_PER_TICK


# ── calendar detector ────────────────────────────────────────────────────────
def _cal_event(offset_sec, title="CPI m/m"):
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) + timedelta(seconds=offset_sec)
    return {"title": title, "date": dt.isoformat(), "forecast": "0.3%", "previous": "0.2%"}


def test_calendar_alert_inside_window_once():
    now = time.time()
    state = {}
    ev = [_cal_event(1200)]               # 20 min out — inside the 45-min lead
    out = ER._calendar_alerts(ev, state, now)
    assert len(out) == 1 and "CPI" in out[0]
    assert ER._calendar_alerts(ev, state, now) == []   # deduped


def test_calendar_ignores_far_and_past_events():
    now = time.time()
    state = {}
    evs = [_cal_event(ER.CAL_LEAD_SEC + 3600), _cal_event(-600)]
    assert ER._calendar_alerts(evs, state, now) == []


# ── shock detector ───────────────────────────────────────────────────────────
class _FakeClient:
    def __init__(self, closes):
        self._closes = closes

    def call(self, method, *a, **k):
        assert method == "fetch_ohlcv"
        # candles: [ts, o, h, l, close, vol]; append a forming candle that must be dropped
        return [[0, 0, 0, 0, c, 1] for c in self._closes] + [[0, 0, 0, 0, 9e9, 1]]


def test_shock_fires_on_fast_move_with_cooldown():
    flat = [100.0] * 13
    spiked = flat[:-1] + [102.5]          # +2.5% in 15m
    state = {}
    out = ER._shock_alerts(_FakeClient(spiked), state, time.time())
    assert len(out) == 2                  # BTC and ETH fake the same feed here
    assert "MARKET SHOCK" in out[0]
    # cooldown: immediate second tick stays silent
    assert ER._shock_alerts(_FakeClient(spiked), state, time.time()) == []


def test_shock_quiet_on_calm_tape():
    calm = [100.0 + i * 0.01 for i in range(13)]
    assert ER._shock_alerts(_FakeClient(calm), {}, time.time()) == []
