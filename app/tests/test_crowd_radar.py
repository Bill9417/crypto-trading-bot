"""🐋 Crowd radar — is "massive" a real word here, or a flattering one?

The detector's entire claim rests on a percentile: this symbol's open interest
moved more than 98% of the 2h moves it has made in five days. Every test below
attacks one of the ways that sentence could be true and worthless — a flat
symbol whose noise is its own extreme, a distribution that contains the reading
it is supposed to judge, a rank quietly reported as a probability.

The other half is the four-state read. OI up with price up is CONVENTIONALLY
new longs, and getting that mapping backwards would label a short squeeze as a
long build — the most confident possible way to be exactly wrong.
"""
import pytest

import crowd_radar as C


def series(start, steps):
    """A series from a start value and a list of per-bar multipliers."""
    out, v = [start], start
    for s in steps:
        v *= (1 + s / 100.0)
        out.append(v)
    return out


def flat(n, v=1000.0):
    return [v] * n


# ── the four reads ───────────────────────────────────────────────────────────
def test_open_interest_rising_with_price_is_read_as_new_longs():
    assert C.oi_read(10.0, 2.0) == C.LONGS_OPENING


def test_open_interest_rising_against_price_is_read_as_new_shorts():
    """The one that matters most: OI building while price FALLS is shorts
    piling in. Read as longs, the radar would announce a long build at exactly
    the moment the crowd went short."""
    assert C.oi_read(10.0, -2.0) == C.SHORTS_OPENING


def test_open_interest_falling_with_price_rising_is_short_covering():
    """Positions leaving, price up — shorts buying back. This is NOT a long
    build: no new money took the long side, an old one gave up."""
    assert C.oi_read(-10.0, 2.0) == C.SHORTS_CLOSING


def test_open_interest_falling_with_price_is_longs_capitulating():
    assert C.oi_read(-10.0, -2.0) == C.LONGS_CLOSING


def test_only_the_two_building_states_are_new_money():
    assert set(C.BUILDING) == {C.LONGS_OPENING, C.SHORTS_OPENING}
    assert C.SHORTS_CLOSING not in C.BUILDING
    assert C.LONGS_CLOSING not in C.BUILDING


# ── the percentile ───────────────────────────────────────────────────────────
def test_a_quiet_symbol_cannot_be_massive_by_its_own_low_standards():
    """THE failure this floor exists for. A symbol whose OI drifts ±0.3% has a
    98th percentile of ~0.4%, so a 0.5% move ranks as extreme and is reported
    as 'massive' — true as a rank, worthless as an event. Percentile alone is
    self-calibrating in the wrong direction on a dead market."""
    oi = [1000 + (i % 3) * 2 for i in range(200)]      # ±0.2% forever
    oi.append(oi[-1] * 1.006)                          # +0.6%: huge FOR THIS SYMBOL
    px = flat(len(oi))
    a = C.assess(oi, px, span=8)
    assert a["pctile"] >= 98, "the setup is wrong — this should rank extreme"
    assert not a["massive"], "0.6% OI called massive because the symbol is asleep"
    assert "floor" in a["reason"]


def test_a_big_move_on_an_already_volatile_symbol_is_not_automatically_massive():
    """The mirror, and the reason a percentile is worth the trouble: on a
    symbol whose OI routinely swings 20% in two hours, a 6% build clears the
    absolute floor and is still an ordinary Tuesday. A fixed threshold set low
    enough to catch quiet majors would fire on this one constantly.

    The swing has to be SLOW — the distribution is built from 8-bar changes, so
    a series that alternates every bar nets out to nothing over the span and is
    a quiet symbol wearing a loud costume.
    """
    import math
    oi = [1000 * (1 + 0.25 * math.sin(2 * math.pi * i / 40)) for i in range(300)]
    tail = oi[-1]
    oi += [tail * (1 + 0.06) ** (k / 8) for k in range(1, 9)]   # +6% over 8 bars
    a = C.assess(oi, flat(len(oi)), span=8, min_oi_pct=5)
    assert 5.9 < a["oi_pct"] < 6.1, a          # the setup produced what it claims
    assert not a["massive"], "6% called massive on a symbol that lives at 20%"


def test_the_current_move_is_not_part_of_the_distribution_it_is_judged_against():
    """Left in, an extreme reading helps define the bar it has to clear — it
    raises its own threshold and ranks itself lower than it really is."""
    oi = flat(200) + [1000.0 * 1.5]
    dist_with = C.pct_changes(oi, 8)
    dist_without = C.pct_changes(oi[:-1], 8)
    assert len(dist_with) > len(dist_without)
    assert max(dist_with) > 40 and (not dist_without or max(dist_without) < 1), \
        "the spike is in the reference set"


def test_the_rank_is_reported_with_the_sample_it_came_from():
    """A percentile with no n is a number pretending to be evidence. The alert
    prints '前 X% (N 筆樣本)' and this is where N comes from."""
    oi = series(1000, [0.1] * 250) + [1000 * 3]
    a = C.assess(oi, flat(len(oi)), span=8)
    assert a["samples"] > 100
    assert a["samples"] == len(C.pct_changes(oi[:-1], 8))


def test_an_unwind_ranks_as_extreme_as_a_build():
    """Magnitude, not signed value. Ranked signed, every OI collapse would sit
    at the bottom of the distribution and the radar would be blind to half of
    what it exists to catch — and the unwind is often the louder event."""
    dist = [1.0, -1.0, 2.0, -2.0, 0.5]
    assert C.percentile_of(dist, -30.0) == 100.0
    assert C.percentile_of(dist, -30.0) == C.percentile_of(dist, 30.0)


def test_percentile_of_an_empty_distribution_is_not_an_endorsement():
    """A brand-new listing has no history. Returning 100 ('more extreme than
    everything I know') would make every new symbol alert immediately."""
    assert C.percentile_of([], 50.0) == 0.0


def test_a_symbol_without_enough_history_is_skipped_not_guessed():
    a = C.assess([1, 2, 3], [1, 2, 3], span=8)
    assert not a["massive"] and a["reason"] == "not enough history"


def test_a_zero_baseline_cannot_divide():
    a = C.assess([0] * 20, [1] * 20, span=8)
    assert not a["massive"]


def test_a_genuine_build_on_a_normal_symbol_does_fire():
    """The radar has to actually work: ordinary drift for five days, then a
    real 12% OI build with price up = crowded longs."""
    oi = [1000 * (1 + 0.0008 * (i % 7 - 3)) for i in range(300)]
    oi += [oi[-1] * (1 + 0.015) ** k for k in range(1, 9)]     # ~+12.6% over 8 bars
    px = flat(len(oi) - 1) + [1010.0]
    px = flat(len(oi))
    px[-1] = px[-9] * 1.03
    a = C.assess(oi, px, span=8)
    assert a["massive"], a
    assert a["state"] == C.LONGS_OPENING
    assert a["oi_pct"] > 10


# ── cooldown ─────────────────────────────────────────────────────────────────
def test_the_same_pile_up_does_not_alert_every_sweep():
    store = C._blank()
    assert C.should_alert(store, "SOLUSDT", C.LONGS_OPENING, 12.0, 1000.0)
    store["last"]["SOLUSDT:longs_opening"] = {"ts": 1000.0, "oi_pct": 12.0}
    assert not C.should_alert(store, "SOLUSDT", C.LONGS_OPENING, 13.0, 1500.0)


def test_a_materially_bigger_pile_up_breaks_the_cooldown():
    """A build going from 12% to 30% inside two hours is a different event, and
    silence there is the expensive kind."""
    store = C._blank()
    store["last"]["SOLUSDT:longs_opening"] = {"ts": 1000.0, "oi_pct": 12.0}
    assert C.should_alert(store, "SOLUSDT", C.LONGS_OPENING, 30.0, 1500.0)


def test_a_flip_to_the_other_side_is_a_new_event_not_a_repeat():
    """Cooldown is keyed on symbol AND direction. Keyed on symbol alone, a
    long build that flips to a short build would be swallowed as a duplicate —
    and the flip is usually the more interesting half."""
    store = C._blank()
    store["last"]["SOLUSDT:longs_opening"] = {"ts": 1000.0, "oi_pct": 12.0}
    assert C.should_alert(store, "SOLUSDT", C.SHORTS_OPENING, 11.0, 1100.0)


def test_the_cooldown_expires():
    store = C._blank()
    store["last"]["SOLUSDT:longs_opening"] = {"ts": 1000.0, "oi_pct": 12.0}
    assert C.should_alert(store, "SOLUSDT", C.LONGS_OPENING, 11.0,
                          1000.0 + C.COOLDOWN_SEC + 1)


# ── the message ──────────────────────────────────────────────────────────────
def _row(**kw):
    base = {"symbol": "SOLUSDT", "state": C.LONGS_OPENING, "oi_pct": 18.4,
            "px_pct": 3.1, "pctile": 99.2, "samples": 480, "turnover": 4.12e8,
            "span_h": 2.0, "ratio": 2.84, "ratio_pctile": 96.0}
    return {**base, **kw}


def test_the_alert_says_it_is_positioning_and_not_a_signal():
    """This repo has measured 48 high-win-rate setups and found 46 losing. A
    crowding alert reads like a trade idea, has never been scored against
    forward returns here, and must say so in the message rather than in a
    docstring nobody opens."""
    msg = C.build_alert(_row())
    assert "不是進場訊號" in msg
    assert "從未驗證" in msg


def test_the_alert_shows_the_rank_and_its_sample_size():
    msg = C.build_alert(_row())
    assert "480" in msg, "the rank is quoted with no sample behind it"
    assert "罕見度" in msg


def test_the_alert_never_calls_the_rank_a_probability():
    """'前 0.8%' is a description of five days. 'p=0.008' would be a claim
    about likelihood that overlapping windows cannot support."""
    msg = C.build_alert(_row())
    for forbidden in ("機率", "p=", "p<", "顯著"):
        assert forbidden not in msg, f"the rank is being sold as inference: {forbidden}"


def test_a_short_build_is_not_headlined_as_a_long_build():
    msg = C.build_alert(_row(state=C.SHORTS_OPENING, px_pct=-3.1))
    assert "做空" in msg and "大量做多堆積" not in msg
    assert "軋空" in msg


def test_an_unwind_is_not_described_as_new_money():
    msg = C.build_alert(_row(state=C.SHORTS_CLOSING))
    assert "回補" in msg
    assert "新資金" in msg          # "...不是新資金進場"


def test_the_alert_survives_a_missing_long_short_ratio():
    """The ratio is a second call that is allowed to fail. If its absence
    raised, one flaky endpoint would silence the whole radar."""
    row = _row()
    row.pop("ratio")
    row.pop("ratio_pctile")
    msg = C.build_alert(row)
    assert "未平倉" in msg and "多空比" not in msg


# ── the sweep ────────────────────────────────────────────────────────────────
class _Fake:
    """Stands in for Binance. One symbol piles in, one is asleep."""

    def __init__(self):
        self.ratio_calls = []

    def oi_history(self, sym, **kw):
        if sym == "HOTUSDT":
            oi = [1000 * (1 + 0.0008 * (i % 7 - 3)) for i in range(300)]
            # A SHORT, sharp spike, not a long ramp. Spread over the full
            # 8-bar span, every trailing window contains most of the ramp, so
            # the reading barely exceeds its own reference distribution and
            # lands at p99.3 — an artefact of the fixture, not of the market.
            # Two bars keeps the historical windows clean and the rank honest.
            oi += [oi[-1] * 1.09, oi[-1] * 1.09 * 1.09]
            px = flat(len(oi))
            px[-1] = px[-9] * 1.04
            return oi, px
        return flat(300), flat(300)

    def ls_ratio(self, sym, **kw):
        self.ratio_calls.append(sym)
        return [1.0] * 400 + [2.9]

    def live_oi(self, sym):
        """No-op live reading: returns the symbol's own last bar, so splicing
        it changes nothing and the fixtures keep asserting about the series
        they actually define."""
        return self.oi_history(sym)[0][-1]


@pytest.fixture
def fake(monkeypatch):
    f = _Fake()
    monkeypatch.setattr(C, "oi_history", f.oi_history)
    monkeypatch.setattr(C, "ls_ratio", f.ls_ratio)
    # live_oi MUST be stubbed too. Left real, these tests hit Binance — and
    # "HOTUSDT" is a genuine symbol (Holo), so the call SUCCEEDED and spliced
    # real open interest of ~10^9 onto a fixture series of ~1000, producing a
    # fake build large enough to break its own cooldown. A test that reaches
    # the network does not fail honestly, it fails as whatever the market is
    # doing that minute.
    monkeypatch.setattr(C, "live_oi", f.live_oi)
    monkeypatch.setattr(C, "PACE_SEC", 0)
    return f


def test_the_sweep_finds_the_pile_up_and_ignores_the_quiet_symbol(fake):
    store = C._blank()
    sent = []
    out = C.scan([{"symbol": "HOTUSDT", "turnover": 5e8},
                  {"symbol": "COLDUSDT", "turnover": 5e8}],
                 now=1000.0, store=store, send=sent.append)
    assert out["checked"] == 2
    assert [h["symbol"] for h in out["hits"]] == ["HOTUSDT"]
    assert len(sent) == 1 and "HOT" in sent[0]


def test_the_second_call_is_only_spent_on_a_symbol_that_already_passed(fake):
    """The long/short ratio costs a request per symbol. Fetched for everything
    it would triple the sweep's cost to decorate alerts that never fire."""
    C.scan([{"symbol": "HOTUSDT", "turnover": 5e8},
            {"symbol": "COLDUSDT", "turnover": 5e8}],
           now=1000.0, store=C._blank(), send=None)
    assert fake.ratio_calls == ["HOTUSDT"]


def test_one_dead_symbol_does_not_stop_the_sweep(monkeypatch, fake):
    def boom(sym, **kw):
        if sym == "DEADUSDT":
            raise RuntimeError("delisted")
        return fake.oi_history(sym, **kw)

    monkeypatch.setattr(C, "oi_history", boom)
    out = C.scan([{"symbol": "DEADUSDT", "turnover": 5e8},
                  {"symbol": "HOTUSDT", "turnover": 5e8}],
                 now=1000.0, store=C._blank(), send=None)
    assert out["errors"] == 1
    assert [h["symbol"] for h in out["hits"]] == ["HOTUSDT"]


def test_a_suppressed_hit_is_still_recorded_but_not_sent(fake):
    """The web page should show that the pile-up is still there even while
    Telegram is deliberately quiet about it."""
    store = C._blank()
    store["last"]["HOTUSDT:longs_opening"] = {"ts": 1000.0, "oi_pct": 20.0}
    sent = []
    out = C.scan([{"symbol": "HOTUSDT", "turnover": 5e8}],
                 now=1001.0, store=store, send=sent.append)
    assert out["hits"] and out["hits"][0]["suppressed"] == "cooldown"
    assert out["alerted"] == 0
    assert sent == []


def test_a_broken_universe_call_returns_instead_of_raising(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("binance down")

    monkeypatch.setattr(C, "universe", boom)
    out = C.scan(now=1000.0, store=C._blank(), send=None)
    assert "error" in out and out["checked"] == 0


# ── where it is allowed to speak ────────────────────────────────────────────
def test_crowding_alerts_carry_no_account_data():
    """The 清算 topic is a JOINABLE group topic. Positioning is public market
    data and belongs there; balances and position sizes never do."""
    msg = C.build_alert(_row())
    for leak in ("餘額", "保證金", "USDT 可用", "槓桿", "口數"):
        assert leak not in msg


# ── stock perps are not the crowd ───────────────────────────────────────────
def test_stock_perps_are_kept_out_of_the_universe(monkeypatch):
    """The first live sweep returned six hits and FIVE were stock perps —
    TSLA, GOOGL, SNDK, SKHY, AAOI. They step at the US open and flatline all
    weekend, so the calendar alone ranks them extreme every Monday, and the
    weekend dead zone drags the reference distribution down while it does it.

    The original filter guessed from the ticker string and matched nothing.
    """
    tickers = [{"symbol": "BTCUSDT", "quoteVolume": "9e9"},
               {"symbol": "TSLAUSDT", "quoteVolume": "9e9"},
               {"symbol": "GOOGLUSDT", "quoteVolume": "9e9"},
               {"symbol": "SOLUSDT", "quoteVolume": "9e9"}]
    info = {"symbols": [
        {"symbol": "BTCUSDT", "underlyingType": "COIN", "contractType": "PERPETUAL"},
        {"symbol": "TSLAUSDT", "underlyingType": "EQUITY",
         "contractType": "TRADIFI_PERPETUAL"},
        {"symbol": "GOOGLUSDT", "underlyingType": "EQUITY",
         "contractType": "TRADIFI_PERPETUAL"},
        {"symbol": "SOLUSDT", "underlyingType": "COIN", "contractType": "PERPETUAL"}]}

    monkeypatch.setattr(C, "_get", lambda path, params: (
        tickers if "ticker" in path else info))
    monkeypatch.setattr(C, "_tradfi_cache", {"ts": 0.0, "symbols": frozenset()})
    got = [u["symbol"] for u in C.universe()]
    assert got == ["BTCUSDT", "SOLUSDT"], got


def test_the_stock_filter_reuses_the_definition_it_shares_with_the_executor():
    """One definition of 'is this a stock', not two that drift. The raw
    exchangeInfo rows here are exactly the `info` payload market_data reads."""
    from market_data import is_tradfi_market
    assert is_tradfi_market({"info": {"underlyingType": "EQUITY"}})
    assert is_tradfi_market({"info": {"contractType": "TRADIFI_PERPETUAL"}})
    assert not is_tradfi_market({"info": {"underlyingType": "COIN",
                                          "contractType": "PERPETUAL"}})


def test_the_sweep_refuses_to_run_unfiltered_if_it_cannot_tell_stocks_apart(monkeypatch):
    """Fail closed. If exchangeInfo is unreachable the honest outcome is no
    sweep — an unfiltered one is the exact failure the guard exists for."""
    def only_tickers(path, params):
        if "ticker" in path:
            return [{"symbol": "BTCUSDT", "quoteVolume": "9e9"}]
        raise RuntimeError("exchangeInfo down")

    monkeypatch.setattr(C, "_get", only_tickers)
    monkeypatch.setattr(C, "_tradfi_cache", {"ts": 0.0, "symbols": frozenset()})
    with pytest.raises(RuntimeError, match="TradFi"):
        C.universe()
    # and scan() turns that into a reported error rather than a crash
    out = C.scan(now=1000.0, store=C._blank(), send=None)
    assert "error" in out


# ── instant: the live reading (added 2026-08-12) ────────────────────────────
def test_the_live_reading_replaces_the_last_closed_bar():
    """openInterestHist is binned, so a build starting at :01 is invisible
    until :15. The live value is what removes that."""
    oi, px = C.with_live([100.0, 101.0], [10.0, 10.0], 130.0)
    assert oi == [100.0, 101.0, 130.0]


def test_price_does_not_take_the_live_value():
    """The price here is IMPLIED from the OI notional. Splicing a live
    last-traded price onto an implied series puts a ~0.3% basis error into a
    comparison whose only job is to decide a sign — a 2h move reading +0.1% on
    one basis and −0.2% on the other flips the long/short read on rounding."""
    oi, px = C.with_live([100.0, 101.0], [10.0, 12.0], 130.0)
    assert px == [10.0, 12.0, 12.0], "the price series grew a foreign basis"
    assert len(px) == len(oi), "series must stay aligned"


def test_a_live_build_is_caught_before_the_bar_closes():
    """End to end: five quiet days of closed bars, and a spike that exists
    ONLY in the live reading. Without with_live this is invisible."""
    oi = [1000 * (1 + 0.0008 * (i % 7 - 3)) for i in range(300)]
    px = flat(len(oi))
    assert not C.assess(oi, px, span=8)["massive"], "the closed bars are quiet"
    loi, lpx = C.with_live(oi, px, oi[-1] * 1.14)
    a = C.assess(loi, lpx, span=8)
    assert a["massive"] and a["oi_pct"] > 12, a


def test_a_dead_live_endpoint_falls_back_to_the_bars(monkeypatch, fake):
    """Freshness is allowed to fail; correctness is not. A 500 on the live call
    must cost 15 minutes of latency, never the whole sweep."""
    def boom(sym):
        raise RuntimeError("openInterest 500")

    monkeypatch.setattr(C, "live_oi", boom)
    out = C.scan([{"symbol": "HOTUSDT", "turnover": 5e8}],
                 now=1000.0, store=C._blank(), send=None)
    assert out["checked"] == 1 and out["errors"] == 0
    assert [h["symbol"] for h in out["hits"]] == ["HOTUSDT"]


def test_with_live_on_an_empty_series_does_not_invent_one():
    assert C.with_live([], [], 100.0) == ([], [])


def test_the_radar_runs_every_sweep_not_every_third_one():
    """The S2 sweep is ~5 min. A self-pace above that would throttle the radar
    back to exactly the bin lag the live call was added to remove."""
    assert C.RUN_EVERY_SEC <= 300, C.RUN_EVERY_SEC


# ── small caps are the point (2026-08-12) ───────────────────────────────────
def test_the_universe_reaches_small_caps():
    """A $50M floor keeps only ~45 majors, which is where OI anomalies matter
    LEAST — a 6% build on BTC is a rounding error on a $10B book. The whole
    request was the small-cap tier."""
    assert C.MIN_TURNOVER <= 5_000_000, C.MIN_TURNOVER
    assert C.TOP_N >= 250, C.TOP_N


def test_the_floor_is_not_zero():
    """On a genuinely dead book one position IS the open interest, so a
    crowding read becomes a whale read — which is a different module."""
    assert C.MIN_TURNOVER > 0


def test_size_is_reported_rather_than_used_to_exclude():
    """The thin-book caveat is handled by putting the money on screen, not by
    dropping the symbol and pretending nothing happened there."""
    assert C.tier_of(9e8) == "大型"
    assert C.tier_of(2e8) == "中型"
    assert C.tier_of(3e6) == "小型"
    msg = C.build_alert(_row(notional=3_100_000, tier="小型"))
    assert "未平倉額" in msg and "小型" in msg and "$3.1M" in msg


def test_a_small_cap_hit_carries_its_notional(fake):
    out = C.scan([{"symbol": "HOTUSDT", "turnover": 3e6}],
                 now=1000.0, store=C._blank(), send=None)
    hit = out["hits"][0]
    assert hit["tier"] == "小型"
    assert hit["notional"] > 0


# ── the alert budget at 301 symbols ─────────────────────────────────────────
def _many(n, pctiles):
    return [{"symbol": f"S{i}USDT", "turnover": 5e6} for i in range(n)]


def test_the_dashboard_shows_more_than_telegram_sends():
    """A 98th-percentile gate over 301 symbols is ~6 hits EVERY sweep — 72
    Telegram messages an hour. The board wants all of them; the phone does
    not. Two gates, one purpose each."""
    assert C.ALERT_PCTILE > C.PCTILE_GATE
    assert C.MAX_ALERTS_PER_SWEEP <= 5


def test_only_the_loudest_few_are_sent_and_the_rest_are_still_recorded(monkeypatch):
    """The cap must not become a silent filter: everything found stays on the
    board and in the return value, with a reason for the silence."""
    rows = []
    for i in range(8):
        oi = [1000 * (1 + 0.0008 * (i % 7 - 3)) for i in range(300)]
        oi += [oi[-1] * (1 + 0.02) ** k for k in range(1, 9)]
        rows.append(oi)

    def hist(sym, **kw):
        idx = int(sym[1:-4])
        oi = rows[idx]
        px = flat(len(oi))
        px[-1] = px[-9] * 1.04
        return oi, px

    monkeypatch.setattr(C, "oi_history", hist)
    monkeypatch.setattr(C, "live_oi", lambda s: hist(s)[0][-1])
    monkeypatch.setattr(C, "ls_ratio", lambda s, **k: [1.0] * 400 + [2.0])
    monkeypatch.setattr(C, "PACE_SEC", 0)

    sent = []
    store = C._blank()
    out = C.scan([{"symbol": f"S{i}USDT", "turnover": 5e6} for i in range(8)],
                 now=1000.0, store=store, send=sent.append)
    assert len(out["hits"]) == 8, "findings were dropped, not just silenced"
    assert len(sent) <= C.MAX_ALERTS_PER_SWEEP
    assert len(store["recent"]) == 8, "the board lost what Telegram skipped"
    quiet = [h for h in out["hits"] if h.get("quiet")]
    assert len(quiet) == 8 - len(sent)
    assert all(q["quiet"] for q in quiet), "silenced with no reason recorded"


def test_the_sweep_reports_what_it_held_back():
    """A cap that reported only what it sent would read as 'nothing else was
    happening'."""
    import inspect
    src = inspect.getsource(C.scan)
    assert "held_back" in src and "shown" in src


# ── the request budget ──────────────────────────────────────────────────────
def test_the_live_call_is_skipped_where_it_cannot_change_the_answer():
    """At 301 symbols the live reading is half the request budget. On a symbol
    whose closed bars sit at +0.4% it cannot move anything across the gate."""
    assert not C._worth_a_live_call({"oi_pct": 0.4, "pctile": 30, "massive": False})


def test_the_live_call_is_spent_on_anything_near_the_gate():
    assert C._worth_a_live_call({"oi_pct": 4.0, "pctile": 80, "massive": False})
    assert C._worth_a_live_call({"oi_pct": 1.0, "pctile": 96, "massive": False})
    assert C._worth_a_live_call({"massive": True, "oi_pct": 9.0, "pctile": 99})


def test_a_symbol_already_over_the_gate_still_gets_a_fresh_reading():
    """Skipping the live call there would alert on a stale number — the one
    place freshness is most visible."""
    assert C._worth_a_live_call({"massive": True, "oi_pct": 20.0, "pctile": 99.9})
