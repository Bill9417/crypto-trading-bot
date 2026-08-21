"""🧾 The paper-trading posture: everything off, everything recorded.

Stated 2026-08-21: "all are dry run but keep the record. If the strategy works
I will run on real money." That makes the RECORD the deliverable — a dry run
whose outcomes are never measured produces nothing to decide on, and every
month of it is wasted.

Two invariants, and each has already been broken once:

  · nothing is armed. Four engines can place orders and all four are switched
    off in .env; a test that reads the switches catches an accidental commit
    of a live one.
  · every engine that ALERTS also SETTLES. 供需區 recorded signals for weeks
    and settled none of them, because zone_outcomes.tick() existed and nothing
    called it; 隧道上方爆量 shipped with a sightings file and no outcome book
    at all. Both looked completely healthy from outside — signals appearing,
    files being written — while accumulating nothing that could ever be
    checked against their backtests.
"""
import ast
import os

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sweep_calls():
    """{module.func} actually called in strategy2_scanner."""
    with open(os.path.join(APP_DIR, "strategy2_scanner.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)}


# Every book that a live scanner writes into. Adding an engine without adding
# it here does not fail — but the completeness test below fails if a module
# named *_outcomes exists and is not listed.
BOOKS = ["flip_outcomes", "zone_outcomes", "vegas_outcomes", "thrust_outcomes"]


@pytest.mark.parametrize("book", BOOKS)
def test_every_book_that_records_also_settles(book):
    """A book that only records is a claim nobody can falsify. 供需區 sat at
    76 open / 0 settled for as long as it existed."""
    calls = _sweep_calls()
    records = f"{book}.note" in calls or f"{book}.record" in calls
    settles = f"{book}.tick" in calls
    if records:
        assert settles, (
            f"{book} records signals but nothing settles them — its live "
            f"record can only ever be empty")


def test_no_outcome_book_is_left_unwired():
    """The point is the CLASS, not the four known books. 隧道上方爆量 had a
    detector, a card, a backtest and no outcome module at all, and nothing
    noticed until the posture was audited by hand."""
    modules = [f[:-3] for f in os.listdir(APP_DIR)
               if f.endswith("_outcomes.py")]
    unlisted = sorted(set(modules) - set(BOOKS) - {"strategy4_outcomes",
                                                   "signal_outcomes"})
    assert not unlisted, (
        f"outcome modules not covered by this test: {unlisted}")


@pytest.mark.parametrize("detector,book", [
    ("vegas_scan", "vegas_outcomes"),
    ("vol_thrust", "thrust_outcomes"),
])
def test_every_alerting_detector_has_a_book(detector, book):
    """A detector whose signals go to a card but not to a book can be admired
    and never evaluated.

    The WRITE is looked for across the whole app, not just in the sweep:
    vegas_scan hands vegas_outcomes.note in as an `on_fire` callback rather
    than calling it inline, which is equally correct and invisible to a
    sweep-only search. The SETTLE is checked in the sweep, because that is the
    only loop that runs it.
    """
    assert os.path.exists(os.path.join(APP_DIR, f"{book}.py")), \
        f"{detector} alerts with no forward record"
    written = [f for f in os.listdir(APP_DIR)
               if f.endswith(".py") and f != f"{book}.py"
               and (f"{book}.note" in open(os.path.join(APP_DIR, f),
                                           encoding="utf-8").read()
                    or (f == "vegas_scan.py" and book == "vegas_outcomes"
                        and "on_fire=O.note" in open(
                            os.path.join(APP_DIR, f), encoding="utf-8").read()))]
    assert written, f"{detector} fires but nothing ever writes to {book}"


def test_the_thrust_record_measures_what_its_card_claims():
    """A record kept in different units to the claim it is testing cannot
    settle it."""
    import thrust_outcomes as TO
    import vol_thrust as T
    claimed = tuple(h for h, _m, _md in T.MEASURED["fwd"])
    assert TO.HORIZONS == claimed, (
        f"the book measures {TO.HORIZONS} but the card quotes {claimed}")


def test_each_book_writes_to_its_own_file():
    """zone_outcomes wraps flip_outcomes and thrust_outcomes wraps
    vegas_outcomes. A wrapper that forgets its own path silently writes into
    the book it borrowed from, merging two strategies' records."""
    import flip_outcomes
    import thrust_outcomes
    import vegas_outcomes
    import zone_outcomes
    paths = [flip_outcomes.STORE_FILE, zone_outcomes.STORE_FILE,
             vegas_outcomes.STORE_FILE, thrust_outcomes.STORE_FILE]
    assert len(set(paths)) == len(paths), f"two books share a file: {paths}"


def test_a_wrapper_does_not_write_into_the_book_it_borrows(tmp_path,
                                                           monkeypatch):
    """The mechanism, not just the constant: thrust_outcomes.note() must land
    in the thrust file and leave the vegas file untouched."""
    import thrust_outcomes as TO
    import vegas_outcomes as VO
    monkeypatch.setattr(TO, "STORE_FILE", str(tmp_path / "thrust.json"))
    monkeypatch.setattr(VO, "STORE_FILE", str(tmp_path / "vegas.json"))
    VO.save(VO._blank())
    TO.note({"symbol": "X/USDT:USDT", "base": "X", "ts": 1, "close": 1.0}, 1.0)
    assert len(TO.load()["open"]) == 1
    assert len(VO.load()["open"]) == 0, "the wrapper wrote into the wrong book"


# ── nothing is armed ─────────────────────────────────────────────────────────
LIVE_SWITCHES = {
    "LIVE_TRADING": "false",        # S1 — Binance orders
    "STRATEGY2_LIVE": "false",      # S2 as the live engine
    "STRATEGY3_LIVE": "false",      # S3 — Bybit flag-flip
    "S4_EXEC": "off",               # S4 — Bybit perp orders
    "S1_BYBIT_MIRROR": "false",     # the S1 -> Bybit mirror
}


def _env_file():
    path = os.path.join(APP_DIR, ".env")
    if not os.path.exists(path):
        pytest.skip(".env not present")
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


@pytest.mark.parametrize("key,expected", sorted(LIVE_SWITCHES.items()))
def test_no_engine_is_armed(key, expected):
    """Paper mode, deliberately. This fails the moment one is committed live —
    which is the point: arming an engine should be a decision, not something
    that arrives with an unrelated change."""
    env = _env_file()
    got = (env.get(key) or "").strip().lower()
    assert got == expected, (
        f"{key}={got!r} — expected {expected!r}. If this is deliberate, change "
        f"the expectation here in the same commit that arms it.")
