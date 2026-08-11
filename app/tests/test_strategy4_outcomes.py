"""Did the S4 setup work? — the tracker's arithmetic and its honesty.

S4 places no orders, so there are no exchange fills to reconcile against later.
If a result is not written down when it happens it is gone: strategy4_signals
.json is OVERWRITTEN by every scan, which is why the docstring's promise that
"the scan records every signal it fires" was not true until this module existed.

The tests that matter are the ones about which way the code guesses when the
data cannot decide, because those are the assumptions that quietly determine
whether a strategy looks profitable.
"""
import time

import pytest

import strategy4_outcomes as O

BAR = 1_000_000_000_000          # signal bar timestamp, ms
M15 = 15 * 60 * 1000
NOW = BAR / 1000 + 10 * 3600     # 10h after the signal — inside the window


def trade(**kw):
    base = {"key": "X:1", "symbol": "X/USDT:USDT", "base": "X", "segment": "crypto",
            "entry": 100.0, "sl": 95.0, "tp": 110.0, "bar_ts": BAR}
    return {**base, **kw}


def candle(i, high, low, close=None):
    return [BAR + i * M15, 100.0, high, low, close if close is not None else low, 0]


# ── the tie-break that decides everything ───────────────────────────────────
def test_a_candle_spanning_both_stop_and_target_is_scored_as_a_stop():
    """THE assumption. A 15m range that contains both levels says nothing about
    which was touched first, and guessing the target would be flattering AND
    unfalsifiable — the reading that makes the strategy look best is exactly
    the one the data cannot support.

    Biased toward the stop, real results can only beat the record, never come
    in under it. That is the direction an honest bias must point.
    """
    r = O.settle(trade(), [candle(1, high=112, low=94)], NOW)
    assert r["outcome"] == "sl"
    assert r["r"] == -1.0


def test_the_signal_bar_cannot_fill_its_own_trade():
    """The setup is read at that bar's CLOSE, so its high and low already
    happened. Counting them would let a signal be stopped out by a wick that
    printed before anyone could have acted on it — the same lookahead that made
    a TradingView strategy in this repo show 78% win rate and port at ~22%."""
    huge = [BAR, 100.0, 120.0, 90.0, 100.0, 0]      # spans SL and TP, at bar_ts
    assert O.settle(trade(), [huge], NOW) == {}


def test_a_clean_target_and_a_clean_stop_score_as_expected():
    assert O.settle(trade(), [candle(1, 105, 99), candle(2, 111, 103)], NOW)["r"] == 2.0
    assert O.settle(trade(), [candle(1, 101, 97), candle(2, 99, 94)], NOW)["r"] == -1.0


def test_an_unresolved_trade_stays_open():
    assert O.settle(trade(), [candle(1, 104, 98)], NOW) == {}


# ── expiry ──────────────────────────────────────────────────────────────────
def test_an_expired_trade_is_marked_to_market_not_scored_zero():
    """Calling it 0R flatters anything that dawdles; dropping it deletes
    exactly the trades that went nowhere, which is survivorship bias applied
    to your own record."""
    late = BAR / 1000 + 60 * 3600
    r = O.settle(trade(), [candle(1, 104, 98, close=102)], late)
    assert r["outcome"] == "expired"
    assert r["r"] == pytest.approx(0.4)          # (102 − 100) / 5R risk


def test_expiry_only_applies_after_the_window():
    early = BAR / 1000 + 3600
    assert O.settle(trade(), [candle(1, 104, 98, close=102)], early) == {}


def test_mae_and_mfe_are_recorded_in_r():
    r = O.settle(trade(), [candle(1, 108, 98, close=104), candle(2, 111, 103)], NOW)
    assert r["mae"] == pytest.approx(-0.4)       # low 98  → −2 / 5R
    assert r["mfe"] == pytest.approx(2.2)        # high 111 → +11 / 5R


# ── recording ───────────────────────────────────────────────────────────────
def _sig(symbol="A/USDT:USDT", bar=BAR, entry=100.0, sl=95.0, tp=110.0, seg="crypto"):
    # Mirrors strategy4.plan()'s full shape — tp_pct included, because
    # format_signal reads it and a fixture missing it fails inside the alert
    # rather than in the code under test.
    return {"symbol": symbol, "base": symbol.split("/")[0], "segment": seg,
            "bar_ts": bar, "quality": 60, "score": 80, "price": entry,
            "slope": 1.0, "div_ago": 3, "div_sources": ["MACD"], "oi_state": 1,
            "plan": {"entry": entry, "sl": sl, "tp": tp, "rr": 2.0,
                     "stop_pct": (entry - sl) / entry * 100,
                     "tp_pct": (tp - entry) / entry * 100}}


def test_the_same_signal_is_never_recorded_twice():
    """A setup that stays valid reappears in consecutive scans. strategy4.tick
    passes the ALERTED set, which is already cooldown-deduped, but the store
    must not depend on that — double counting inflates n and every statistic
    derived from it."""
    store = O._blank()
    assert O.record([_sig()], store, NOW) == 1
    assert O.record([_sig()], store, NOW) == 0
    assert len(store["open"]) == 1


def test_a_settled_signal_is_not_reopened_by_a_later_scan():
    store = O._blank()
    O.record([_sig()], store, NOW)
    t = store["open"].pop("A/USDT:USDT:%d" % BAR)
    store["closed"].append({**t, "outcome": "sl", "r": -1.0})
    assert O.record([_sig()], store, NOW) == 0, "a closed trade was reopened"


def test_an_unusable_plan_is_skipped_rather_than_stored_broken():
    store = O._blank()
    assert O.record([_sig(sl=105.0)], store, NOW) == 0      # stop above entry
    assert O.record([_sig(tp=90.0)], store, NOW) == 0       # target below entry
    assert O.record([{"symbol": "B", "bar_ts": 1}], store, NOW) == 0   # no plan
    assert store["open"] == {}


# ── the tally ───────────────────────────────────────────────────────────────
def test_the_tally_records_variance_from_the_very_first_trade():
    """/reality has to fall back on a lower-bound interval because S2's totals
    never stored a sum of squares and the per-trade R values are long pruned.
    This starts clean, so it must not repeat that mistake."""
    store = O._blank()
    for r in (2.0, -1.0):
        O.accumulate(store, {"segment": "crypto", "outcome": "tp" if r > 0 else "sl", "r": r})
    b = store["tally"]["all"]
    assert b["n"] == 2 and b["sumsq"] == 5.0 and b["sumsq_n"] == 2

    import reality
    assert reality.ci(b)[2] == "exact", "S4 should never need the lower bound"


def test_the_tally_splits_by_segment():
    store = O._blank()
    O.accumulate(store, {"segment": "crypto", "outcome": "tp", "r": 2.0})
    O.accumulate(store, {"segment": "tradfi", "outcome": "sl", "r": -1.0})
    assert store["tally"]["all"]["n"] == 2
    assert store["tally"]["crypto"]["n"] == 1
    assert store["tally"]["tradfi"]["n"] == 1
    assert store["tally"]["crypto"]["tp"] == 1
    assert store["tally"]["tradfi"]["sl"] == 1


def test_the_tally_outlives_the_pruned_record_list():
    """closed[] is capped; the tally is not. A strategy whose long-run
    expectancy disappears with its oldest records has no long-run expectancy."""
    store = O._blank()
    for i in range(O.KEEP_CLOSED + 25):
        c = {"segment": "crypto", "outcome": "sl", "r": -1.0, "key": f"k{i}"}
        store["closed"].append(c)
        O.accumulate(store, c)
    store["closed"] = store["closed"][-O.KEEP_CLOSED:]
    assert len(store["closed"]) == O.KEEP_CLOSED
    assert store["tally"]["all"]["n"] == O.KEEP_CLOSED + 25


# ── the read ────────────────────────────────────────────────────────────────
def test_stats_refuse_a_verdict_on_a_tiny_sample():
    """Seven trades at 57% and +3.4R is exactly the shape that has fooled this
    repo before. It must not read as an edge."""
    store = O._blank()
    for r in (2.0, 2.0, 2.0, -1.0, -1.0, -1.0, 0.42):
        O.accumulate(store, {"segment": "crypto", "outcome": "tp" if r > 0 else "sl", "r": r})
    st = O.stats(store)
    assert st["exp"] > 0 and st["net"] > 0          # looks good ...
    assert st["tag"] == "too few"                   # ... and says so anyway
    assert st["straddles_zero"] is True
    assert "樣本太少" in st["verdict_zh"]


def test_hit_rate_ignores_expired_trades():
    """命中率 answers "when it resolved, how often was it the target?" — an
    expired trade resolved as neither, and counting it as a miss would blame
    the entry for a timeout."""
    store = O._blank()
    O.accumulate(store, {"segment": "crypto", "outcome": "tp", "r": 2.0})
    O.accumulate(store, {"segment": "crypto", "outcome": "sl", "r": -1.0})
    O.accumulate(store, {"segment": "crypto", "outcome": "expired", "r": 0.1})
    assert O.stats(store)["hit_rate"] == pytest.approx(50.0)


def test_every_verdict_tag_has_chinese():
    """/s4 is a Chinese page. A reworded English verdict must not silently
    fall through to English here — the map is keyed on the short tag."""
    import reality
    for exp, straddle, basis in ((-0.5, False, "exact"), (0.5, False, "exact"),
                                 (-0.5, False, "floor"), (0.5, False, "floor"),
                                 (0.01, True, "exact")):
        tag = reality.tag({"n": 100, "exp": exp, "straddles_zero": straddle,
                           "basis": basis})
        assert tag in O.VERDICT_ZH, f"no Chinese for tag {tag!r}"
    assert reality.tag({"n": 2, "exp": -1.0}) in O.VERDICT_ZH


def test_web_view_survives_an_empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "STORE_FILE", str(tmp_path / "none.json"))
    v = O.web_view()
    assert v["tracked"] == 0 and v["open"] == [] and v["closed"] == []
    assert v["stats"] == {}


# ── the rule S4 must never break ────────────────────────────────────────────
def test_the_tracker_has_no_order_path():
    """S4 is alert-only. Adding bookkeeping must not smuggle in an execution
    seam — the same guard strategy4 itself carries."""
    import inspect
    src = inspect.getsource(O)
    for forbidden in ("create_order", "createOrder", "place_order", "open_flip",
                      "set_leverage", "cancel_order"):
        assert forbidden not in src, f"{forbidden} appeared in the S4 tracker"


def test_tracking_failure_never_breaks_the_scan(tmp_path, monkeypatch, capsys):
    """Bookkeeping is strictly less important than the scan. An unwritable
    store must not stop S4 alerting — exercised for real rather than by
    grepping the source, which is how the first version of this test managed
    to assert nothing at all."""
    import strategy4 as S4
    monkeypatch.setattr(S4, "ENABLED", True)
    monkeypatch.setattr(S4, "STATE_FILE", str(tmp_path / "s4_state.json"))
    monkeypatch.setattr(S4, "SIGNALS_FILE", str(tmp_path / "s4_signals.json"))
    monkeypatch.setattr(S4, "scan", lambda client=None: {
        "signals": [_sig()], "checked": 1, "checked_by": {}, "rejected": {},
        "ts": time.time()})

    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(O, "tick", _boom)

    assert S4.tick(client=object()) is True, "a broken tracker stopped the scan"
    assert "outcome tracking failed" in capsys.readouterr().out


def test_the_scan_records_what_it_alerted(tmp_path, monkeypatch):
    """The other half: when tracking works, the alerted signal reaches the
    store. This is the promise strategy4's own docstring makes."""
    import strategy4 as S4
    monkeypatch.setattr(S4, "ENABLED", True)
    monkeypatch.setattr(S4, "STATE_FILE", str(tmp_path / "s4_state.json"))
    monkeypatch.setattr(S4, "SIGNALS_FILE", str(tmp_path / "s4_signals.json"))
    monkeypatch.setattr(O, "STORE_FILE", str(tmp_path / "s4_out.json"))
    monkeypatch.setattr(S4, "scan", lambda client=None: {
        "signals": [_sig()], "checked": 1, "checked_by": {}, "rejected": {},
        "ts": time.time()})
    monkeypatch.setattr(O, "evaluate_open", lambda *a, **k: {"settled": 0})

    assert S4.tick(client=object()) is True
    assert len(O.load()["open"]) == 1, "the alerted signal was not recorded"


def test_the_store_round_trips(tmp_path, monkeypatch):
    p = tmp_path / "s4o.json"
    monkeypatch.setattr(O, "STORE_FILE", str(p))
    store = O._blank()
    O.record([_sig()], store, time.time())
    O.save(store)
    assert O.load()["open"] == store["open"]


# ── short settlement (added 2026-08-11) ─────────────────────────────────────
# settle() was long-only. Left that way, a short's stop sits ABOVE entry so
# `low <= sl` fires on the first candle and EVERY short records -1R instantly —
# a book full of losses that never happened.
def _c(ts, high, low, close):
    return [ts, close, high, low, close, 1000.0]


def _short(entry=100.0, sl=103.0, tp=94.0, bar_ts=0):
    return {"entry": entry, "sl": sl, "tp": tp, "side": "short", "bar_ts": bar_ts}


def test_short_target_scores_a_win_not_a_loss():
    """Price FALLING to the target is the short's win. On the long formula this
    same walk scores -1R."""
    r = O.settle(_short(), [_c(1, 100.5, 93.0, 94.0)], now_ts=0)
    assert r and r["outcome"] == "tp"
    assert r["r"] > 0


def test_short_stop_is_hit_by_price_rising():
    r = O.settle(_short(), [_c(1, 104.0, 99.0, 103.5)], now_ts=0)
    assert r and r["outcome"] == "sl"
    assert r["r"] == -1.0


def test_a_quiet_candle_settles_neither_side():
    assert O.settle(_short(), [_c(1, 101.0, 99.0, 100.0)], now_ts=0) == {}


def test_short_ties_still_go_to_the_stop():
    """Same pessimism as the long side: a candle spanning both is a loss."""
    r = O.settle(_short(), [_c(1, 104.0, 93.0, 100.0)], now_ts=0)
    assert r and r["outcome"] == "sl"


def test_short_excursions_are_measured_from_the_shorts_side():
    """MAE is the move AGAINST a short (price up), MFE the move with it."""
    r = O.settle(_short(), [_c(1, 102.0, 99.0, 100.0),
                            _c(2, 100.0, 93.0, 94.0)], now_ts=0)
    assert r and r["outcome"] == "tp"
    assert r["mae"] < 0 and r["mfe"] > 0


def test_long_settlement_is_unchanged_by_the_short_support():
    """The regression guard: adding shorts must not have moved the long path."""
    long_t = {"entry": 100.0, "sl": 97.0, "tp": 106.0, "side": "long", "bar_ts": 0}
    win = O.settle(long_t, [_c(1, 107.0, 100.0, 106.5)], now_ts=0)
    loss = O.settle(long_t, [_c(1, 101.0, 96.0, 96.5)], now_ts=0)
    assert win["outcome"] == "tp" and win["r"] > 0
    assert loss["outcome"] == "sl" and loss["r"] == -1.0


def test_a_trade_with_no_side_is_still_treated_as_long():
    """Everything recorded before today has no `side` field. It must keep
    settling exactly as it did, not silently become a short."""
    legacy = {"entry": 100.0, "sl": 97.0, "tp": 106.0, "bar_ts": 0}
    r = O.settle(legacy, [_c(1, 107.0, 100.0, 106.5)], now_ts=0)
    assert r["outcome"] == "tp" and r["r"] > 0


def test_record_keeps_a_short_instead_of_dropping_it():
    store = {"open": {}, "closed": []}
    sig = {"symbol": "AAPL/USDT:USDT", "base": "AAPL", "segment": "tradfi",
           "side": "short", "bar_ts": 1,
           "plan": {"entry": 100.0, "sl": 103.0, "tp": 94.0, "rr": 2, "stop_pct": 3.0}}
    assert O.record([sig], store, now_ts=0) == 1
    assert next(iter(store["open"].values()))["side"] == "short"
