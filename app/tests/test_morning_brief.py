"""☀️ Public morning brief — pure-logic tests (no network, no state file).

The brief goes to the GROUP (report topic), so the critical property is that
it can never leak account data: build_brief only ever sees market fields.
"""
from datetime import datetime

import morning_brief as MB


def _now(hour=9):
    return datetime(2026, 7, 17, hour, 30, tzinfo=MB.TZ)


def _data():
    return {
        "prices": {"BTC": {"last": 108432.0, "pct": 1.2},
                   "ETH": {"last": 3520.5, "pct": -2.0},
                   "SOL": {"last": 155.0, "pct": None}},
        "fng": {"value": 61, "label": "Greed", "week_ago": 48},
        "global": {"btc_dominance": 62.34},
        "today_events": ["  • 20:30 CPI y/y（預測 2.4%）"],
        "signals": {"n": 14, "premium": 2},
        "outcomes": {"n": 9, "hit_pct": 55.6},
    }


# ── cadence gate ─────────────────────────────────────────────────────────────
def test_due_only_after_brief_hour():
    assert not MB._due({}, _now(hour=MB.BRIEF_HOUR - 1))
    assert MB._due({}, _now(hour=MB.BRIEF_HOUR))


def test_due_once_per_day():
    today = _now().strftime("%Y-%m-%d")
    assert not MB._due({"last_brief": today}, _now())
    assert MB._due({"last_brief": "2026-07-16"}, _now())


# ── brief body ───────────────────────────────────────────────────────────────
def test_brief_has_all_sections(monkeypatch):
    import macro_events
    monkeypatch.setattr(macro_events, "today_lines", lambda *a, **k: ["  今日無高影響美國數據"])
    monkeypatch.setattr(macro_events, "lines", lambda *a, **k: ["  🔴 08/12 20:30 CPI 通膨年增"])
    msg = MB.build_brief(_data(), _now())
    assert "🗓 今日" in msg and "📅 本週要看的數據" in msg and "CPI 通膨年增" in msg
    assert "早安市場快報" in msg and "2026-07-17" in msg and "週五" in msg
    assert "BTC 108,432（+1.2%）" in msg
    assert "ETH 3,520.5（−2.0%）" in msg          # real minus glyph
    assert "SOL 155" in msg                        # missing pct → price only
    assert "恐懼貪婪 61（貪婪） · 週前 48" in msg
    assert "BTC 佔比 62.3%" in msg
    assert "14 個訊號" in msg and "⭐ 精選 2 個" in msg
    assert "已結算 9 個訊號" in msg and "56%" in msg
    assert "/guide" in msg and "非投資建議" in msg


def test_brief_degrades_without_data():
    msg = MB.build_brief({}, _now())
    assert "早安市場快報" in msg
    assert "今日無高影響美國數據" in msg
    assert "💰" not in msg                         # no fake price line
    assert "/help" in msg                          # footer always present


def test_brief_hides_outcome_stat_below_sample_floor():
    d = _data()
    d["outcomes"] = {"n": 3, "hit_pct": 100.0}     # tiny sample = braggy noise
    assert "已結算" not in MB.build_brief(d, _now())


def test_brief_never_contains_account_words():
    # The public brief must stay account-free by CONSTRUCTION — no balance,
    # equity or per-account P&L wording may ever appear.
    msg = MB.build_brief(_data(), _now())
    for banned in ("餘額", "可用", "USDT", "Binance", "Bybit", "持倉", "未實現"):
        assert banned not in msg


# ── gather helpers ───────────────────────────────────────────────────────────
class _FakeClient:
    def call(self, method, syms):
        assert method == "fetch_tickers"
        return {"BTC/USDT:USDT": {"last": 108000.0, "percentage": 0.5},
                "ETH/USDT:USDT": {"last": None},               # dead ticker
                "SOL/USDT:USDT": {"close": 155.0}}             # no 'last' key


def test_gather_prices_bulk_and_degrading():
    out = MB._gather_prices(_FakeClient())
    assert out["BTC"] == {"last": 108000.0, "pct": 0.5}
    assert "ETH" not in out                        # unpriced ticker dropped
    assert out["SOL"]["last"] == 155.0 and out["SOL"]["pct"] is None


def test_gather_prices_survives_client_error():
    class _Boom:
        def call(self, *a):
            raise RuntimeError("exchange down")
    assert MB._gather_prices(_Boom()) == {}


def test_signal_tally_counts_last_24h(monkeypatch, tmp_path):
    import json
    import time
    f = tmp_path / "sigs.json"
    now = time.time()
    f.write_text(json.dumps({"signals": [
        {"ts": now - 3600, "premium": True},
        {"ts": now - 7200},
        {"ts": now - 90000},                       # >24h → excluded
    ]}))
    monkeypatch.setattr(MB, "SIGNALS_FILE", str(f))
    assert MB._signal_tally(now) == {"n": 2, "premium": 1}


# ── tick(): pin wiring ────────────────────────────────────────────────────────
def test_tick_sends_and_pins_unpinning_yesterdays(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(MB, "STATE_FILE", str(tmp_path / "brief.json"))
    monkeypatch.setattr(MB, "_due", lambda state, now: True)
    monkeypatch.setattr(MB, "_gather", lambda client, now: _data())
    monkeypatch.setattr(MB.telegram_utils, "send_message_and_pin",
                        lambda *a, **kw: calls.append(kw) or 909)
    MB._save_state({"pinned_mid": 808})

    assert MB.tick(client=None) is True
    assert calls[0]["channel"] == "report"
    assert calls[0]["unpin_previous"] == 808
    assert MB._load_state()["pinned_mid"] == 909


def test_tick_false_when_send_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(MB, "STATE_FILE", str(tmp_path / "brief.json"))
    monkeypatch.setattr(MB, "_due", lambda state, now: True)
    monkeypatch.setattr(MB, "_gather", lambda client, now: _data())
    monkeypatch.setattr(MB.telegram_utils, "send_message_and_pin",
                        lambda *a, **kw: None)
    assert MB.tick(client=None) is False
    assert "pinned_mid" not in MB._load_state()
