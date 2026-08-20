"""💸 The book must record what you would have GOT, not what the shape did.

Asked for 2026-08-20 after the gap between page and reality was quantified:
the recorded +0.291R assumed a fill AT the level and assumed you took all ~63
signals a day. Neither is available to a real account, so the page and the
account could never have agreed.

Two changes make them the same question, and these tests hold them:
  · settle() deducts fees and slippage, so `r` IS the net result
  · record() declines a signal when every slot was full, and counts it
"""
import flip_outcomes as F
import strategy4_outcomes as O
import trade_costs as C


def _win_candles():
    return [[i * 900_000 + 900_000, 100, 105, 99, 104, 1] for i in range(3)]


def _loss_candles():
    return [[i * 900_000 + 900_000, 100, 100.5, 97, 97.5, 1] for i in range(3)]


TRADE = {"entry": 100.0, "sl": 98.0, "tp": 104.0, "side": "long",
         "bar_ts": 0, "stop_pct": 2.0}


def test_the_headline_r_is_net_of_costs():
    r = O.settle(TRADE, _win_candles(), now_ts=9e9)
    assert r["r_gross"] == 2.0
    assert r["cost_r"] > 0
    assert r["r"] == round(2.0 - r["cost_r"], 4)
    assert r["r"] < r["r_gross"], "the headline is still the frictionless number"


def test_cost_subtracts_from_the_result_not_the_direction():
    """A stop-out only crosses the spread once on the way out, which tempts a
    half-cost shortcut. Taking it would flatter every book by exactly the
    amount that decides whether these strategies are viable."""
    loss = O.settle(TRADE, _loss_candles(), now_ts=9e9)
    assert loss["r_gross"] == -1.0
    assert loss["r"] < -1.0, "a loss got cheaper because of fees"
    win = O.settle(TRADE, _win_candles(), now_ts=9e9)
    assert abs(win["cost_r"] - loss["cost_r"]) < 1e-9


def test_a_tight_stop_is_mostly_cost():
    """Why MIN_STOP_PCT exists and is not a tuning knob."""
    assert C.cost_r(0.5) > C.cost_r(1.0) > C.cost_r(3.0)
    assert C.cost_r(0.5) > 0.5, "a 0.5% stop should be dominated by cost"


def test_an_unknown_stop_costs_nothing_rather_than_a_guess():
    net, cost = C.net_r(2.0, None)
    assert (net, cost) == (2.0, 0.0)
    assert C.cost_r(0.0) == 0.0


def test_the_book_only_takes_what_a_free_slot_existed_for():
    st = F._blank()

    def sig(i):
        return {"symbol": f"S{i}/USDT:USDT", "base": f"S{i}",
                "ts": 1_787_000_000_000 + i, "blue_sky": True,
                "zone_top": 1.02, "zone_bottom": 1.0, "touches": 3,
                "plan": {"entry": 1.05, "sl": 1.0, "tp": 1.15,
                         "stop_pct": 4.8, "rr": 2.0}}

    took = sum(1 for i in range(20) if F.record(sig(i), st, now_ts=1.0, max_concurrent=8))
    assert took == 8, "the book recorded more trades than it had slots for"
    assert st["skipped_no_slot"] == 12, "declined signals were dropped silently"


def test_the_page_states_both_assumptions():
    """A number without its cost model and its capacity limit is a
    frictionless, unlimited-capital result wearing a track record's clothes."""
    v = F.web_view()
    assert v.get("costs") and "滑價" in v["costs"]
    assert v.get("max_concurrent", 0) > 0
    assert "skipped_no_slot" in v
