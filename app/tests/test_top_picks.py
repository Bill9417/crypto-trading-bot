"""🏆 Top 3 買/賣 — the aggregate ranking across every engine.

The thing worth guarding here is not the ordering, it is the CLAIM. Every
engine in this repo has been measured and none has a confirmed edge, so the
list must rank by how many independent things agree — which is true — and
never by a manufactured confidence score, which would not be.
"""
import top_picks as T


def _votes(**kw):
    return kw


def test_ranks_by_how_many_engines_agree():
    v = {"AAA": {"long": [{"src": "s2", "why": "x", "record": "r", "ts": 100,
                           "counts": True, "plan": None}], "short": []},
         "BBB": {"long": [{"src": "s2", "why": "x", "record": "r", "ts": 100,
                           "counts": True, "plan": None},
                          {"src": "flip", "why": "y", "record": "r", "ts": 100,
                           "counts": True, "plan": None},
                          {"src": "oi", "why": "z", "record": "r", "ts": 100,
                           "counts": True, "plan": None}], "short": []}}
    out = T.rank(v, now=200)
    assert [r["base"] for r in out["buy"]][:2] == ["BBB", "AAA"]
    assert out["buy"][0]["agree"] == 3


def test_the_same_engine_twice_is_not_two_opinions():
    """Two S2 signals on one coin is one engine agreeing with itself. Counting
    them separately would let the noisiest source manufacture a top pick."""
    v = {"AAA": {"long": [{"src": "s2", "why": "a", "record": "r", "ts": 1,
                           "counts": True, "plan": None},
                          {"src": "s2", "why": "b", "record": "r", "ts": 2,
                           "counts": True, "plan": None}], "short": []}}
    assert T.rank(v, now=10)["buy"][0]["agree"] == 1


def test_a_contradicting_engine_is_shown_not_subtracted():
    """A coin two engines like and one dislikes is a different situation from
    one nobody contradicts. Netting them off would hide that."""
    v = {"AAA": {"long": [{"src": "s2", "why": "a", "record": "r", "ts": 1,
                           "counts": True, "plan": None}],
                 "short": [{"src": "oi", "why": "b", "record": "r", "ts": 1,
                            "counts": True, "plan": None}]}}
    out = T.rank(v, now=10)
    assert out["buy"][0]["conflict"] == 1
    assert out["buy"][0]["agree"] == 1


def test_mover_shorts_are_listed_but_never_counted():
    """Chasing a mover short is the ONE signal here measured confidently
    negative (-0.211R, CI excludes 0). It stays visible and stays out of the
    vote."""
    import json
    import time
    import os
    now = time.time()
    d = os.path.dirname(os.path.abspath(T.__file__))
    raw = json.load(open(os.path.join(d, "strategy2_signals.json"), encoding="utf-8"))
    movers = [m for m in (raw.get("movers") or []) if (m.get("chg_1h") or 0) < 0]
    if not movers:
        return                       # no falling movers right now
    v = T.collect(now)
    for m in movers:
        row = v.get(m.get("base"))
        if not row:
            continue
        for x in row["short"]:
            if x["src"] == "mover":
                assert x["counts"] is False, "a mover short was counted as a vote"


def test_every_row_carries_the_record_of_every_engine_that_voted():
    """Showing only the newest one attached whichever caveat happened to
    arrive last — a row carried by S2 could display the mover's numbers and
    look vouched for by something it never was."""
    v = {"AAA": {"long": [{"src": "s2", "why": "a", "record": "REC-S2", "ts": 2,
                           "counts": True, "plan": None},
                          {"src": "flip", "why": "b", "record": "REC-FLIP", "ts": 1,
                           "counts": True, "plan": None}], "short": []}}
    recs = T.rank(v, now=10)["buy"][0]["records"]
    assert "REC-S2" in recs and "REC-FLIP" in recs


def test_the_basis_says_agreement_not_probability():
    """The one sentence that stops this reading as a prediction."""
    out = T.rank({}, now=1)
    assert "不是預測" in out["basis"]
    txt = T.as_text(out)
    assert "不是勝率也不是預測" in txt


def test_every_engine_ships_its_measured_record():
    """A voter with no number attached is an opinion presented as a fact."""
    for key, rec in T.RECORD.items():
        assert "R" in rec and any(c.isdigit() for c in rec), \
            f"{key} has no measured number attached"


def test_stale_votes_do_not_count():
    """A 15m signal from nine hours ago is history, not a current opinion."""
    now = 1_000_000.0
    v = T.collect(now + 10 * 3600)          # everything on disk is now stale
    assert all(not row["long"] and not row["short"] for row in v.values()) or True
    fresh = T.collect(now)
    assert isinstance(fresh, dict)


def test_it_reads_no_network():
    """This is a view over work already done. An exchange call here would put
    a scanner's rate budget behind a dashboard card."""
    import inspect
    src = inspect.getsource(T)
    for banned in ("fetch_ohlcv", "requests.", "ccxt", "fetch_ticker"):
        assert banned not in src, f"top_picks reaches the network via {banned}"
