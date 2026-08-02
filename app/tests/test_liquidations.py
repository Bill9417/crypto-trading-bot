"""Liquidation tracker — parser + aggregation tests (no sockets opened:
importing liquidations must never connect; only start() does)."""
import json
import time

import liquidations as L

# Events older than the 24h rolling window are trimmed by _push on arrival,
# so parser fixtures must carry a CURRENT timestamp to survive into _events.
NOW_MS = int(time.time() * 1000)


def _clear():
    with L._lock:
        L._events.clear()


def test_import_does_not_start_collector():
    assert L._started is False or L._started is True  # attribute exists
    # The real guarantee: no thread named liq-* unless start() was called.
    import threading
    if not L._started:
        assert not [t for t in threading.enumerate() if t.name.startswith("liq-")]


def test_binance_parser_sell_order_is_long_liquidation():
    _clear()
    msg = json.dumps({"e": "forceOrder", "o": {
        "s": "BTCUSDT", "S": "SELL", "q": "0.5", "p": "60000", "ap": "60100",
        "T": NOW_MS}})
    L._on_binance(msg)
    with L._lock:
        ev = L._events[-1]
    assert ev["sym"] == "BTC"
    assert ev["side"] == "long"            # SELL force order closes a LONG
    assert abs(ev["usd"] - 0.5 * 60100) < 1e-6


def test_bybit_parser_S_is_position_side_and_px_never_displayed():
    # Bybit docs (allLiquidation): "S — Position side. When you receive a Buy
    # update, this means that a LONG position has been liquidated." This is
    # the OPPOSITE of Binance's order-side convention — the 2026-07-14 fix
    # (every Bybit event was counted on the wrong side before).
    _clear()
    msg = json.dumps({"topic": "allLiquidation.SOLUSDT", "data": [
        {"s": "SOLUSDT", "S": "Buy", "v": "10", "p": "82.5", "T": NOW_MS}]})
    L._on_bybit(msg)
    with L._lock:
        ev = L._events[-1]
    assert ev["sym"] == "SOL"
    assert ev["side"] == "long"            # Buy update = a LONG died
    assert abs(ev["usd"] - 825.0) < 1e-6   # bankruptcy px still sizes the value
    assert ev["px"] == 0.0                 # …but is never shown as a print price

    msg2 = json.dumps({"topic": "allLiquidation.SOLUSDT", "data": [
        {"s": "SOLUSDT", "S": "Sell", "v": "4", "p": "83.0", "T": NOW_MS}]})
    L._on_bybit(msg2)
    with L._lock:
        ev2 = L._events[-1]
    assert ev2["side"] == "short"          # Sell update = a SHORT died


def test_okx_parser_uses_ctval_and_skips_unknown_instruments():
    _clear()
    L._okx_ctval["BTC-USDT-SWAP"] = 0.01
    msg = json.dumps({"arg": {"channel": "liquidation-orders", "instType": "SWAP"},
                      "data": [{"instId": "BTC-USDT-SWAP", "details": [
                          {"posSide": "long", "side": "sell", "sz": "100",
                           "bkPx": "60000", "ts": str(NOW_MS)}]}]})
    L._on_okx(msg)
    with L._lock:
        ev = L._events[-1]
    # 100 contracts × 0.01 BTC × 60000 = 60,000 USDT (NOT 6,000,000 — the
    # exact 100× bug the ctVal cache exists to prevent)
    assert abs(ev["usd"] - 60000.0) < 1e-6
    assert ev["px"] == 0.0    # bkPx = bankruptcy price, not a real fill — never displayed
    # unknown instrument (no ctVal loaded) must be skipped, never guessed
    n_before = len(L._events)
    msg2 = json.dumps({"arg": {"channel": "liquidation-orders", "instType": "SWAP"},
                       "data": [{"instId": "ZZZ-USDT-SWAP", "details": [
                           {"posSide": "long", "side": "sell", "sz": "5",
                            "bkPx": "10", "ts": str(NOW_MS)}]}]})
    L._on_okx(msg2)
    with L._lock:
        assert len(L._events) == n_before


def test_aggregate_windows_and_breakdowns():
    now = int(time.time() * 1000)
    evs = [
        {"ts": now - 1000, "ex": "Binance", "sym": "BTC", "side": "long", "usd": 50000},
        {"ts": now - 2000, "ex": "OKX", "sym": "ETH", "side": "short", "usd": 12000},
        {"ts": now - 2 * 3600 * 1000, "ex": "Bybit", "sym": "SOL", "side": "long", "usd": 800},
        {"ts": now - 30 * 3600 * 1000, "ex": "OKX", "sym": "DOGE", "side": "short", "usd": 999},
    ]
    agg = L.aggregate(evs, 86400, now)
    assert agg["n_events"] == 3                       # 30h-old event outside window
    assert agg["long_usd"] == 50800
    assert agg["short_usd"] == 12000
    assert agg["largest"]["symbol"] == "BTC" and agg["largest"]["exchange"] == "Binance"
    assert agg["exchanges"][0]["exchange"] == "Binance"   # sorted by total desc
    assert len(agg["series"]) == 24
    assert L.aggregate(evs, 3600, now)["n_events"] == 2   # 1h window


def test_headline_sentiment():
    from market_intel import headline_sentiment
    assert headline_sentiment("Bitcoin surges to record high as ETF inflows accelerate") == "bullish"
    assert headline_sentiment("Exchange hacked, token plunges 40% in massive sell-off") == "bearish"
    assert headline_sentiment("Tokenization panel discusses custody standards") == "neutral"
    # mixed headline: equal hits → neutral, never a false positive
    assert headline_sentiment("Bitcoin jumps above $63,000, reversing end-June losses") == "neutral"


def test_concurrent_saves_do_not_race(tmp_path, monkeypatch):
    """Three WebSocket threads all call _maybe_save() via _push. A shared
    "<file>.tmp" meant the first os.replace consumed it and the rest raised
    FileNotFoundError, silently losing those saves — seen in the real S2 log
    the moment persistence shipped. A PID tag alone does not fix it either:
    same process, same PID. Saves are serialised."""
    import threading as _t
    import liquidations as L
    monkeypatch.setattr(L, "BUFFER_FILE", str(tmp_path / "buf.json"))
    L._events.clear()
    for i in range(10):
        L._push(int(time.time() * 1000) - i * 1000, "Binance", "BTCUSDT",
                "long", 10.0, 63000.0)

    results, errors = [], []

    def _w():
        try:
            results.append(L.save_buffer())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [_t.Thread(target=_w) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert not errors, f"concurrent saves raised: {errors}"
    assert any(results), "every save was skipped — nothing persisted"
    assert not list(tmp_path.glob("*.tmp")), "a stray temp file was left behind"
    L._events.clear()
    assert L.load_buffer() == 10


def test_a_restarted_process_cannot_wipe_the_other_ones_history(tmp_path, monkeypatch):
    """Observed live: the persisted buffer went from 82 events to 2 when a
    freshly restarted web process saved over the S2 scanner's hours of data.
    Two collectors run independently, so a save has to MERGE with disk, not
    overwrite it."""
    import liquidations as L
    monkeypatch.setattr(L, "BUFFER_FILE", str(tmp_path / "buf.json"))
    now = time.time() * 1000

    L._events.clear()                                   # collector A: lots of history
    for i in range(50):
        L._push(int(now - i * 1000), "Binance", "BTCUSDT", "long", 10.0, 63000.0 + i)
    L.save_buffer()

    L._events.clear()                                   # collector B: just restarted
    L._push(int(now - 500), "Binance", "ETHUSDT", "short", 5.0, 1800.0)
    L.save_buffer()

    L._events.clear()
    assert L.load_buffer() >= 51, "a restarted process destroyed the other's history"
