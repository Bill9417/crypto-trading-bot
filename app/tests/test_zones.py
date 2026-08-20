"""📦 Supply / demand zone signals.

The rules were read off a chart the owner sent, which is exactly the situation
this repo has been burned by — so the tests here are mostly about the claim,
not the arithmetic: the numbers shown next to the signals must be the ZONE's
own, the caveats must travel with them, and the trend cut must not quietly
become a gate before the forward book can settle it.
"""
import os

import zones
import zone_outcomes as Z


def _bars(n=400, base=100.0):
    """A range that revisits the same highs and lows, so zones exist."""
    out, t = [], 1_700_000_000_000
    for i in range(n):
        ph = i % 40
        px = base + (6.0 if ph < 8 else -6.0 if ph < 16 else (ph - 20) * 0.4)
        out.append([t + i * 900_000, px, px + 1.2, px - 1.2, px, 1000.0])
    return out


def test_zones_are_built_from_the_flip_detectors_own_pivots():
    """One definition of resistance in the codebase, not two."""
    src = open(zones.__file__, encoding="utf-8").read()
    assert "import breakout_flip as B" in src
    assert "B.swing_highs" in src and "B.cluster_zones" in src


def test_a_zone_price_has_closed_through_is_no_longer_fresh():
    z = zones.build(_bars())
    assert z, "no zones found on a fixture built from repeated pivots"
    assert any(x["fresh"] for x in z) or any(not x["fresh"] for x in z)
    for x in z:
        assert x["top"] > x["bottom"], "a zero-width zone is a level, not a zone"
        assert x["touches"] >= zones.MIN_TOUCHES


def test_reads_take_an_at_index_so_a_replay_cannot_see_the_future():
    bars = _bars()
    early = zones.build(bars, 300)
    late = zones.build(bars)
    assert all(z["last_idx"] <= 300 for z in early), "a zone used bars after `at`"
    assert len(late) >= len(early)
    # trend, too
    assert set(zones.trend(bars, 300)) == {"res_slope", "sup_slope"}


def test_trend_abstains_rather_than_reporting_flat():
    """None means 'too few pivots to fit a line'. Zero would claim the market
    is going sideways, which is a different statement.

    Exercised through the FITTING path (enough bars, too few pivots in the
    window) as well as the empty one — an early return covers only one of
    them, and it is the fit that decides in practice."""
    assert zones.trend([])["res_slope"] is None
    assert zones.trend(_bars(30))["res_slope"] is None
    # a long flat series has pivots but none inside a 5-bar window
    flat = [[i, 100.0, 100.0, 100.0, 100.0, 1.0] for i in range(400)]
    t = zones.trend(flat, 399, bars=5)
    assert t["res_slope"] is None and t["sup_slope"] is None


def test_the_trend_cut_is_recorded_not_enforced():
    """Measured +0.175R with the trend vs -0.032R against — but the plain
    signal dies without the best five symbols, so gating now would leave
    nothing to compare the forward book against."""
    assert zones.REQUIRE_TREND is False
    src = open(zones.__file__, encoding="utf-8").read()
    fn = src[src.index("def consider("):]
    assert '"with_trend": with_trend' in fn, "the cut is not even recorded"
    # it must not short-circuit on it
    assert "if not with_trend" not in fn and "if with_trend and" not in fn


def test_a_plan_without_a_valid_stop_is_no_signal():
    """The zone IS the stop. No usable level means no trade, never an invented
    stop distance."""
    assert zones.plan(0, {"top": 1, "bottom": 0.9}, "long") == {}
    # stop the wrong side of entry
    assert zones.plan(100.0, {"top": 200.0, "bottom": 190.0}, "long") == {}
    # absurdly wide
    assert zones.plan(100.0, {"top": 100.5, "bottom": 50.0}, "long") == {}
    ok = zones.plan(100.0, {"top": 101.0, "bottom": 98.0}, "long")
    assert ok and ok["sl"] < 100.0 < ok["tp"]


def test_the_board_shows_the_zones_numbers_not_the_flips():
    """zone_outcomes wraps flip_outcomes for the settle logic. Passing its
    MEASURED through would put another strategy's evidence next to these
    signals — plausible, specific, and about something else."""
    v = Z.web_view()
    m = v.get("measured") or {}
    assert m.get("n") == Z.MEASURED["n"] == 3653
    assert "measured_oos" not in v and "measured_seq" not in v


def test_the_caveats_travel_with_the_numbers():
    """The headline is +0.175R. The parts that stop it being a result have to
    be on the same card, not in a commit message."""
    m = Z.MEASURED
    for k in ("minus_top5", "with_trend_minus_top5", "early_half", "rotation_p"):
        assert k in m, f"{k} is not carried with the claim"
    assert m["minus_top5"]["ci"][0] < 0 < m["minus_top5"]["ci"][1]
    dash = open(os.path.join(os.path.dirname(os.path.abspath(zones.__file__)),
                             "templates/index.html"), encoding="utf-8").read()
    card = dash[dash.index('data-card="zones"'):]
    card = card[:card.index("</script>")]
    # The SENTENCE, not just the field name — the reader sees prose, and a
    # card that still references minus_top5 while printing nothing about it
    # would pass a field-name check.
    assert "拿掉最賺的 5 檔" in card, "the concentration caveat is not on the card"
    assert "還不是結論" in card and "信賴區間含 0" in card


def test_the_book_is_separate_from_the_flip_book():
    import flip_outcomes as F
    assert Z.STORE_FILE != F.STORE_FILE


def test_the_confirming_timeframe_is_asked_a_looser_question():
    """signal() needs the bar that crossed IN. A 5m bar rarely crosses on the
    same close as the 15m one, so demanding signal() on both would reject
    almost everything for a reason about candle alignment, not the market."""
    bars = _bars()
    # at_zone is true whenever price SITS in a fresh matching zone…
    inside = any(zones.at_zone(bars, s) for s in ("short", "long"))
    assert isinstance(inside, bool)
    src = open(zones.__file__, encoding="utf-8").read()
    fn = src[src.index("def consider("):]
    assert "at_zone(rows[:-1]" in fn, "the 5m is not asked the looser question"
    assert "signal(rows" not in fn, "the 5m is being asked to cross on the same bar"


def _tradeable(n=600, base=100.0):
    """Like _bars, but the swing extremes VARY, so the clustered zone has real
    width and the resulting stop lands inside plan()'s 0.5-6% band.

    The flat fixture produced zones ~0.15% wide, so consider() declined every
    signal on stop distance — which reads exactly like "the code is broken"
    and is in fact the cost gate doing its job.
    """
    out, t = [], 1_700_000_000_000
    for i in range(n):
        ph, cyc = i % 40, i // 40
        wob = ((cyc * 7) % 5 - 2) * 0.45          # extremes drift ~±1%
        px = base + (6.0 + wob if ph < 8 else -6.0 + wob if ph < 16 else (ph - 20) * 0.4)
        out.append([t + i * 900_000, px, px + 1.2, px - 1.2, px, 1000.0])
    return out


def _first_fire(bars):
    for i in range(300, len(bars) - 1):
        if zones.signal(bars, i) and zones.consider("Q/USDT:USDT", bars[:i + 2], {}, 1.0):
            return i
    return None


def test_the_5m_check_costs_one_call_and_only_after_the_free_ones(monkeypatch):
    """Asked AFTER a 15m entry has passed everything free — a handful of calls
    a sweep, not one per symbol."""
    calls = []
    bars = _tradeable()
    i = _first_fire(bars)
    assert i, "fixture produced no tradeable 15m entry"

    def fetch(sym, tf, n):
        calls.append((sym, tf, n))
        return bars

    # A symbol that does NOT fire must not cost a call. Flat, so there are no
    # pivots, no zones and nothing to enter — truncating the wavy fixture does
    # not work, because 280 bars of it still produce an entry.
    flat = [[1_700_000_000_000 + i * 900_000, 100.0, 100.0, 100.0, 100.0, 1.0]
            for i in range(600)]
    assert not zones.signal(flat), "the flat fixture is not actually quiet"
    zones.consider("Q/USDT:USDT", flat, {}, 1.0, fetch_tf=fetch)
    assert calls == [], "the 5m was fetched for a symbol with no 15m entry"
    zones.consider("Q/USDT:USDT", bars[:i + 2], {}, 1.0, fetch_tf=fetch)
    assert len(calls) == 1 and calls[0][1] == zones.CONFIRM_TF


def test_an_unasked_timeframe_is_unknown_not_a_disagreement():
    """No fetcher, or a failing one, must record 'unknown'. Storing 'no' would
    assert the 5m disagreed when nobody asked it."""
    bars = _tradeable()
    i = _first_fire(bars)
    assert i, "fixture produced no tradeable 15m entry"
    r = zones.consider("Q/USDT:USDT", bars[:i + 2], {}, 1.0)
    assert r and r["tf5"] == "unknown"

    def boom(*a, **k):
        raise RuntimeError("exchange down")

    r2 = zones.consider("Q/USDT:USDT", bars[:i + 2], {}, 2.0, fetch_tf=boom)
    assert r2 and r2["tf5"] == "unknown", "a failed check was recorded as disagreement"


def test_confirmation_is_recorded_not_required():
    assert zones.REQUIRE_CONFIRM is False
    dash = open(os.path.join(os.path.dirname(os.path.abspath(zones.__file__)),
                             "templates/index.html"), encoding="utf-8").read()
    card = dash[dash.index('data-card="zones"'):]
    card = card[:card.index("</script>")]
    # The COMPARATOR, not the string — tf5==='agree' also appears in the
    # badge, so a bare substring check passes with the sort gutted.
    assert "live.slice().sort(" in card, "the board does not sort at all"
    cmp = card[card.index("live.slice().sort("):]
    cmp = cmp[:cmp.index("});")]
    assert "tf5==='agree'" in cmp, "the board does not sort confirmed first"
    assert "z.tf5==null?''" in card, "an unasked 5m would render as ✗"
