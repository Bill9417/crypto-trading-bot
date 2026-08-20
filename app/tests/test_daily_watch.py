"""👀 每日觀察清單 — the coins that kept showing up today.

Distinct from 綜合前三名, which is a live snapshot that changes every sweep.
This ACCUMULATES across a 台北 day, and the properties that make that honest
are what these tests hold: the count must measure time rather than page loads,
the ranking must be breadth rather than a blended score, and a day must not
inherit yesterday's tally.
"""
import daily_watch as W
import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "STATE_FILE", str(tmp_path / "watch.json"))


def _fake(sightings):
    """Accepts the shorthand {"AAA": {"s2": "做多"}} as well as the real shape
    {"AAA": {"s2": {"note": ..., "side": ...}}}. The shorthand carries no
    direction, which is the correct reading of a test that does not state one."""
    norm = {}
    for base, srcs in sightings.items():
        norm[base] = {
            src: (v if isinstance(v, dict) else {"note": v, "side": None})
            for src, v in srcs.items()}
    return lambda: norm


def test_the_count_measures_time_not_page_loads(monkeypatch):
    """Leaving the dashboard open would otherwise inflate every coin on it,
    and two people looking would double it."""
    monkeypatch.setattr(W, "_sightings", _fake({"AAA": {"s2": "做多"}}))
    first = W.top()["top"][0]["hits"]
    for _ in range(6):
        W.top()
    assert W.top()["top"][0]["hits"] == first, "page loads advanced the tally"


def test_a_forced_refresh_does_advance_it(monkeypatch):
    monkeypatch.setattr(W, "_sightings", _fake({"AAA": {"s2": "做多"}}))
    W.top()
    before = W.top(do_refresh=False)["top"][0]["hits"]
    W.refresh(force=True)
    assert W.top(do_refresh=False)["top"][0]["hits"] > before


def test_ranked_by_how_many_engines_agree_not_by_repetition(monkeypatch):
    """A coin three engines flagged once beats one engine that flagged the
    same coin thirty times. There is no weighted score on purpose: every one
    of these engines measures somewhere between negative and indistinguishable
    from zero, so a blend of them would be a made-up number."""
    monkeypatch.setattr(W, "_sightings", _fake({
        "BROAD": {"s2": "a", "flip": "b", "zone": "c"},
        "LOUD": {"s2": "a"},
    }))
    W.top()
    for _ in range(30):                      # LOUD seen over and over
        W.refresh(force=True)
    rows = W.top(do_refresh=False)["top"]
    assert rows[0]["base"] == "BROAD", "repetition outranked agreement"
    assert rows[0]["engines"] == 3 and rows[1]["engines"] == 1


def test_a_tie_is_broken_by_recency_not_the_alphabet():
    """Alphabetical was the original last resort and it put 0G and A at the
    top of a five-way tie — an ordering that means nothing and looks like one
    that does."""
    import inspect
    src = inspect.getsource(W.top)
    assert '-r["last"]' in src, "ties still fall through to the alphabet"


def test_today_does_not_inherit_yesterday(monkeypatch):
    from datetime import datetime, timedelta
    monkeypatch.setattr(W, "_sightings", _fake({"OLD": {"s2": "x"}}))
    day1 = datetime(2026, 8, 19, 10, 0, tzinfo=W.TZ)
    W.top(now=day1)
    monkeypatch.setattr(W, "_sightings", _fake({"NEW": {"s2": "x"}}))
    out = W.top(now=day1 + timedelta(days=1))
    bases = [r["base"] for r in out["top"]]
    assert bases == ["NEW"], f"yesterday's coins carried over: {bases}"


def test_a_broken_source_contributes_nothing_rather_than_erroring(monkeypatch):
    """One engine's state file being unreadable must not take the list down."""
    import builtins
    real = builtins.open

    def boom(path, *a, **k):
        if "strategy2_signals" in str(path):
            raise OSError("unreadable")
        return real(path, *a, **k)

    monkeypatch.setattr(builtins, "open", boom)
    out = W._sightings()
    assert isinstance(out, dict)


def test_it_is_an_observe_list_with_no_entry_or_stop(monkeypatch):
    monkeypatch.setattr(W, "_sightings", _fake({"AAA": {"s2": "做多"}}))
    row = W.top()["top"][0]
    for banned in ("entry", "sl", "tp", "plan", "score"):
        assert banned not in row, f"{banned} on a watchlist row reads as a trade"


def test_one_count_per_refresh_per_engine(monkeypatch):
    """The invariant the removed guard used to claim: _sightings() is a dict
    keyed by base then source, so a pair cannot appear twice in one pass."""
    monkeypatch.setattr(W, "_sightings", _fake({"AAA": {"s2": "x", "flip": "y"}}))
    W.top()
    row = W.top(do_refresh=False)["top"][0]
    assert row["hits"] == 2 and row["engines"] == 2, \
        "one refresh counted an engine more than once"
    W.refresh(force=True)
    assert W.top(do_refresh=False)["top"][0]["hits"] == 4


def test_old_days_are_dropped_rather_than_growing_forever(monkeypatch):
    from datetime import datetime, timedelta
    monkeypatch.setattr(W, "_sightings", _fake({"AAA": {"s2": "x"}}))
    start = datetime(2026, 8, 1, 10, 0, tzinfo=W.TZ)
    for i in range(W.KEEP_DAYS + 4):
        W.top(now=start + timedelta(days=i))
    import json
    with open(W.STATE_FILE, encoding="utf-8") as f:
        kept = json.load(f)
    assert len(kept) <= W.KEEP_DAYS, f"state grows forever: {len(kept)} days"


def test_engines_pointing_opposite_ways_are_not_called_agreement(monkeypatch):
    """AIN sat at #5 with 'S2 做空' and '供需區 做多' badged as 2 個引擎 — a
    confluence claim the data did not support, on exactly the row a reader
    would act on."""
    monkeypatch.setattr(W, "_sightings", _fake({
        "SPLIT": {"s2": {"note": "做空", "side": "short"},
                  "zone": {"note": "做多", "side": "long"}},
        "AGREE": {"s2": {"note": "做多", "side": "long"},
                  "zone": {"note": "做多", "side": "long"}},
    }))
    rows = {r["base"]: r for r in W.top()["top"]}
    assert rows["SPLIT"]["conflict"] is True and rows["SPLIT"]["side"] is None
    assert rows["AGREE"]["conflict"] is False and rows["AGREE"]["side"] == "long"


def test_a_conflicted_coin_ranks_below_a_clean_one(monkeypatch):
    monkeypatch.setattr(W, "_sightings", _fake({
        "SPLIT": {"s2": {"note": "做空", "side": "short"},
                  "zone": {"note": "做多", "side": "long"},
                  "flip": {"note": "做多", "side": "long"}},
        "CLEAN": {"s2": {"note": "做多", "side": "long"},
                  "zone": {"note": "做多", "side": "long"}},
    }))
    order = [r["base"] for r in W.top()["top"]]
    assert order[0] == "CLEAN", "a 3-engine disagreement outranked a 2-engine agreement"


def test_a_directionless_engine_does_not_create_a_conflict(monkeypatch):
    """OI anomalies carry no side. Absent is not opposite."""
    monkeypatch.setattr(W, "_sightings", _fake({
        "AAA": {"s2": {"note": "做多", "side": "long"},
                "oi": {"note": "持倉 +5%", "side": None}},
    }))
    r = W.top()["top"][0]
    assert r["conflict"] is False and r["side"] == "long"
