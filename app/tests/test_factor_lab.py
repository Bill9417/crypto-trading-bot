"""🧪 factor_lab — the guarantee is CALIBRATION, not features.

This tool exists to stop the mistake that has already cost this project
twice (S1 walk-forward negative in 2026-06 and again 2026-07). So the tests
that matter are not "does it compute RSI" — they are:

  · on data with provably NO edge, does it say so?
  · on data with a planted edge, does it still find it?
  · can a future edit reintroduce lookahead without a test screaming?

A tool that answers "edge!" on a random walk is worse than no tool, because
it launders noise into confidence.
"""
import numpy as np
import pandas as pd
import pytest

import factor_lab as FL


def _random_walk(seed, n=5000, vol=0.004):
    r = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(r.normal(0, vol, n)))
    return [[i * 3600000, c[i], c[i] * 1.002, c[i] * 0.998, c[i],
             abs(r.normal(1000, 300))] for i in range(n)]


def _planted_edge(seed, n=5000):
    """Oversold predicts a higher forward return, by construction."""
    r = np.random.default_rng(seed)
    c, st = [100.0], 50.0
    for _ in range(1, n):
        st = 0.9 * st + 0.1 * (50 + r.normal(0, 20))
        c.append(c[-1] * np.exp((50 - st) * 0.00035 + r.normal(0, 0.004)))
    return [[i * 3600000, c[i], c[i] * 1.002, c[i] * 0.998, c[i], 1000.0]
            for i in range(n)]


# ── the two calibration guarantees ───────────────────────────────────────────
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_random_walk_is_never_called_an_edge(seed):
    """The headline guarantee. A random walk has no edge; if this ever passes,
    the lab is manufacturing exactly the false confidence it exists to stop."""
    res = FL.analyse({"N": FL.panel_for("N", _random_walk(seed))})
    ok, reasons = FL.verdict(res["walk_forward"], res["null"])
    assert not ok, f"random walk seed {seed} was called an edge: {reasons}"


def test_a_real_edge_is_still_found():
    """The other side: a lab that rejects everything is also useless."""
    res = FL.analyse({"E": FL.panel_for("E", _planted_edge(0))})
    ok, _ = FL.verdict(res["walk_forward"], res["null"])
    assert ok
    assert res["walk_forward"]["oos_edge_R"] > 0.5


# ── no lookahead ─────────────────────────────────────────────────────────────
def test_target_uses_only_the_future_and_factors_only_the_past():
    df = pd.DataFrame({"ts": range(100), "open": 1.0, "high": 2.0, "low": 0.5,
                       "close": np.arange(1, 101, dtype=float), "volume": 1.0})
    fx, atr = FL.build_factors(df)
    y = FL.forward_target(df, atr, horizon=5)
    # target at i is built from close[i+5] — verify against a hand calculation
    i = 10
    expect = (df["close"][i + 5] / df["close"][i] - 1) / (atr[i] / df["close"][i])
    assert y[i] == pytest.approx(expect)
    # ...and the last `horizon` rows cannot be known yet
    assert y.tail(5).isna().all()


def test_incomplete_future_rows_are_dropped():
    """Keeping them would score a trade whose outcome is only partly known."""
    oh = _random_walk(0, n=1200)
    p = FL.panel_for("X", oh, horizon=FL.HORIZON_BARS)
    assert len(p) <= len(oh) - FL.HORIZON_BARS
    assert p["y"].notna().all()


def test_a_factor_cannot_see_its_own_bar_future():
    """Shifting price AFTER bar i must not change the factors at bar i."""
    n = 400
    base = _random_walk(3, n=n)
    tampered = [row[:] for row in base]
    for r in tampered[300:]:
        r[4] *= 3.0                       # violently change the future only
    f1, _ = FL.build_factors(pd.DataFrame(base, columns=["ts", "open", "high", "low", "close", "volume"]).astype(float))
    f2, _ = FL.build_factors(pd.DataFrame(tampered, columns=["ts", "open", "high", "low", "close", "volume"]).astype(float))
    pd.testing.assert_frame_equal(f1.iloc[:299], f2.iloc[:299])


# ── the metric itself ────────────────────────────────────────────────────────
def test_edge_is_picked_minus_unpicked_not_raw_return():
    """A long-only subset of an up-trending market shows a positive mean
    return with NO skill at all. Only picked-minus-unpicked isolates the
    selection; an earlier version measured raw return and called drifting
    random walks an edge in every fold."""
    n = 4000
    r = np.random.default_rng(1)
    # strong upward drift, no relationship between factors and the future
    c = 100 * np.exp(np.cumsum(r.normal(0.0015, 0.004, n)))
    oh = [[i * 3600000, c[i], c[i] * 1.002, c[i] * 0.998, c[i], 1000.0] for i in range(n)]
    wf = FL.walk_forward(FL.panel_for("UP", oh))
    assert wf["folds"], "no fold scored"
    # the raw return is dragged up by the drift...
    assert np.mean([f["mean_R"] for f in wf["folds"]]) > -1
    # ...while the edge stays near zero, which is the honest answer
    assert abs(wf["oos_edge_R"]) < 1.5


def test_empirical_p_is_never_zero():
    """With a finite null you cannot honestly claim p = 0."""
    null = {"runs": 10, "dist": np.zeros(10)}
    assert FL.empirical_p(99.0, null) > 0


def test_verdict_needs_every_fold_not_a_majority():
    strong_null = {"runs": 100, "dist": np.full(100, -9.0), "p95": -9.0}
    ok, _ = FL.verdict({"oos_edge_R": 1.0, "oos_positive_folds": 3,
                        "n_folds_scored": 4}, strong_null)
    assert not ok, "3 of 4 folds must not pass — that is coin-flip territory"
    ok, _ = FL.verdict({"oos_edge_R": 1.0, "oos_positive_folds": 4,
                        "n_folds_scored": 4}, strong_null)
    assert ok


def test_no_null_means_no_verdict():
    ok, reasons = FL.verdict({"oos_edge_R": 5.0, "oos_positive_folds": 4,
                              "n_folds_scored": 4}, {})
    assert not ok and "null" in reasons[0].lower()
