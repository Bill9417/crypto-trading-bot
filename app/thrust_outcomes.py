"""
📓 隧道上方爆量 forward record — what actually happened after each alert.

A THIN WRAPPER over vegas_outcomes, not a copy. That module already owns the
settle logic — next-bar entry, forward % at fixed horizons, mean AND median —
and every function there takes an explicit store path and horizon tuple. A
second implementation of "walk the candles and see what happened" is how two
books start disagreeing about what a win is.

WHY THIS EXISTS AT ALL. vol_thrust shipped with a state file that records
SIGHTINGS — which coin fired, when, how big the surge was — and nothing that
records OUTCOMES. So the card carried a backtest and could never accumulate
anything to check it against: exactly the unfalsifiable-claim shape this repo
keeps finding, and the one thing that makes a dry run worth running.

Found 2026-08-21 while auditing the paper-trading posture, on the way to the
only question that matters here: if this is ever going to decide real money,
what is the evidence going to be?

THE HORIZONS MATCH THE CLAIM. vol_thrust.MEASURED quotes forward returns at
+1/6/24/48h, so the live book measures the same four. A record kept in
different units to the claim it is testing cannot settle it.
"""
import os

import vegas_outcomes as V
import vol_thrust as T

STORE_FILE = os.path.join(os.path.dirname(__file__), "thrust_outcomes.json")

# The same horizons vol_thrust.MEASURED reports, so backtest and live sit in
# the same units and can be read side by side.
HORIZONS = tuple(h for h, _mean, _median in T.MEASURED["fwd"])


def load() -> dict:
    return V.load(STORE_FILE)


def save(store: dict) -> None:
    V.save(store, STORE_FILE)


def note(sig: dict, now_ts: float = None) -> bool:
    """Record one alerted thrust. False if this bar is already in the book."""
    return V.note(sig, now_ts, path=STORE_FILE)


def tick(client=None, now_ts: float = None) -> dict:
    """Settle whatever has aged past the longest horizon."""
    return V.tick(client, now_ts, horizons=HORIZONS, path=STORE_FILE)


def stats(store: dict = None) -> dict:
    return V.stats(store if store is not None else load(), horizons=HORIZONS)


def web_view(limit: int = 20) -> dict:
    store = load()
    return {
        "live": stats(store),
        "horizons": list(HORIZONS),
        # The backtest this record exists to test, carried alongside so the
        # card cannot show one without the other.
        "measured": T.MEASURED,
        "open": len(store.get("open") or {}),
        "settled": len(store.get("closed") or []),
        # Same assumptions as the vegas book — it is the same settle code — so
        # the card can say what its R actually means.
        "basis": V.web_view().get("basis") or {},
        "max_concurrent": V.MAX_CONCURRENT,
        "skipped_no_slot": int(store.get("skipped_no_slot") or 0),
    }
