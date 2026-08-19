"""📈 Chart overlays: presets and support/resistance.

The 幣種分析 chart drew ten series at once — four EMAs, four tunnel lines,
Vegas and the score — and no support or resistance at all. Asked 2026-08-19 to
keep "just the double tunnel and the resistance and support".

/strategy2 shares the same endpoint and exists to mirror the TV.pine
indicator, so the EMA stack cannot simply be deleted; the preset is what keeps
one endpoint serving both without a second one to drift.
"""
import app as APP


def _client():
    APP.app.config["TESTING"] = True
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def _bars(n=400, base=100.0, period=10):
    """A range that prints repeated highs and lows, so pivots exist.

    A SHORT period on purpose: with a 40-bar wave a 30-bar slice contained no
    complete swing, so the "too little history" test passed whether the guard
    was there or not.
    """
    out, t = [], 1_700_000_000_000
    for i in range(n):
        half = period / 2.0
        wave = abs((i % period) - half) - half / 2.0
        px = base + wave * (1.0 + (i // period % 5) * 0.35)
        out.append([t + i * 3_600_000, px, px + 0.4, px - 0.4, px, 1000.0])
    return out


def test_the_tunnel_preset_keeps_only_the_double_tunnel():
    keys = [d["key"] for d in APP.chart_ema_defs("tunnel")]
    assert keys == ["tia", "tib", "toa", "tob"], keys
    # Both PAIRS — "double tunnel" is inner and outer, not one of them.
    assert len(keys) == 4


def test_the_full_preset_is_untouched_for_the_pine_mirror():
    keys = [d["key"] for d in APP.chart_ema_defs("full")]
    assert keys[:4] == ["e20", "e50", "e100", "e200"]
    assert len(keys) == 8


def test_an_unknown_preset_falls_back_to_full_not_empty():
    """A typo in a query string must not silently erase the chart."""
    c = _client()
    d = c.get("/api/strategy2_ohlcv/BTC%2FUSDT%3AUSDT?lines=nonsense").get_json()
    assert len(d["ema_defs"]) == 8 and d["vegas"] is True


def test_levels_sit_on_both_sides_of_price_nearest_first():
    lv = APP.chart_levels(_bars())
    assert lv, "no levels found on a series built from repeated pivots"
    price = _bars()[-2][4]
    res = [x for x in lv if x["kind"] == "resistance"]
    sup = [x for x in lv if x["kind"] == "support"]
    assert all(x["price"] > price for x in res)
    assert all(x["price"] < price for x in sup)
    # Asked for MORE than the default, so the ordering has enough items to be
    # distinguishable — at two a side a wrong sort is right half the time.
    wide = APP.chart_levels(_bars(), max_each=4)
    for kind in ("resistance", "support"):
        side = [x for x in wide if x["kind"] == kind]
        if len(side) < 3:
            continue
        gaps = [abs(x["price"] - price) for x in side]
        assert gaps == sorted(gaps), f"{kind} levels are not nearest-first: {gaps}"
    assert any(len([x for x in wide if x["kind"] == k]) >= 3
               for k in ("resistance", "support")), \
        "the fixture is too poor in pivots to test the ordering at all"


def test_the_default_is_two_a_side():
    """Each line carries an axis label; past two a side they sit on top of the
    price scale's own readings and the chart stops being readable."""
    rich = _bars()
    lv = APP.chart_levels(rich)
    for kind in ("resistance", "support"):
        assert len([x for x in lv if x["kind"] == kind]) <= 2
    assert len(APP.chart_levels(rich, max_each=4)) > len(lv), \
        "max_each does nothing — the cap is not what limits the list"


def test_levels_abstain_on_too_little_history():
    """An empty list means 'no levels found'. Inventing one from 30 bars would
    put a line on the chart that too little history supports — and this
    fixture DOES contain pivots at 30 bars, so the guard is what stops it."""
    assert APP.chart_levels(_bars(30)) == []
    assert APP.chart_levels(_bars(30), max_each=4) == []
    assert APP.chart_levels([]) == []
    assert APP.chart_levels(None) == []


def test_levels_come_from_the_flip_detectors_own_pivots(monkeypatch):
    """Same source as the 壓力翻支撐 alerts. A second definition here is how
    the chart and the alert would start disagreeing about what resistance is."""
    import breakout_flip as B
    seen = []
    real = B.swing_highs
    monkeypatch.setattr(B, "swing_highs",
                        lambda *a, **k: (seen.append(1), real(*a, **k))[1])
    APP.chart_levels(_bars())
    assert seen, "chart_levels computed pivots without breakout_flip"


def test_the_payload_carries_levels_and_the_vegas_flag():
    c = _client()
    d = c.get("/api/strategy2_ohlcv/BTC%2FUSDT%3AUSDT?lines=tunnel").get_json()
    assert d["vegas"] is False, "Vegas is a third average on a cut-back chart"
    # Non-empty when candles arrived: a real market always has pivots, so an
    # empty list here means the payload dropped them rather than found none.
    assert isinstance(d["levels"], list)
    if d.get("candles"):
        assert d["levels"], "candles arrived but no levels were sent"
        assert {x["kind"] for x in d["levels"]} <= {"support", "resistance"}
    assert [x["key"] for x in d["ema_defs"]] == ["tia", "tib", "toa", "tob"]


def test_each_page_asks_for_the_stack_it_needs():
    with open("templates/coin.html", encoding="utf-8") as f:
        coin = f.read()
    with open("templates/strategy2.html", encoding="utf-8") as f:
        s2 = f.read()
    assert "lines: 'tunnel'" in coin, "幣種分析 lost its cut-back chart"
    assert "lines:" not in s2, "/strategy2 must keep the full .pine stack"
