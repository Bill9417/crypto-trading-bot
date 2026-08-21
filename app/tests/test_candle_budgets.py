"""📏 Every scanner must fetch at least as many bars as its own gates need.

THE FAILURE THIS PREVENTS IS SILENCE. When a requirement outgrows a fetch, the
scanner does not crash — it takes the "not enough history" branch on every
symbol and reports zero signals, which is exactly what it reports on a quiet
market. The logs look normal. In July that cost three days before anyone
noticed, and the same shape came back through a period change when
All-in-One_ULTIMATE_Pro moved the outer Vegas tunnel to EMA676 and
strategy2_meter's requirement jumped to ~690.

That one was fixed twice, differently:
  · strategy2_scanner took the STRUCTURAL fix — CANDLES = max(literal, need)
  · strategy4 was fixed by bumping the literal 450 -> 750 by hand

Only the first kind holds. On 2026-08-21 strategy4 was still on a hand-set
literal with 42 bars of headroom, in the module that places real Bybit orders.
This file pins the derivation for every scanner, so the next period change
cannot disarm one of them quietly.
"""
import importlib

import pytest

import strategy2_meter as METER


def _meter_need():
    return METER.SIGNAL_MIN_CANDLES + 20


# (module, attribute holding the fetch size, callable giving the requirement)
BUDGETS = [
    ("strategy2_scanner", "CANDLES", _meter_need),
    ("strategy4", "CANDLES", _meter_need),
    ("vegas_scan", "CANDLES", lambda: importlib.import_module("vegas_reclaim").WARMUP + 1),
    ("strategy3_scanner", "CANDLES",
     lambda: importlib.import_module("strategy3_signal").MIN_CANDLES),
]


@pytest.mark.parametrize("mod_name,attr,need_fn", BUDGETS,
                         ids=[b[0] for b in BUDGETS])
def test_the_fetch_covers_the_requirement(mod_name, attr, need_fn):
    mod = importlib.import_module(mod_name)
    have = getattr(mod, attr)
    need = need_fn()
    assert have >= need, (
        f"{mod_name}.{attr} fetches {have} but its gates need {need} — every "
        f"symbol would report 'not enough history', which reads as a quiet market")


@pytest.mark.parametrize("mod_name,attr", [(m, a) for m, a, _ in BUDGETS],
                         ids=[b[0] for b in BUDGETS])
def test_raising_the_requirement_raises_the_fetch(mod_name, attr, monkeypatch):
    """The only version of this check that can fail.

    Equality against today's numbers passes whether the budget is derived or
    hardcoded — both are 750 right now. Move the requirement and see whether
    the budget moves: a literal will not.
    """
    monkeypatch.setattr(METER, "SIGNAL_MIN_CANDLES", METER.SIGNAL_MIN_CANDLES + 400)
    import strategy3_signal
    import vegas_reclaim
    monkeypatch.setattr(vegas_reclaim, "WARMUP", vegas_reclaim.WARMUP + 400)
    monkeypatch.setattr(strategy3_signal, "MIN_CANDLES",
                        strategy3_signal.MIN_CANDLES + 2000)
    mod = importlib.reload(importlib.import_module(mod_name))
    try:
        need = {"vegas_scan": lambda: vegas_reclaim.WARMUP + 1,
                "strategy3_scanner": lambda: strategy3_signal.MIN_CANDLES,
                }.get(mod_name, _meter_need)()
        assert getattr(mod, attr) >= need, (
            f"{mod_name}.{attr} did not follow its requirement up — it is a "
            f"hand-set literal, and the next period change disarms it silently")
    finally:
        # Reload back under the real values so later tests see the shipped
        # module, not the one built against a monkeypatched requirement.
        monkeypatch.undo()
        importlib.reload(mod)


def test_every_scanner_with_a_fetch_budget_is_listed_here():
    """A new scanner with a hand-set candle count must not slip in unlisted —
    the point of this file is the CLASS, not the three known instances."""
    import ast
    import os
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    listed = {m for m, _, _ in BUDGETS}
    unlisted = []
    for fn in sorted(os.listdir(app_dir)):
        if not fn.endswith(".py"):
            continue
        name = fn[:-3]
        if name in listed:
            continue
        with open(os.path.join(app_dir, fn), encoding="utf-8") as f:
            src = f.read()
        # a module-level CANDLES literal AND a fetch_ohlcv that uses it
        if "fetch_ohlcv" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "CANDLES"
                    for t in node.targets):
                unlisted.append(name)
    assert not unlisted, (
        f"module-level CANDLES budget not covered by this test: {unlisted}")
