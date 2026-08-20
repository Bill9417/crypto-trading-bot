"""💸 Paper mode — strategies run, nothing spends.

2026-08-20: the owner moved the balance to a different Bybit account and asked
for the engines to keep running so results still accumulate. The Bybit account
this bot holds keys for was verified empty (0.086 USDT, zero open positions),
so nothing was stranded.

The point of this file is that ONE flag is not enough. s1_bybit_mirror.enabled()
checks its own switch and the API keys and never consults LIVE_TRADING, so the
master switch alone would have left the S1 mirror armed against a live account.
"""
import os
import re

import config

ENV = os.path.join(os.path.dirname(os.path.abspath(config.__file__)), ".env")


def _env(key):
    with open(ENV, encoding="utf-8") as f:
        m = re.search(rf"^{re.escape(key)}=(.*)$", f.read(), re.M)
    return (m.group(1).strip() if m else None)


def test_every_order_path_is_disarmed():
    import copy_engine
    import executor
    import s1_bybit_mirror
    import strategy4_exec
    armed = {
        "LIVE_TRADING (master)": bool(config.LIVE_TRADING),
        "S1 / Binance": bool(executor.is_live()),
        "S1 mirror / Bybit": bool(s1_bybit_mirror.enabled()),
        "S4 / Bybit": bool(strategy4_exec.enabled()),
        "S3 / Bybit": bool(config.STRATEGY3_LIVE),
        "S2": bool(config.STRATEGY2_LIVE),
        "copy followers": bool(copy_engine.live()),
    }
    on = [k for k, v in armed.items() if v]
    assert not on, f"live order paths still armed: {on}"

    # Also at the .env level. strategy4_exec.enabled() needs BOTH its own
    # switch and the master one, so with LIVE_TRADING off it reports disarmed
    # even if S4_EXEC were flipped back — true, and not what we want to rely
    # on: two switches off is the state, one switch off is an accident away.
    for key in ("LIVE_TRADING", "STRATEGY3_LIVE", "S1_BYBIT_MIRROR"):
        assert _env(key) in ("false", "0", "no", "off"), f"{key} is {_env(key)}"
    assert _env("S4_EXEC") not in ("bybit",), "S4_EXEC still names a venue"


def test_the_mirror_is_disarmed_by_its_own_flag_not_the_master():
    """Pins the trap. If this ever starts passing via LIVE_TRADING alone,
    someone has changed the mirror's gate and the belt-and-braces is gone."""
    import s1_bybit_mirror
    src = open(s1_bybit_mirror.__file__, encoding="utf-8").read()
    fn = src[src.index("def enabled()"):]
    fn = fn[:fn.index("\n\n")]
    assert "LIVE_TRADING" not in fn, \
        "the mirror now checks the master switch — update this note, not the test"
    assert _env("S1_BYBIT_MIRROR") in ("false", "0", "no", "off")


def test_signals_are_still_recorded_while_money_is_off():
    """The whole point of paper mode: the book must keep growing."""
    import strategy4_outcomes as O
    store = O.load()
    n0 = len(store["open"]) + len(store["closed"])
    sig = {"symbol": "PAPER/USDT:USDT", "base": "PAPER", "segment": "crypto",
           "side": "long", "bar_ts": 1_787_000_000_000,
           "plan": {"entry": 100.0, "sl": 98.0, "tp": 104.0, "rr": 2.0, "stop_pct": 2.0}}
    import copy
    st = copy.deepcopy(store)
    assert O.record([sig], st, now_ts=1_787_000_100.0) == 1
    assert len(st["open"]) + len(st["closed"]) == n0 + 1


def test_the_execution_path_declines_instead_of_ordering():
    import strategy4_exec as X
    out = X.open_trade({"symbol": "PAPER/USDT:USDT", "side": "long",
                        "plan": {"entry": 100.0, "sl": 98.0, "tp": 104.0}})
    assert out.get("ok") is not True and out.get("skipped") is True
    assert out.get("reason") == "disabled"
