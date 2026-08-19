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
    assert set(ctx) == {"macd_above", "macd_rising", "vol_mult"}


def test_volume_multiple_is_relative_to_the_symbols_own_average():
    quiet = BF.confirm_context(_bars(vol=1000.0, last_vol=1000.0))
    spike = BF.confirm_context(_bars(vol=1000.0, last_vol=9000.0))
    assert abs(quiet["vol_mult"] - 1.0) < 0.05
    assert spike["vol_mult"] > 8.0


def test_context_is_never_a_gate():
    """consider() must not consult it. Requiring these was measured at
    -0.293R +/-0.145 against the flips it would have rejected."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(BF.consider).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            cond = ast.dump(node.test).lower()
            for word in ("context", "macd", "vol_mult"):
                assert word not in cond, f"{word} became a flip gate"


def test_record_keeps_the_star_tier_and_the_readings():
    store = FO._blank()
    sig = {"symbol": "X/USDT:USDT", "base": "X", "ts": 1_700_000_000_000,
           "blue_sky": True, "room_pct": None, "zone_top": 1.02,
           "zone_bottom": 1.0, "touches": 3, "full_setup": True,
           "triangle_ts": 1_699_999_000.0,
           "context": {"macd_above": True, "macd_rising": False, "vol_mult": 4.2},
           "plan": {"entry": 1.05, "sl": 1.0, "tp": 1.15, "stop_pct": 4.8, "rr": 2.0}}
    assert FO.record(sig, store, now_ts=1_700_000_100.0) is True
    row = next(iter(store["open"].values()))
    assert row["full_setup"] is True, \
        "the ⭐ tier is unmeasurable if the record does not keep it"
    assert row["triangle_ts"] == 1_699_999_000.0
    assert row["context"]["vol_mult"] == 4.2


def test_the_alert_states_that_the_readings_do_not_help():
    sig = {"zone_top": 1.02, "zone_bottom": 1.0, "touches": 3, "blue_sky": True,
           "room_pct": None, "full_setup": False, "oi": {},
           "context": {"macd_above": True, "macd_rising": True, "vol_mult": 9.9}}
    msg = BF.format_alert("X/USDT:USDT", sig,
                          {"entry": 1.05, "sl": 1.0, "tp": 1.15,
                           "stop_pct": 4.8, "rr": 2.0})
    # Assert on the TABLE, not the message. An earlier version of this test
    # searched the whole string and passed with the rows removed, because the
    # caveat sentence below the table mentions MACD and 成交量 too — it could
    # not fail for the reason it was written.
    table = msg.split("<pre>")[1].split("</pre>")[0]
    assert "MACD" in table and "成交量" in table
    assert "9.9" in table, "the volume multiple itself must be shown"
    # An unqualified number in a setup alert reads as confirmation.
    assert "參考" in msg and "更差" in msg
