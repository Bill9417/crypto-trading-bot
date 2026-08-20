"""
📓 Outcome book for the zone (SELL/LONG at the box) signals.

A THIN WRAPPER, not a copy. flip_outcomes already owns the settle/tally logic
— which itself delegates to strategy4_outcomes.settle — and every function
there already accepts an explicit store. So this file supplies a different
FILE and reuses the rest. A second implementation of "walk the candles and
decide what happened" is how two books start disagreeing about what a win is.

The book exists before the signal is trusted, on purpose: the zone shape
measured +0.175R with-trend over 3,653 replayed entries, but the plain cut
died when the best five symbols were removed and the early half of the window
straddled zero. That is promising, not proven, and the only thing that settles
it is a forward record nobody can tune.
"""
import os

import flip_outcomes as F

STORE_FILE = os.path.join(os.path.dirname(__file__), "zone_outcomes.json")


def load() -> dict:
    return F.load(STORE_FILE)


def save(store: dict) -> None:
    F.save(store, STORE_FILE)


def note(sig: dict, now_ts: float = None) -> bool:
    """Record one alerted zone entry. False if it was already there."""
    store = load()
    added = F.record(sig, store, now_ts)
    if added:
        save(store)
    return added


def tick(client=None, now_ts: float = None) -> dict:
    """Settle whatever is due. Same cadence and caps as the flip book."""
    store = load()
    done = F.evaluate_open(client, store, now_ts)
    save(store)
    return {**done, "open": len(store["open"]), "closed": len(store["closed"])}


def stats(segment: str = "all") -> dict:
    return F.stats(load(), segment)


# The zone shape's OWN measurement. Replayed 2026-08-20 bar by bar over 52
# liquid Bybit perps, 1,000×15m each, entries scored on the same bracket and
# the same pessimistic intra-candle rule as every other book here.
MEASURED = {
    "n": 3653, "exp": 0.082, "ci": [0.038, 0.125], "wr": 39.6, "pf": 1.14,
    "with_trend": {"n": 2005, "exp": 0.175, "ci": [0.116, 0.234]},
    "against": {"n": 1648, "exp": -0.032, "ci": [-0.097, 0.034]},
    # The parts that stop this being a result:
    "minus_top5": {"n": 3314, "exp": 0.019, "ci": [-0.026, 0.064]},
    "with_trend_minus_top5": {"n": 1815, "exp": 0.090, "ci": [0.030, 0.151]},
    "early_half": {"n": 1043, "exp": 0.062, "ci": [-0.016, 0.140]},
    "late_half": {"n": 962, "exp": 0.297, "ci": [0.210, 0.385]},
    "rotation_p": 0.0375, "rotation_null": 0.111,
    "window": "2026-08-20, 52 symbols × ~10 days of 15m",
}


def web_view(limit: int = 20) -> dict:
    """The board, with the ZONE's numbers on it.

    flip_outcomes.web_view() attaches MEASURED / MEASURED_OOS / MEASURED_SEQ —
    the FLIP's track record. Passing those through would have put another
    strategy's evidence next to these signals, which is the worst kind of
    wrong: plausible, specific, and about something else.
    """
    v = F.web_view(load(), limit)
    for k in ("measured", "measured_oos", "measured_seq"):
        v.pop(k, None)
    v["measured"] = MEASURED
    return v
