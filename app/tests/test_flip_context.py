"""MACD/volume on the flip alert: shown and stored, never required.

Measured 2026-08-19 on 348 replayable live flips — requiring all three is
significantly WORSE (-0.293R +/-0.145, CI excludes zero), so the one thing
these tests must pin down is that they never become a gate.

Also covers the recording gap found the same day: full_setup was printed in
the log and shown in the alert but never written to flip_outcomes, so the ⭐
tier had no live record and could not be checked at all.
"""
import breakout_flip as BF
import flip_outcomes as FO


def _bars(n=400, vol=1000.0, last_vol=None):
    out, px, t = [], 1.0, 1_700_000_000_000
    for i in range(n):
        px *= 1.001 if i % 3 else 0.999
        out.append([t + i * 900_000, px, px * 1.002, px * 0.998, px, vol])
    if last_vol is not None:
        out[-1][5] = last_vol
    return out


def test_context_abstains_instead_of_reporting_neutral():
    # Too little history is NOT "MACD flat, volume normal". {} says unmeasured.
    assert BF.confirm_context(_bars(60)) == {}
    ctx = BF.confirm_context(_bars())
    assert set(ctx) == {"macd_above", "macd_rising", "vol_bar_mult",
                        "vol_base_bars"}


def test_volume_multiple_is_relative_to_the_symbols_own_average():
    quiet = BF.confirm_context(_bars(vol=1000.0, last_vol=1000.0))
    spike = BF.confirm_context(_bars(vol=1000.0, last_vol=9000.0))
    assert abs(quiet["vol_bar_mult"] - 1.0) < 0.05
    assert spike["vol_bar_mult"] > 8.0


def test_macd_reading_is_correct_not_merely_present(monkeypatch):
    """Pin the VALUE, not the key set.

    The first version of this file asserted only that the keys existed and
    that the volume ratio scaled. Inverting the comparison, or shifting the
    histogram by one bar, shipped green — while the alert told the owner the
    opposite of what their chart shows.
    """
    import strategy4
    up = _bars(400)                       # closes drift up: fast EMA leads
    ctx = BF.confirm_context(up)
    closes = [float(c[4]) for c in up]
    macd, sigl = strategy4.macd_series(closes)
    hist = [a - b for a, b in zip(macd, sigl, strict=False)]
    assert ctx["macd_above"] is (macd[-1] > sigl[-1])
    assert ctx["macd_rising"] is (hist[-1] > hist[-2])
    # …and the reading must actually be able to come out the other way, or
    # every assertion above is satisfied by a function that returns a constant.
    # A late sharp drop, so the fast EMA is still falling away from the slow
    # one at the final bar. (A STEADY decline does not do this — MACD stops
    # falling and its lagging signal catches down through it, which is real
    # MACD behaviour and the reason this series is shaped the way it is.)
    down, px, t = [], 1.0, 1_700_000_000_000
    for i in range(400):
        px *= 1.0 if i < 392 else 0.985
        down.append([t + i * 900_000, px, px * 1.002, px * 0.998, px, 1000.0])
    up_sharp, px, t = [], 1.0, 1_700_000_000_000
    for i in range(400):
        px *= 1.0 if i < 392 else 1.015
        up_sharp.append([t + i * 900_000, px, px * 1.002, px * 0.998, px, 1000.0])
    falling = BF.confirm_context(down)
    rising = BF.confirm_context(up_sharp)
    assert falling["macd_above"] is False and falling["macd_rising"] is False
    assert rising["macd_above"] is True and rising["macd_rising"] is True


def test_context_reads_the_bar_it_is_asked_for(monkeypatch):
    """`at` exists so a replay cannot read the future. Without it, pairing
    detect(hist, at=i) with confirm_context(hist) would take MACD and volume
    from the newest bar in the whole series."""
    bars = _bars(400)
    bars[200][5] = 90_000.0               # a spike only visible at index 200
    early = BF.confirm_context(bars, at=200)
    late = BF.confirm_context(bars)
    assert early["vol_bar_mult"] > 8.0, "at= did not read the requested bar"
    assert late["vol_bar_mult"] < 3.0, "the default must read the last bar"


def test_context_is_never_a_gate():
    """Requiring these was measured at -0.293R +/-0.145 against the flips they
    would have rejected.

    Walks EVERY expression that can branch, not just If/While — the first
    version missed ternaries, comprehension filters and boolean guards, so
    `return out if ctx.get("macd_above") else {}` would have passed — and
    covers the whole call chain rather than consider() alone.
    """
    import ast
    import inspect
    banned = ("context", "macd", "vol_bar_mult", "vol_mult")
    for fn in (BF.consider, BF.detect, BF.plan):
        tree = ast.parse(inspect.getsource(fn).lstrip())
        for node in ast.walk(tree):
            tests = []
            if isinstance(node, (ast.If, ast.While, ast.IfExp)):
                tests = [node.test]
            elif isinstance(node, ast.comprehension):
                tests = list(node.ifs)
            elif isinstance(node, ast.BoolOp):
                tests = list(node.values)
            elif isinstance(node, ast.Assert):
                tests = [node.test]
            for t in tests:
                dump = ast.dump(t).lower()
                for word in banned:
                    assert word not in dump, \
                        f"{word} became a gate in {fn.__name__}()"


def test_record_keeps_the_star_tier_and_the_readings():
    store = FO._blank()
    sig = {"symbol": "X/USDT:USDT", "base": "X", "ts": 1_700_000_000_000,
           "blue_sky": True, "room_pct": None, "zone_top": 1.02,
           "zone_bottom": 1.0, "touches": 3, "full_setup": True,
           "triangle_ts": 1_699_999_000.0, "oi": {"oi_pct": 3.1},
           "context": {"macd_above": True, "macd_rising": False, "vol_bar_mult": 4.2},
           "plan": {"entry": 1.05, "sl": 1.0, "tp": 1.15, "stop_pct": 4.8, "rr": 2.0}}
    assert FO.record(sig, store, now_ts=1_700_000_100.0) is True
    row = next(iter(store["open"].values()))
    assert row["full_setup"] is True, \
        "the ⭐ tier is unmeasurable if the record does not keep it"
    assert row["triangle_ts_s"] == 1_699_999_000.0    # SECONDS; bar_ts is ms
    assert row["oi"] == {"oi_pct": 3.1}, "the OI reading must be stored too"
    assert row["context"]["vol_bar_mult"] == 4.2


def test_the_alert_states_that_the_readings_do_not_help():
    sig = {"zone_top": 1.02, "zone_bottom": 1.0, "touches": 3, "blue_sky": True,
           "room_pct": None, "full_setup": False, "oi": {},
           "context": {"macd_above": True, "macd_rising": True, "vol_bar_mult": 9.9}}
    msg = BF.format_alert("X/USDT:USDT", sig,
                          {"entry": 1.05, "sl": 1.0, "tp": 1.15,
                           "stop_pct": 4.8, "rr": 2.0})
    # Assert on the TABLE, not the message. An earlier version of this test
    # searched the whole string and passed with the rows removed, because the
    # caveat sentence below the table mentions MACD and 成交量 too — it could
    # not fail for the reason it was written.
    table = msg.split("<pre>")[1].split("</pre>")[0]
    assert "MACD" in table and "本根量" in table
    assert "9.9" in table, "the volume multiple itself must be shown"
    # An unqualified number in a setup alert reads as confirmation.
    assert "參考" in msg and "更差" in msg


def test_a_bad_candle_cannot_cost_the_alert():
    """The guard used to wrap only the MACD call, leaving the volume
    arithmetic exposed. One null volume raised out of confirm_context, out of
    consider(), and into the scanner's blanket handler — but consider() stamps
    the cooldown BEFORE building its result, so the alert was lost AND the
    symbol stayed suppressed for COOLDOWN_SEC. An annotation must never be
    able to cost the thing it annotates."""
    bars = _bars(400)
    bars[-1][5] = None                    # ccxt hands these back sometimes
    assert BF.confirm_context(bars) == {}     # abstains, does not raise
    bars2 = _bars(400)
    bars2[10] = bars2[10][:4]             # a short row
    assert BF.confirm_context(bars2) == {}


def test_full_setup_unknown_stays_unknown():
    """None means 'never evaluated' and must survive as None. bool() would
    write 'this was NOT a full setup' for a row nobody asked, and those rows
    would dilute the plain half of the very comparison the field enables."""
    store = FO._blank()
    sig = {"symbol": "Y/USDT:USDT", "base": "Y", "ts": 1_700_000_000_000,
           "blue_sky": True, "zone_top": 1.02, "zone_bottom": 1.0, "touches": 3,
           "plan": {"entry": 1.05, "sl": 1.0, "tp": 1.15, "stop_pct": 4.8, "rr": 2.0}}
    assert FO.record(sig, store, now_ts=1.0) is True
    row = next(iter(store["open"].values()))
    assert row["full_setup"] is None, "an unasked question was answered 'no'"

    # …and the tally must not file it on either side.
    import strategy4_outcomes as O
    st = {}
    O.accumulate(st, {"r": -1.0, "segment": "blue", "side": "long",
                      "full_setup": None, "outcome": "sl"})
    assert "star" not in st["tally"] and "plain" not in st["tally"]


def test_the_alert_leads_with_the_live_record_not_a_backtest():
    """The message recited a +0.08R backtest and a +0.12R re-test and never
    said the live forward book is negative — three neutral-to-positive
    headline numbers and no baseline."""
    sig = {"zone_top": 1.02, "zone_bottom": 1.0, "touches": 3, "blue_sky": True,
           "room_pct": None, "full_setup": False, "oi": {},
           "context": {"macd_above": True, "macd_rising": True, "vol_bar_mult": 9.9}}
    msg = BF.format_alert("X/USDT:USDT", sig,
                          {"entry": 1.05, "sl": 1.0, "tp": 1.15,
                           "stop_pct": 4.8, "rr": 2.0})
    live = f"{BF.MEASURED_LIVE['exp']:+.2f}R".replace("+", "")
    assert live in msg, "the live expectancy is not stated"
    assert "實盤" in msg and "虧" in msg
    # It must come BEFORE the backtest lines, or it reads as a footnote.
    assert msg.index("實盤紀錄") < msg.index(f"實測 {BF.MEASURED['n']} 筆")


def _alert(ctx, **sig_extra):
    sig = {"zone_top": 1.02, "zone_bottom": 1.0, "touches": 3, "blue_sky": True,
           "room_pct": None, "full_setup": False, "oi": {}, "context": ctx,
           **sig_extra}
    return BF.format_alert("X/USDT:USDT", sig,
                           {"entry": 1.05, "sl": 1.0, "tp": 1.15,
                            "stop_pct": 4.8, "rr": 2.0})


def test_a_partial_context_never_renders_an_unmade_reading():
    """The row was written with bare .get(), which cannot tell False from
    absent — so a context carrying only a volume printed
    'MACD 在訊號線下方・柱狀轉弱', an assertion about a measurement nobody made."""
    msg = _alert({"vol_bar_mult": 2.0})           # no MACD keys at all
    table = msg.split("<pre>")[1].split("</pre>")[0]
    assert "MACD" not in table, "an unmade MACD reading was rendered as fact"
    assert "本根量" in table                       # the reading that DOES exist
    # A half-context is still half-shown, not all-or-nothing.
    half = _alert({"macd_above": False})
    t2 = half.split("<pre>")[1].split("</pre>")[0]
    assert "在訊號線下方" in t2 and "柱狀" not in t2


def test_the_alert_says_which_bar_the_readings_describe():
    """detect() accepts a flip up to MAX_BARS_SINCE_FLIP bars old, so the
    numbers can belong to a bar an hour after the breakout — while the
    climax-bar reasoning in the caveat is about the breakout candle."""
    ctx = {"macd_above": True, "macd_rising": True, "vol_bar_mult": 9.9}
    stale = _alert(ctx, bars_since_flip=3)
    fresh = _alert(ctx, bars_since_flip=0)
    assert "距翻轉" in stale and "3" in stale
    assert "距翻轉" not in fresh, "no need to caveat the breakout bar itself"


def test_plain_flips_are_tallied_as_plain():
    """star/plain must BOTH be written, or the comparison has one arm."""
    import strategy4_outcomes as O
    st = {}
    O.accumulate(st, {"r": -1.0, "segment": "blue", "side": "long",
                      "full_setup": False, "outcome": "sl"})
    O.accumulate(st, {"r": 2.0, "segment": "blue", "side": "long",
                      "full_setup": True, "outcome": "tp"})
    assert st["tally"]["plain"]["n"] == 1 and st["tally"]["star"]["n"] == 1
