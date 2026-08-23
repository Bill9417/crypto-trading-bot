"""📒 The trade log and the variant replay.

Asked for 2026-08-22: a page listing every trade in two versions — take
everything, versus skip a setup with resistance overhead — with a full log for
both.

THE LOAD-BEARING IDEA, and the one that is easy to get wrong: Type 2 is NOT
Type 1's log with the ceiling rows removed. A book holding 8 positions at once
DECLINES what fires while it is full — the flip book had declined 824 signals
against 82 taken. Skipping a ceiling setup FREES A SLOT, so a later setup that
Type 1 had to decline gets taken instead. The two rule-sets hold different
portfolios and neither is a subset of the other.

That is why the log is uncapped and the filters run at read time, and it is
what most of this file tests.
"""
import os

import pytest

import trade_log as L

BASE_TS = 1_700_000_000.0


def _sig(sym="A", ts=1, blue=True, entry=10.0, sl=9.0, tp=12.0):
    return {"symbol": f"{sym}/USDT:USDT", "base": sym, "ts": ts,
            "blue_sky": blue, "room_pct": None if blue else 3.2, "touches": 3,
            "plan": {"entry": entry, "sl": sl, "tp": tp, "stop_pct": 10.0,
                     "rr": 2.0, "side": "long"}}


def _row(key, seg, fired, exit_ts=None, r=None, outcome=None):
    return {"key": key, "source": "flip", "symbol": f"{key}/USDT:USDT",
            "base": key, "side": "long", "segment": seg,
            "entry": 10.0, "sl": 9.0, "tp": 12.0, "stop_pct": 10.0,
            "fired_ts": fired, "exit_ts": exit_ts, "r": r,
            "r_gross": r, "cost_r": 0.0, "outcome": outcome}


# ── the log records everything ───────────────────────────────────────────────
def test_it_records_far_past_the_capacity_cap():
    """The whole reason this file exists. flip_outcomes stops at 8 and the
    trades a different rule would have taken instead are never settled, so its
    book cannot answer the question."""
    st = L._blank()
    for i in range(30):
        assert L.record(_sig(f"S{i}", ts=i), "flip", st, BASE_TS + i) is True
    assert len(st["rows"]) == 30 > L.MAX_CONCURRENT


def test_the_same_bar_is_never_logged_twice():
    st = L._blank()
    assert L.record(_sig("A", ts=7), "flip", st, BASE_TS) is True
    assert L.record(_sig("A", ts=7), "flip", st, BASE_TS + 999) is False
    assert L.record(_sig("A", ts=8), "flip", st, BASE_TS + 999) is True


def test_a_signal_with_no_plan_is_refused():
    """No entry/stop/target means nothing to replay. Logging it would put a
    row in the book that can never be scored."""
    st = L._blank()
    bad = _sig("A")
    bad["plan"] = {}
    assert L.record(bad, "flip", st, BASE_TS) is False
    assert st["rows"] == []


def test_the_row_carries_what_the_filters_key_on():
    """A field not stored at record time cannot be filtered on later, and
    back-filling it is impossible once the bar is gone."""
    st = L._blank()
    L.record(_sig("A", blue=False), "flip", st, BASE_TS)
    r = st["rows"][0]
    assert r["segment"] == "ceiling" and r["blue_sky"] is False
    for f in ("entry", "sl", "tp", "stop_pct", "bar_ts", "fired_ts", "source"):
        assert r.get(f) is not None, f
    # unsettled fields exist as None so a row's shape never changes
    for f in ("outcome", "exit_price", "exit_ts", "r", "cost_r"):
        assert f in r and r[f] is None


def test_since_is_stamped_once():
    """The page states when the log starts, because everything the capped book
    declined BEFORE that was never settled and cannot be recovered."""
    st = L._blank()
    L.record(_sig("A", ts=1), "flip", st, BASE_TS)
    L.record(_sig("B", ts=2), "flip", st, BASE_TS + 500)
    assert st["since"] == BASE_TS


# ── the replay ───────────────────────────────────────────────────────────────
def test_skipping_a_setup_frees_its_slot_for_a_later_one():
    """THE test. With one slot: A (ceiling) hogs it and B is declined; filter A
    out and B gets in. Neither run is a subset of the other."""
    rows = [_row("A", "ceiling", 0, 100, -1.0, "sl"),
            _row("B", "blue", 10, 200, 2.0, "tp"),
            _row("C", "blue", 150, 300, 2.0, "tp")]
    take_all = L.replay(rows, keep=L.VARIANTS["all"]["keep"], max_concurrent=1)
    blue_only = L.replay(rows, keep=L.VARIANTS["blue_only"]["keep"],
                         max_concurrent=1)
    a = {r["key"] for r in take_all["taken"]}
    b = {r["key"] for r in blue_only["taken"]}
    assert a == {"A", "C"} and b == {"B"}
    assert b - a, "the filtered run took nothing the unfiltered run could not"
    assert a - b


def test_the_reason_a_signal_was_declined_is_recorded():
    """'the rule skipped it' and 'there was no room' are different facts, and
    only one of them is about the strategy."""
    rows = [_row("A", "ceiling", 0, 100), _row("B", "blue", 1, 100),
            _row("C", "blue", 2, 100)]
    res = L.replay(rows, keep=L.VARIANTS["blue_only"]["keep"], max_concurrent=1)
    why = {r["key"]: r["skipped_because"] for r in res["skipped"]}
    assert why == {"A": "filter", "C": "no_slot"}


def test_a_slot_is_returned_when_its_trade_closes():
    rows = [_row("A", "blue", 0, 50), _row("B", "blue", 60, 100)]
    res = L.replay(rows, max_concurrent=1)
    assert [r["key"] for r in res["taken"]] == ["A", "B"]


def test_an_unsettled_trade_holds_its_slot():
    """Capacity you have not got back is capacity you have not got. Treating
    an open position as free would let the replay take trades a real account
    could not have."""
    rows = [_row("A", "blue", 0, None), _row("B", "blue", 10, 100)]
    res = L.replay(rows, max_concurrent=1)
    assert [r["key"] for r in res["taken"]] == ["A"]
    assert res["skipped"][0]["skipped_because"] == "no_slot"


def test_signals_are_replayed_in_fire_order_whatever_order_they_are_stored():
    rows = [_row("C", "blue", 300, 400), _row("A", "blue", 0, 100),
            _row("B", "blue", 150, 250)]
    res = L.replay(rows, max_concurrent=1)
    assert [r["key"] for r in res["taken"]] == ["A", "B", "C"]


# ── the arithmetic ───────────────────────────────────────────────────────────
def test_only_settled_trades_score():
    """An open trade has no result; counting it as a zero would be an outcome
    nobody observed."""
    st = L.stats([_row("A", "blue", 0, 100, 2.0, "tp"),
                  _row("B", "blue", 1, None)])
    assert st["n"] == 1 and st["open"] == 1
    assert st["exp"] == 2.0


def test_an_empty_book_reports_none_rather_than_zero():
    st = L.stats([])
    assert st["n"] == 0
    assert st["exp"] is None and st["net"] is None and st["wr"] is None


def test_the_interval_needs_more_than_one_trade():
    assert L.stats([_row("A", "blue", 0, 1, 2.0, "tp")])["ci"] is None
    two = L.stats([_row("A", "blue", 0, 1, 2.0, "tp"),
                   _row("B", "blue", 2, 3, -1.0, "sl")])
    assert two["ci"] and two["ci"][0] < two["exp"] < two["ci"][1]


# ── the board ────────────────────────────────────────────────────────────────
def _seed(monkeypatch, tmp_path, rows):
    monkeypatch.setattr(L, "STORE_FILE", str(tmp_path / "log.json"))
    L.save({"rows": rows, "since": 0.0})


def test_the_board_computes_every_variant_from_the_same_rows(monkeypatch,
                                                             tmp_path):
    rows = [_row("A", "ceiling", 0, 100, -1.0, "sl"),
            _row("B", "blue", 10, 200, 2.0, "tp")]
    _seed(monkeypatch, tmp_path, rows)
    b = L.board()
    assert set(b["variants"]) == set(L.VARIANTS)
    for v in b["variants"].values():
        assert "stats" in v and "trades" in v and "skipped" in v


def test_the_board_reports_how_the_two_rule_sets_differ(monkeypatch, tmp_path):
    """The only reason to run both is the difference, so the page states it
    rather than leaving it to be worked out from two lists."""
    rows = [_row("A", "ceiling", 0, 100, -1.0, "sl"),
            _row("B", "blue", 10, 200, 2.0, "tp")]
    _seed(monkeypatch, tmp_path, rows)
    monkeypatch.setattr(L, "MAX_CONCURRENT", 1)
    b = L.board()
    assert b["only_in_all"] >= 1 and b["only_in_blue"] >= 1


def test_the_board_scopes_to_one_detector(monkeypatch, tmp_path):
    """Two detectors have different setups, stops and hit rates. A combined
    expectancy is an average over two populations, not a result for either."""
    rows = [_row("A", "blue", 0, 100, 2.0, "tp"),
            {**_row("Z", "blue", 5, 100, -1.0, "sl"), "source": "zone"}]
    _seed(monkeypatch, tmp_path, rows)
    assert L.board(source="flip")["logged"] == 1
    assert L.board(source="zone")["logged"] == 1
    assert L.board()["logged"] == 2
    assert L.board()["sources"] == {"flip": 1, "zone": 1}


def test_trades_are_newest_first(monkeypatch, tmp_path):
    rows = [_row("A", "blue", 0, 100, 2.0, "tp"),
            _row("B", "blue", 500, 600, 2.0, "tp")]
    _seed(monkeypatch, tmp_path, rows)
    t = L.board()["variants"]["all"]["trades"]
    assert [r["key"] for r in t] == ["B", "A"]


# ── the page ─────────────────────────────────────────────────────────────────
def _client():
    import app as APP
    APP.app.config["TESTING"] = True
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def test_the_page_and_its_api_answer():
    c = _client()
    assert c.get("/trades").status_code == 200
    r = c.get("/api/trade_log?source=flip")
    assert r.status_code == 200
    assert set(r.get_json()["variants"]) == set(L.VARIANTS)


def test_the_page_needs_a_login():
    import app as APP
    APP.app.config["TESTING"] = True
    assert APP.app.test_client().get("/trades").status_code in (301, 302, 401)


def test_the_page_paints_its_own_background():
    """app.css does not paint the body on a standalone page. Omitting it
    rendered /trades transparent with BLACK text while the rest of the app is
    dark — it looked like a bad screenshot and it was the page."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "trades.html"),
              encoding="utf-8") as f:
        html = f.read()
    head = html[:html.index("</style>")]
    assert "body {" in head and "background:" in head, \
        "the page never sets a background"
    assert "--wolf-slate" in head


def test_the_page_states_what_the_log_cannot_recover():
    """Signals the capped book declined before the log existed were never
    settled. A page that quietly starts its history is claiming completeness
    it does not have."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "trades.html"),
              encoding="utf-8") as f:
        html = f.read()
    assert "紀錄從" in html, "the page never says when its history starts"
    assert "算不出來" in html, "the page never says what it cannot compute"


def test_the_page_explains_that_the_two_sets_differ():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "templates", "trades.html"),
              encoding="utf-8") as f:
        html = f.read()
    assert "only_in_all" in html and "only_in_blue" in html
    assert "空出位子" in html, "the page never explains WHY they differ"


def test_the_sweep_records_and_settles_the_log():
    """A log nothing writes to, or nothing settles, is an empty page forever."""
    import ast
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "strategy2_scanner.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    calls = {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name)}
    assert "trade_log.note" in calls, "nothing writes to the log"
    assert "trade_log.tick" in calls, "nothing settles the log"


@pytest.mark.parametrize("name", sorted(L.VARIANTS))
def test_every_variant_is_described_for_a_human(name):
    v = L.VARIANTS[name]
    assert v["label"] and v["desc"] and callable(v["keep"])
