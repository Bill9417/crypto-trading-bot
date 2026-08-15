"""📓 壓力翻支撐 forward record — the only thing that can settle the pattern.

breakout_flip ships with 163 backtested trades whose interval straddles zero,
over one ~10-day regime. That is a hypothesis. These tests guard the machinery
that turns it into evidence, and the failures that matter are the ones that
would make the record quietly WRONG rather than absent.
"""
import pytest

import breakout_flip as B
import flip_outcomes as F
import strategy4_outcomes as S4O

BAR = 1_000_000_000_000
M15 = 15 * 60 * 1000
NOW = BAR / 1000 + 10 * 3600


@pytest.fixture(autouse=True)
def store_file(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "STORE_FILE", str(tmp_path / "flip.json"))
    monkeypatch.setattr(F, "PACE_SEC", 0)
    return tmp_path / "flip.json"


def sig(**kw):
    base = {"symbol": "X/USDT:USDT", "base": "X", "ts": BAR,
            "zone_top": 100.2, "zone_bottom": 100.0, "touches": 2,
            "blue_sky": True, "room_pct": None,
            "plan": {"entry": 102.0, "sl": 99.85, "tp": 106.3, "rr": 2.0,
                     "stop_pct": 2.1}}
    return {**base, **kw}


def candle(i, high, low, close=None):
    return [BAR + i * M15, 100.0, high, low,
            close if close is not None else low, 0]


# ── recording ───────────────────────────────────────────────────────────────
def test_an_alerted_flip_is_recorded():
    store = F._blank()
    assert F.record(sig(), store, NOW) is True
    assert len(store["open"]) == 1
    row = next(iter(store["open"].values()))
    assert row["side"] == "long" and row["entry"] == 102.0


def test_the_same_flip_is_not_recorded_twice():
    """Keyed on the signal BAR, not wall-clock: the sweep re-runs every 5
    minutes and the same 15m bar would otherwise book the same trade repeatedly
    and inflate the sample with copies."""
    store = F._blank()
    assert F.record(sig(), store, NOW) is True
    assert F.record(sig(), store, NOW + 300) is False
    assert len(store["open"]) == 1


def test_a_settled_flip_is_not_re_recorded():
    store = F._blank()
    F.record(sig(), store, NOW)
    closed = store["open"].pop(F.key_of(sig()))
    store["closed"].append({**closed, "outcome": "tp", "r": 2.0})
    assert F.record(sig(), store, NOW) is False


def test_an_impossible_plan_is_refused():
    """sl above entry would make risk negative and every R meaningless."""
    store = F._blank()
    bad = sig(plan={"entry": 100.0, "sl": 105.0, "tp": 110.0})
    assert F.record(bad, store, NOW) is False
    assert not store["open"]


# ── the split the backtest cared about ──────────────────────────────────────
def test_blue_sky_and_ceiling_are_recorded_as_different_segments():
    """The backtest's most interesting result was blue sky +0.137R vs
    ceiling-nearby −0.261R. Pooled into one number the forward data can neither
    confirm nor kill that, which is the entire point of collecting it."""
    store = F._blank()
    F.record(sig(), store, NOW)
    F.record(sig(symbol="Y/USDT:USDT", base="Y", blue_sky=False, room_pct=2.1),
             store, NOW)
    segs = {t["segment"] for t in store["open"].values()}
    assert segs == {"blue", "ceiling"}


def test_the_tally_splits_by_segment():
    store = F._blank()
    S4O.accumulate(store, {"segment": "blue", "outcome": "tp", "r": 2.0,
                           "side": "long"})
    S4O.accumulate(store, {"segment": "ceiling", "outcome": "sl", "r": -1.0,
                           "side": "long"})
    assert store["tally"]["blue"]["n"] == 1
    assert store["tally"]["ceiling"]["n"] == 1
    assert store["tally"]["all"]["n"] == 2


# ── settlement reuses S4's rules ────────────────────────────────────────────
def test_settlement_is_the_shared_one_not_a_second_copy():
    """A second implementation would drift, silently, in the flattering
    direction. This pins that the shared function is what runs."""
    import inspect
    src = inspect.getsource(F)
    assert "S4O.settle" in src
    assert "def settle" not in src, "flip_outcomes grew its own settlement"


def test_a_candle_hitting_both_levels_is_scored_as_a_stop():
    """Inherited from strategy4_outcomes and re-asserted here because it is the
    single assumption that decides whether this record flatters the pattern."""
    trade = {"entry": 102.0, "sl": 99.85, "tp": 106.3, "side": "long",
             "bar_ts": BAR, "key": "k", "symbol": "X"}
    out = S4O.settle(trade, [candle(1, high=107, low=99)], NOW)
    assert out["outcome"] == "sl" and out["r"] == -1.0


def test_the_signal_bar_cannot_fill_its_own_trade():
    trade = {"entry": 102.0, "sl": 99.85, "tp": 106.3, "side": "long",
             "bar_ts": BAR, "key": "k", "symbol": "X"}
    same_bar = [[BAR, 100, 107, 99, 100, 0]]
    assert S4O.settle(trade, same_bar, NOW) == {}


# ── evaluation ──────────────────────────────────────────────────────────────
class _Client:
    def __init__(self, candles):
        self.candles = candles
        self.asked = []

    def call(self, method, symbol, tf, since, limit):
        self.asked.append(symbol)
        return self.candles


def test_a_resolved_flip_moves_from_open_to_closed():
    store = F._blank()
    F.record(sig(), store, NOW)
    c = _Client([candle(1, high=103, low=101), candle(2, high=107, low=102)])
    done = F.evaluate_open(c, store, NOW)
    assert done["settled"] == 1
    assert not store["open"] and len(store["closed"]) == 1
    assert store["closed"][0]["outcome"] == "tp"
    assert store["tally"]["all"]["n"] == 1


def test_an_unresolved_flip_stays_open_and_is_stamped():
    store = F._blank()
    F.record(sig(), store, NOW)
    c = _Client([candle(1, high=103, low=101)])
    done = F.evaluate_open(c, store, NOW)
    assert done["still_open"] == 1 and len(store["open"]) == 1
    assert next(iter(store["open"].values()))["checked_ts"] == NOW


def test_every_open_flip_is_checked_within_one_rotation():
    """Least-recently-checked first. Sorted by age with a per-tick cap, the
    head of the list is the same set every tick and the tail is never looked
    at — the bug strategy4_outcomes already paid for."""
    store = F._blank()
    for i in range(8):
        F.record(sig(symbol=f"S{i}/USDT:USDT", base=f"S{i}", ts=BAR + i),
                 store, NOW)
    c = _Client([candle(1, high=103, low=101)])
    for t in range(2):                       # ceil(8/6) == 2
        F.evaluate_open(c, store, NOW + t, max_eval=6)
    assert len({s for s in c.asked}) == 8


def test_a_dead_symbol_does_not_stall_the_book():
    store = F._blank()
    F.record(sig(), store, NOW)
    F.record(sig(symbol="Y/USDT:USDT", base="Y", ts=BAR + 1), store, NOW)

    class _Dead(_Client):
        def call(self, method, symbol, tf, since, limit):
            self.asked.append(symbol)
            if symbol == "X/USDT:USDT":
                raise RuntimeError("delisted")
            return self.candles

    c = _Dead([candle(1, high=107, low=101)])
    done = F.evaluate_open(c, store, NOW)
    assert done["errors"] == 1 and done["settled"] == 1


def test_no_client_means_no_settlement_rather_than_a_crash():
    store = F._blank()
    F.record(sig(), store, NOW)
    assert F.evaluate_open(None, store, NOW)["settled"] == 0


# ── the view ────────────────────────────────────────────────────────────────
def test_the_web_view_shows_the_backtest_beside_the_live_sample():
    """A live n of 6 next to a hidden 163 invites reading the small number as
    the answer. Both are shown, always."""
    v = F.web_view(F._blank())
    assert v["measured"]["n"] == B.MEASURED["n"]
    assert v["measured"]["ci"][0] < 0 < v["measured"]["ci"][1], \
        "the shipped interval no longer straddles zero — recheck the claim"


def test_the_view_survives_an_empty_store():
    v = F.web_view(F._blank())
    assert v["recent"] == [] and v["open"] == [] and v["closed"] == []
    assert v["stats"] == {}


def test_note_persists_immediately(store_file):
    """The alert is sent right after. If the record only lived in memory a
    crash between the two would lose the trade but not the message — the
    sample would silently under-count exactly the trades that were acted on."""
    assert F.note(sig(), NOW) is True
    assert store_file.exists()
    assert len(F.load()["open"]) == 1


# ── the card is wired ───────────────────────────────────────────────────────
def test_the_dashboard_knows_about_the_flip_card():
    import dashboard_layout as L
    assert "flips" in L.CARD_IDS
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(here, "templates", "index.html"),
                encoding="utf-8").read()
    assert 'data-card="flips"' in html


# ── the flip strip shows one row per coin (2026-08-14) ──────────────────────
def test_the_strip_shows_each_symbol_once():
    """The client-side dedupe keyed on `symbol:bar_ts`, which is unique per
    flip — so the same coin flipping again produced a second key and rendered
    twice. Live it was GENIUS×6, BLESS×6, AWE×5."""
    store = F._blank()
    store["recent"] = [
        {"symbol": "GENIUS/USDT:USDT", "key": "g:3", "fired_ts": 3},
        {"symbol": "GENIUS/USDT:USDT", "key": "g:2", "fired_ts": 2},
        {"symbol": "GENIUS/USDT:USDT", "key": "g:1", "fired_ts": 1},
        {"symbol": "BLESS/USDT:USDT", "key": "b:1", "fired_ts": 1}]
    v = F.web_view(store)
    assert [r["symbol"] for r in v["recent"]] == ["GENIUS/USDT:USDT",
                                                  "BLESS/USDT:USDT"]


def test_an_open_trade_does_not_also_appear_as_recent():
    """One position, not two. The strip concatenates open + recent, and a
    freshly opened flip is in BOTH."""
    store = F._blank()
    row = {"symbol": "X/USDT:USDT", "key": "x:1", "fired_ts": 5}
    store["open"] = {"x:1": row}
    store["recent"] = [row]
    v = F.web_view(store)
    assert [r["symbol"] for r in v["open"] + v["recent"]] == ["X/USDT:USDT"]


def test_two_open_flips_on_one_symbol_collapse_to_one():
    store = F._blank()
    store["open"] = {"x:1": {"symbol": "X", "key": "x:1", "fired_ts": 1},
                     "x:2": {"symbol": "X", "key": "x:2", "fired_ts": 9}}
    v = F.web_view(store)
    assert len(v["open"]) == 1
    assert v["open"][0]["fired_ts"] == 9        # newest kept


# ── OI context is context, never a gate (2026-08-15) ────────────────────────
def test_oi_context_never_changes_which_flips_fire():
    """Asked for as a "bonus flag". Measured on 192 production-universe flips
    over 29 days: gating on OI percentile made it WORSE (>=80: -0.072R vs
    +0.169R baseline), the ladder was non-monotonic, and the one live-looking
    cut had a lift over the flips it REJECTED of +0.281R +/-0.402 — noise.

    So it decorates the alert and decides nothing. This pins that: the same
    candles must produce the same fire/no-fire verdict whatever OI says."""
    import breakout_flip as B
    rows, p = [], 100.0
    for i in range(200):
        p += 0.05
        rows.append([i, p, p + 0.4, p - 0.4, p, 1000.0])
    st = {}
    with_oi = B.consider("X/USDT:USDT", rows, dict(st), 1000.0)
    B.oi_context = lambda *a, **k: {}          # OI unavailable
    without = B.consider("X/USDT:USDT", rows, dict(st), 1000.0)
    assert bool(with_oi) == bool(without), "OI availability changed the verdict"


def test_a_failed_oi_lookup_costs_the_context_not_the_alert(monkeypatch):
    """A dead endpoint must not swallow the signal."""
    import breakout_flip as B
    import crowd_radar as CR
    monkeypatch.setattr(CR, "oi_history",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert B.oi_context("X/USDT:USDT", []) == {}


def test_a_missing_oi_reading_is_absent_not_zero():
    """A 0% OI change asserts "positioning did not move". Absent means "we did
    not read it". Same fabricated-zero rule as everywhere else in this repo."""
    import breakout_flip as B
    import crowd_radar as CR
    orig = CR.oi_history
    CR.oi_history = lambda *a, **k: ([], [])
    try:
        assert B.oi_context("X/USDT:USDT", []) == {}
    finally:
        CR.oi_history = orig


def test_the_alert_does_not_present_oi_as_confirmation():
    """The row invites exactly that inference, so the measured non-result is
    stated next to it."""
    import breakout_flip as B
    sig = {"blue_sky": True, "room_pct": None, "zone_top": 1.02,
           "zone_bottom": 1.0, "touches": 3, "price": 1.05,
           "oi": {"oi_pct": 7.2, "pctile": 99.0, "state": "longs_opening"}}
    pl = {"entry": 1.05, "sl": 0.998, "tp": 1.15, "rr": 2.0, "stop_pct": 4.9}
    txt = B.format_alert("X/USDT:USDT", sig, pl)
    assert "持倉量" in txt
    assert "參考" in txt and "不是加分條件" in txt
