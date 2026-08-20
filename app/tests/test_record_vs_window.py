"""📏 The record is the tally. `closed` is a rolling window.

On 2026-08-20 I reported the flip strategy as "-0.180R over 397, CI excludes
zero — the pattern loses money", computed from store["closed"]. That list is
pruned to KEEP_CLOSED, which at ~140 fires/day is a rolling FOUR DAYS. Two days
later the same list read +0.686R, also with an interval excluding zero, on the
same unchanged code. The unpruned tally said +0.29R the whole time.

Two four-day windows of one strategy, opposite conclusions, both "significant".
These tests exist so the window can never be mistaken for the sample again.
"""
import os

import app as APP
import flip_outcomes as F

APP_DIR = os.path.dirname(os.path.abspath(APP.__file__))


def _client():
    APP.app.config["TESTING"] = True
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def test_stats_come_from_the_unpruned_tally():
    """If stats ever read `closed`, the headline number starts rolling over
    every four days while claiming to be a track record."""
    import inspect

    import strategy4_outcomes as S4O
    src = inspect.getsource(S4O.stats)
    assert 'store.get("tally")' in src
    assert '"closed"' not in src, "the scoreboard is reading the pruned list"


def test_the_payload_ships_both_counts_so_they_cannot_be_joined():
    d = _client().get("/api/flips").get_json()
    assert d.get("closed_is_window") is True
    assert "window_n" in d and "lifetime_n" in d
    stats_n = ((d.get("stats") or {}).get("all") or {}).get("n")
    if stats_n:
        assert d["lifetime_n"] == stats_n, \
            "the advertised sample size is not the one the stats rest on"
        assert len(d.get("closed") or []) <= d["window_n"]


def test_the_window_really_is_smaller_than_the_record():
    """Not a tautology — it is the whole hazard. When these are equal the book
    is young and the distinction is invisible; once pruning starts they
    diverge silently."""
    store = F.load()
    life = F.lifetime_n(store)
    win = len(store.get("closed") or [])
    if life > F.KEEP_CLOSED:
        assert win <= F.KEEP_CLOSED < life, \
            "pruning is on but the window is not smaller than the record"


def test_the_pruned_list_is_labelled_at_the_point_of_use():
    with open(os.path.join(APP_DIR, "flip_outcomes.py"), encoding="utf-8") as f:
        src = f.read()
    seg = src[src.index("def web_view("):]
    seg = seg[:seg.index("\n\n\ndef ") if "\n\n\ndef " in seg else len(seg)]
    # The EXPLANATION, not the word — "window_n" is a field name a few lines
    # below and contains "window", so a bare substring check passes with the
    # comment deleted.
    assert "KEEP_CLOSED" in seg and "rolling" in seg.lower(), \
        "nothing at the point of use explains that this list is pruned"
