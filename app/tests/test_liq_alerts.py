"""liq_alerts — cascade detection, cooldown/escalation, message formatting."""
import liq_alerts


def _ev(sym, side, usd, ts_ms, ex="Binance"):
    return {"ts": ts_ms, "ex": ex, "sym": sym, "side": side, "usd": usd}


# A REALISTIC fake clock (2026-07). The original 1_000_000_000_000 (year 2001)
# made urllib3 warn "system time is way off" whenever a test touched the
# network — which the tick test accidentally did (see the fetch_klines stub).
NOW_MS = 1_783_800_000_000


# ── window_stats ─────────────────────────────────────────────────────────────
def test_window_stats_filters_symbol_and_time():
    events = [
        _ev("BTC", "long", 1_000_000, NOW_MS - 60_000),
        _ev("BTC", "short", 500_000, NOW_MS - 120_000),
        _ev("ETH", "long", 900_000, NOW_MS - 60_000),          # other symbol
        _ev("BTC", "long", 700_000, NOW_MS - 700_000),         # outside 10min
    ]
    st = liq_alerts.window_stats(events, "BTC", 600, now_ms=NOW_MS)
    assert st["long_usd"] == 1_000_000
    assert st["short_usd"] == 500_000
    assert st["n"] == 2
    assert st["largest"]["usd"] == 1_000_000


def test_window_stats_empty():
    st = liq_alerts.window_stats([], "BTC", 600, now_ms=NOW_MS)
    assert st["total_usd"] == 0 and st["largest"] is None


# ── classify_side ────────────────────────────────────────────────────────────
def test_classify_long_dominant():
    st = {"long_usd": 8.0, "short_usd": 2.0, "total_usd": 10.0}
    assert liq_alerts.classify_side(st) == "long"


def test_classify_short_dominant():
    st = {"long_usd": 1.0, "short_usd": 9.0, "total_usd": 10.0}
    assert liq_alerts.classify_side(st) == "short"


def test_classify_mixed():
    st = {"long_usd": 5.0, "short_usd": 5.0, "total_usd": 10.0}
    assert liq_alerts.classify_side(st) == "mixed"


# ── should_alert ─────────────────────────────────────────────────────────────
def test_alert_fires_over_threshold():
    st = {"total_usd": 2_500_000}
    assert liq_alerts.should_alert(st, 2_000_000, None, now=1000.0)


def test_no_alert_under_threshold():
    st = {"total_usd": 1_500_000}
    assert not liq_alerts.should_alert(st, 2_000_000, None, now=1000.0)


def test_cooldown_blocks_repeat():
    st = {"total_usd": 2_500_000}
    last = {"ts": 1000.0, "usd": 2_400_000}
    assert not liq_alerts.should_alert(st, 2_000_000, last, now=1300.0,
                                       cooldown_sec=1800)


def test_escalation_breaks_cooldown():
    st = {"total_usd": 8_000_000}                   # ≥3× the alerted burst
    last = {"ts": 1000.0, "usd": 2_400_000}
    assert liq_alerts.should_alert(st, 2_000_000, last, now=1300.0,
                                   cooldown_sec=1800)


def test_alert_again_after_cooldown():
    st = {"total_usd": 2_100_000}
    last = {"ts": 1000.0, "usd": 2_400_000}
    assert liq_alerts.should_alert(st, 2_000_000, last, now=3000.0,
                                   cooldown_sec=1800)


# ── build_alert ──────────────────────────────────────────────────────────────
def _stats():
    return {"long_usd": 7_500_000, "short_usd": 700_000, "total_usd": 8_200_000,
            "n": 43, "largest": _ev("BTC", "long", 1_200_000, NOW_MS)}


def test_build_alert_long_sweep():
    day = {"long_usd": 45_000_000, "short_usd": 12_000_000, "total_usd": 57e6,
           "n": 900, "largest": None}
    ticker = {"last": 67_850.0, "percentage": -1.8, "high": 70_100.0, "low": 66_900.0}
    msg = liq_alerts.build_alert("BTC", _stats(), "long", day, ticker)
    assert "多單被掃" in msg
    assert "$8.2M" in msg and "43 筆" in msg
    assert "最大單筆 $1.2M · Binance" in msg
    assert "價格 67,850" in msg
    assert "注意反轉" in msg
    assert "多單 $45.0M / 空單 $12.0M" in msg
    assert "非進場訊號" in msg          # honesty footer is not optional


def test_build_alert_survives_missing_ticker():
    day = {"long_usd": 0.0, "short_usd": 0.0, "total_usd": 0.0, "n": 0,
           "largest": None}
    msg = liq_alerts.build_alert("ETH", _stats(), "short", day, ticker=None)
    assert "空單被掃" in msg
    assert not any(ln.startswith("價格") for ln in msg.splitlines())


# ── fmt_summary ──────────────────────────────────────────────────────────────
def test_fmt_summary():
    snap = {"total_usd": 90e6, "long_usd": 60e6, "short_usd": 30e6,
            "largest": {"symbol": "BTC", "side": "long", "usd": 2.5e6,
                        "exchange": "OKX"},
            "collecting_since": 1000.0}
    d = {"long_usd": 45e6, "short_usd": 12e6, "n": 900}
    msg = liq_alerts.fmt_summary(snap, d, d)
    assert "BTC: 多單 $45.0M" in msg
    assert "全市場: $90.0M" in msg
    assert "BTC 多單 $2.5M @ OKX" in msg
    assert "收集中" in msg              # restart-resets honesty line


def test_fmt_summary_collector_down():
    snap = {"total_usd": 0, "long_usd": 0, "short_usd": 0, "largest": None,
            "collecting_since": None}
    d = {"long_usd": 0.0, "short_usd": 0.0, "n": 0}
    assert "收集器未啟動" in liq_alerts.fmt_summary(snap, d, d)


# ── tick (full path, no sockets) ─────────────────────────────────────────────
class _FakeClient:
    def fetch_ticker(self, pair):
        return {"last": 67_850.0, "percentage": -1.8,
                "high": 70_100.0, "low": 66_900.0}


def _no_network(*_a, **_k):
    raise RuntimeError("tests must not fetch klines from the network")


def test_tick_sends_on_cascade(monkeypatch):
    import liquidations
    import telegram_utils
    events = [_ev("BTC", "long", 3_000_000, NOW_MS - 60_000)]
    monkeypatch.setattr(liquidations, "events_copy", lambda: events)
    monkeypatch.setattr(liq_alerts, "fetch_klines", _no_network)  # map is optional
    monkeypatch.setattr(liq_alerts.time, "time", lambda: NOW_MS / 1000)
    sent = []
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **kw: sent.append((msg, kw)) or True)
    monkeypatch.setattr(liq_alerts, "_last_alert", {})
    assert liq_alerts.tick(_FakeClient()) == 1
    msg, kw = sent[0]
    assert kw["channel"] == "liq" and "BTC 多單被掃" in msg


def test_tick_quiet_market_sends_nothing(monkeypatch):
    import liquidations
    events = [_ev("BTC", "long", 100_000, NOW_MS - 60_000)]
    monkeypatch.setattr(liquidations, "events_copy", lambda: events)
    monkeypatch.setattr(liq_alerts, "fetch_klines", _no_network)
    monkeypatch.setattr(liq_alerts.time, "time", lambda: NOW_MS / 1000)
    monkeypatch.setattr(liq_alerts, "_last_alert", {})
    assert liq_alerts.tick(_FakeClient()) == 0


# ── recent prints with prices ────────────────────────────────────────────────
def _pev(sym, side, usd, ts_ms, px, ex="Binance"):
    return {"ts": ts_ms, "ex": ex, "sym": sym, "side": side, "usd": usd, "px": px}


def test_recent_liqs_newest_first_with_price():
    events = [
        _pev("BTC", "long", 85_000, NOW_MS - 180_000, 67_912.0),
        _pev("BTC", "short", 2_100_000, NOW_MS - 60_000, 68_450.0, ex="OKX"),
        _pev("BTC", "long", 5_000, NOW_MS - 30_000, 67_900.0),   # dust — filtered
        _pev("ETH", "long", 90_000, NOW_MS - 10_000, 1_800.0),   # other symbol
    ]
    rows = liq_alerts.recent_liqs(events, "BTC", now_ms=NOW_MS)
    assert len(rows) == 2
    assert rows[0] == "🔺空 $2.1M @ 68,450 · OKX · 1分前"
    assert rows[1] == "🔻多 $85K @ 67,912 · Binance · 3分前"


def test_recent_liqs_skips_priceless_legacy_events():
    events = [_ev("BTC", "long", 500_000, NOW_MS - 1000)]        # no px key
    assert liq_alerts.recent_liqs(events, "BTC", now_ms=NOW_MS) == []


def test_window_stats_price_band():
    events = [
        _pev("BTC", "long", 1_000_000, NOW_MS - 60_000, 67_100.0),
        _pev("BTC", "long", 1_000_000, NOW_MS - 90_000, 66_900.0),
    ]
    st = liq_alerts.window_stats(events, "BTC", 600, now_ms=NOW_MS)
    assert st["px_min"] == 66_900.0 and st["px_max"] == 67_100.0


def test_build_alert_includes_price_band_and_magnet():
    stats = liq_alerts.window_stats(
        [_pev("BTC", "long", 3_000_000, NOW_MS - 60_000, 67_000.0)],
        "BTC", 600, now_ms=NOW_MS)
    day = {"long_usd": 1e6, "short_usd": 1e6, "total_usd": 2e6, "n": 5,
           "largest": None}
    liqmap = {"price": 67_000.0,
              "long": [(65_800.0, 9.0), (64_200.0, 5.0)], "short": []}
    msg = liq_alerts.build_alert("BTC", stats, "long", day, liqmap=liqmap)
    assert "清算價格帶 67,000 – 67,000" in msg
    assert "🧲 下一個磁鐵 ~65,800 (-1.8%)" in msg   # closest surviving below


# ── 🧲 liquidation map ───────────────────────────────────────────────────────
def _flat_klines(n=30, px=100.0, qv=1000.0):
    return [(i, px, px, px, px, qv) for i in range(n)]


def test_liq_map_levels_sit_around_price():
    m = liq_alerts.liq_map(_flat_klines())
    assert m["price"] == 100.0
    assert m["long"] and m["short"]
    assert all(p < 100.0 for p, _s in m["long"])
    assert all(p > 100.0 for p, _s in m["short"])
    # 10x tier from entry 100 → long liq ≈ 90, short liq ≈ 110
    assert any(abs(p - 90.0) < 0.5 for p, _s in m["long"])
    assert any(abs(p - 110.0) < 0.5 for p, _s in m["short"])


def test_liq_map_removes_swept_levels():
    # same anchors, but a crash wick to 89 AFTER them clears every long level
    calm = _flat_klines(30)
    swept = _flat_klines(28) + [(28, 100, 100, 89.0, 100, 1000.0),
                                (29, 100, 100, 100, 100, 1000.0)]
    strength = lambda m: sum(s for _p, s in m["long"])  # noqa: E731
    assert strength(liq_alerts.liq_map(swept)) < strength(liq_alerts.liq_map(calm))


def test_liq_map_insufficient_data():
    m = liq_alerts.liq_map([])
    assert m["long"] == [] and m["short"] == []


def test_fmt_map_renders_bars():
    m = {"price": 67_850.0,
         "long": [(66_490.0, 8.0)], "short": [(69_420.0, 10.0), (68_510.0, 4.0)]}
    txt = liq_alerts.fmt_map(m, "BTC")
    assert "現價 67,850" in txt
    assert "69,420 (+2.3%) ▰▰▰▰▰" in txt
    assert "66,490 (-2.0%) ▰▰▰▰" in txt
    assert "上方磁鐵" in txt and "下方磁鐵" in txt


def test_fmt_summary_includes_recent_and_map():
    snap = {"total_usd": 0, "long_usd": 0, "short_usd": 0, "largest": None,
            "collecting_since": 1000.0}
    d = {"long_usd": 0.0, "short_usd": 0.0, "n": 0}
    msg = liq_alerts.fmt_summary(
        snap, d, d,
        recent={"BTC": ["🔻多 $85K @ 67,912 · Binance · 3分前"], "ETH": []},
        maps={"BTC": {"price": 67_850.0, "long": [(66_490.0, 8.0)], "short": []}})
    assert "🕐 BTC 最近清算:" in msg and "@ 67,912" in msg
    assert "清算地圖" in msg
    assert "僅供參考" in msg            # the map is a model, never presented as data
