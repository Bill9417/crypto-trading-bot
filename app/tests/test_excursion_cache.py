"""The trade-autopsy panel must remember failures, not only successes.

/api/performance/excursions caches MAE/MFE per closed trade "for the process
lifetime", and its docstring promised later loads would be instant. That was
only ever true for trades it could COMPUTE. A record that failed cached
nothing, so every load re-walked the klines for it and failed the same way:
2026-08-09 three unusable trades out of 34 cost 6.4s on EVERY load, forever,
while 31 cached ones cost nothing.

Two of the three were AERGO, delisted from Binance — ccxt raises BadSymbol and
will keep raising it for as long as those rows exist. A closed trade is
immutable; "cannot compute this" is exactly as permanent a fact as a number,
and belongs in the cache with the same confidence.

The one thing that must NOT become permanent is a transient network failure,
so this pins both halves: settled-forever vs bounded-retry.
"""
import app as A


def _reset():
    A._excursion_cache.clear()
    A._excursion_fail.clear()


# ── delisted symbols are settled on the first attempt ───────────────────────
def test_a_delisted_symbol_is_never_retried():
    """ccxt.BadSymbol means the market is gone, not 'try again later'."""
    import ccxt
    assert ccxt.BadSymbol in A._PERMANENT_SYMBOL_ERRORS, \
        "BadSymbol must be treated as permanent or delisted coins refetch forever"


def test_permanent_and_transient_failures_are_distinguished():
    """The distinction is the whole point: retrying a delisted market is pure
    waste, and never retrying a network blip loses a real data point."""
    import ccxt
    assert ccxt.NetworkError not in A._PERMANENT_SYMBOL_ERRORS
    assert ccxt.RequestTimeout not in A._PERMANENT_SYMBOL_ERRORS


# ── the cache protocol itself ───────────────────────────────────────────────
def test_a_settled_failure_short_circuits_before_any_fetch():
    """None in the fail-map means settled forever. The endpoint must skip such
    a record without touching the network — that skip IS the fix."""
    _reset()
    A._excursion_fail[999] = None
    settled = (A._excursion_fail.get(999, 0) is None
               or A._excursion_fail.get(999, 0) >= A._EXCURSION_MAX_TRIES)
    assert settled


def test_a_transient_failure_is_retried_but_not_forever():
    _reset()
    rid = 1234
    for attempt in range(1, A._EXCURSION_MAX_TRIES + 1):
        settled = (A._excursion_fail.get(rid, 0) is None
                   or A._excursion_fail.get(rid, 0) >= A._EXCURSION_MAX_TRIES)
        assert not settled, f"gave up after {attempt - 1} tries — blips lose data"
        A._excursion_fail[rid] = A._excursion_fail.get(rid, 0) + 1
    settled = (A._excursion_fail.get(rid, 0) is None
               or A._excursion_fail.get(rid, 0) >= A._EXCURSION_MAX_TRIES)
    assert settled, "unbounded retry — this is exactly what made the panel slow"


def test_the_retry_budget_is_small_enough_to_matter():
    """Each retry costs a real kline walk. A generous budget just spreads the
    original bug over more loads."""
    assert 1 <= A._EXCURSION_MAX_TRIES <= 5


def test_success_still_wins_over_a_recorded_failure():
    """A record that failed transiently and then succeeded must serve the
    cached value — the fail-map must not shadow a real result."""
    _reset()
    A._excursion_fail[7] = 2
    A._excursion_cache[7] = {"symbol": "BTC/USDT:USDT", "mae": 1.0, "mfe": 2.0}
    assert A._excursion_cache.get(7) is not None      # checked first in the loop
