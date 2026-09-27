"""
📓 The OI radar's forward book — what followed every pile-up on the board.

crowd_radar's own docstring admits nothing there was ever measured against
forward returns. These pin the counting: what is recorded, when a horizon is
due, how the reference price is chosen, and that the summary refuses to
speak below MIN_N.
"""
import crowd_outcomes as O
import crowd_radar as C

T0 = 1_700_000_000.0
BAR = 900_000


def _row(state="longs_opening", ts=T0, sym="HOTUSDT"):
    return {"symbol": sym, "state": state, "ts": ts, "oi_pct": 12.0,
            "px_pct": 3.0, "pctile": 99.1, "tier": "中型", "notional": 5e7}


def _klines(ts, drift, n=100, start_px=100.0):
    """Binance-shaped 15m rows from the first bar opening after `ts`, price
    multiplied by `drift` per bar."""
    start = (int(ts * 1000) // BAR + 1) * BAR
    out, px = [], start_px
    for i in range(n):
        o = px
        px = px * drift
        out.append([start + i * BAR, o, max(o, px) * 1.001, min(o, px) * 0.999,
                    px, 1.0, start + (i + 1) * BAR - 1])
    return out


def _settled(state, drift, n_rows, now_offset_h=25):
    store = C._blank()
    for i in range(n_rows):
        O.note(store, _row(state, ts=T0 + i, sym=f"S{i}USDT"))
    O.settle(store, lambda s, ts: _klines(ts, drift), now=T0 + now_offset_h * 3600,
             max_fetch=10_000)
    return store


# ── recording ────────────────────────────────────────────────────────────────
def test_a_board_row_is_opened_once():
    store = C._blank()
    assert O.note(store, _row())
    assert not O.note(store, _row()), "the same pile-up was opened twice"
    assert len(store["book"]["open"]) == 1


def test_a_row_without_a_known_read_is_not_recorded():
    store = C._blank()
    assert not O.note(store, {**_row(), "state": "mystery"})
    assert not O.note(store, {**_row(), "ts": None})


def test_the_scan_puts_every_board_row_in_the_book(monkeypatch):
    """The record must cover the same events the reader saw, not a subset."""
    store = C._blank()
    monkeypatch.setattr(C, "PACE_SEC", 0)
    row = {**_row(), "massive": True, "samples": 300, "reason": None}
    monkeypatch.setattr(C, "oi_history", lambda s, **k: ([1000.0] * 300, [1.0] * 300))
    monkeypatch.setattr(C, "live_oi", lambda s: 1000.0)
    monkeypatch.setattr(C, "assess", lambda oi, px, **k: dict(row))
    monkeypatch.setattr(C, "ls_ratio", lambda s, **k: [])
    out = C.scan([{"symbol": "HOTUSDT", "turnover": 5e8}], now=T0, store=store)
    assert out["hits"] and list(store["book"]["open"]) == ["HOTUSDT:longs_opening:1700000000"]
    assert store["last_scan"]["checked"] == 1


# ── when a horizon is due ────────────────────────────────────────────────────
def test_a_horizon_is_due_only_after_its_closing_bar_printed():
    e = {"ts": T0, "ret": {}}
    assert O.due_horizons(e, T0 + 3600) == [], "1h asked before the 1h bar closed"
    assert O.due_horizons(e, T0 + 3600 + 15 * 60) == [1]
    assert O.due_horizons(e, T0 + 25 * 3600) == [1, 4, 24]
    e["ret"]["1"] = 0.1
    assert O.due_horizons(e, T0 + 25 * 3600) == [4, 24]


# ── scoring ──────────────────────────────────────────────────────────────────
def test_the_reference_is_the_open_of_the_first_bar_after_the_alert():
    """Never the radar's implied price (a notional over a coin count), and
    never a bar that printed before the alert existed."""
    e = {"ts": T0, "ret": {}}
    kl = _klines(T0, 1.0)
    # A bar that opened BEFORE the alert must not supply the reference.
    kl.insert(0, [kl[0][0] - BAR, 50.0, 51.0, 49.0, 50.5, 1.0, kl[0][0] - 1])
    assert O.score(e, kl, now=T0 + 2 * 3600)
    assert e["px0"] == 100.0
    assert e["ret"]["1"] == 0.0


def test_returns_are_measured_from_the_alert_not_the_reference_bar():
    e = {"ts": T0, "ret": {}}
    O.score(e, _klines(T0, 1.01), now=T0 + 25 * 3600)
    assert e["ret"]["1"] > 0 and e["ret"]["4"] > e["ret"]["1"] and e["ret"]["24"] > e["ret"]["4"]
    assert e["max_up"] >= e["ret"]["24"] and e["max_dn"] <= 0


def test_a_symbol_with_no_candles_is_not_scored():
    e = {"ts": T0, "ret": {}}
    assert not O.score(e, [], now=T0 + 25 * 3600)
    assert "px0" not in e


def test_settle_closes_at_the_last_horizon_and_counts_lifetime():
    store = C._blank()
    O.note(store, _row())
    r = O.settle(store, lambda s, ts: _klines(ts, 0.999), now=T0 + 1.5 * 3600)
    assert r["scored"] == 1 and r["closed"] == 0 and r["open"] == 1
    r = O.settle(store, lambda s, ts: _klines(ts, 0.999), now=T0 + 25 * 3600)
    assert r["closed"] == 1 and not store["book"]["open"]
    assert store["book"]["n_lifetime"] == 1
    assert set(store["book"]["closed"][0]["ret"]) == {"1", "4", "24"}


def test_a_dead_symbol_costs_one_error_and_never_the_tick():
    store = C._blank()
    O.note(store, _row(sym="DEADUSDT"))
    O.note(store, _row(sym="LIVEUSDT"))

    def fetch(sym, ts):
        if sym == "DEADUSDT":
            raise RuntimeError("delisted")
        return _klines(ts, 1.0)
    r = O.settle(store, fetch, now=T0 + 25 * 3600)
    assert r["errors"] == 1 and r["closed"] == 1


def test_a_row_with_no_data_for_three_days_is_dropped_and_counted():
    store = C._blank()
    O.note(store, _row())
    O.settle(store, lambda s, ts: [], now=T0 + 73 * 3600)
    assert not store["book"]["open"] and store["book"]["dropped"] == 1


def test_the_per_tick_cap_rotates_rather_than_starving_the_back_of_the_queue():
    store = C._blank()
    for i in range(3):
        O.note(store, _row(sym=f"S{i}USDT"))
    calls = []

    def fetch(sym, ts):
        calls.append(sym)
        return []                            # nothing scores, everything stays open
    O.settle(store, fetch, now=T0 + 2 * 3600, max_fetch=2)
    O.settle(store, fetch, now=T0 + 2 * 3600 + 1, max_fetch=2)
    assert set(calls) == {"S0USDT", "S1USDT", "S2USDT"}, "one row was never looked at"


# ── the read ─────────────────────────────────────────────────────────────────
def test_the_summary_refuses_a_verdict_below_min_n():
    store = _settled("longs_opening", 0.998, n_rows=5)
    s = O.summary(store)["states"]["longs_opening"]
    assert s["h"]["4"]["n"] == 5 and s["h"]["4"]["rev_pct"] == 100.0
    assert "樣本不足" in s["verdict_zh"]
    assert O.record_line(store, "longs_opening") == ""


def test_a_squeeze_is_reversed_price_for_a_build():
    """New longs with price then falling = the crowd got hurt."""
    store = _settled("longs_opening", 0.998, n_rows=40)
    s = O.summary(store)["states"]["longs_opening"]
    assert s["h"]["4"]["rev_pct"] == 100.0 and s["h"]["4"]["rev_ci"][0] > 50
    assert s["verdict_zh"].startswith("反向擠壓成立")
    line = O.record_line(store, "longs_opening")
    assert "過去 40 次" in line and "100% 價格反向" in line
    assert "機率" not in line and "probability" not in line.lower()


def test_price_following_the_build_is_no_squeeze():
    store = _settled("shorts_opening", 0.998, n_rows=40)   # shorts, price keeps falling
    s = O.summary(store)["states"]["shorts_opening"]
    assert s["h"]["4"]["cont_pct"] == 100.0
    assert s["verdict_zh"].startswith("沒有擠壓")


def test_an_unwind_is_read_as_continuation_not_squeeze():
    store = _settled("shorts_closing", 1.002, n_rows=40)   # covering, price keeps rising
    s = O.summary(store)["states"]["shorts_closing"]
    assert s["verdict_zh"] == "多半延續"
    assert "延續原方向" in O.record_line(store, "shorts_closing")


def test_open_rows_feed_the_short_horizons_before_the_long_one_closes():
    store = C._blank()
    O.note(store, _row())
    O.settle(store, lambda s, ts: _klines(ts, 0.999), now=T0 + 1.5 * 3600)
    s = O.summary(store)["states"]["longs_opening"]["h"]
    assert s["1"]["n"] == 1 and s["24"]["n"] == 0
    assert O.summary(store)["pending"] == 1


def test_the_squeeze_size_is_the_median_excursion_against_the_crowd():
    store = _settled("longs_opening", 0.998, n_rows=3)
    s = O.summary(store)
    assert s["squeeze_n"] == 3 and s["squeeze_median_pct"] > 0


def test_wilson_does_not_collapse_at_the_edges():
    lo, hi = O.wilson(0, 10)
    assert lo == 0.0 and hi > 0
    lo, hi = O.wilson(10, 10)
    assert hi == 100.0 and lo < 100


def test_the_alert_carries_the_record_once_it_exists():
    store = _settled("longs_opening", 0.998, n_rows=40)
    row = {**_row(), "samples": 300, "span_h": 2.0, "turnover": 5e8}
    msg = C.build_alert(row, O.record_line(store, "longs_opening"))
    assert "📓 過去 40 次" in msg
    assert "不是進場訊號" in msg, "the record must not displace the disclaimer"
    assert "沒有" not in C.build_alert(row) or "📓" not in C.build_alert(row)


def test_the_tick_settles_after_the_sweep(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(C, "PACE_SEC", 0)
    monkeypatch.setattr(C, "scan", lambda **k: {"checked": 0, "hits": []})
    monkeypatch.setattr(C, "klines_after", lambda s, ts: _klines(ts, 1.0))
    store = C._blank()
    O.note(store, _row())
    C.save(store)
    out = C.tick(now=T0 + 25 * 3600, force=True)
    assert out["book"]["closed"] == 1
    assert C.load()["book"]["n_lifetime"] == 1


def test_the_book_is_written_and_settled_by_the_radars_own_tick():
    """The dry-run audit looks for note/tick pairs in the sweep. This book
    is both written and settled one level down, inside crowd_radar.tick —
    which the sweep calls — so the same "records but never settles" failure
    the zone book had for weeks is checked here, at the level it lives."""
    import ast
    import os
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def calls(module):
        with open(os.path.join(app_dir, module + ".py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        return {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)}
    radar = calls("crowd_radar")
    assert "crowd_outcomes.note" in radar, "the radar never records a pile-up"
    assert "crowd_outcomes.settle" in radar, "the radar records but never settles"
    assert "crowd_radar.tick" in calls("strategy2_scanner"), "the sweep never runs the radar"
