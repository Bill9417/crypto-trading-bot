"""A shadow trade must carry a MEASURED divergence reading, not the default.

evaluate() returns early when the plan is under the fee floor, and that return
sat before divergence_scan() ran — so every shadow was stored with the
initialised div_sources=[], which the outcome book then recorded as fact. On
the real record that produced 178 shadows all claiming "no divergence fired"
against 136 live trades that all had one: a perfect split, which is never what
a measurement looks like.

Mutation-checked: dropping the divergence_scan line from the shadow branch
turns this red.
"""
import strategy4 as S4


def _candles(n=760):
    """A slow uptrend with a pullback into support — the shape S4 looks for."""
    out, px, t = [], 100.0, 1_700_000_000_000
    for i in range(n):
        px *= 1.0009 if i % 7 else 0.9975
        hi, lo = px * 1.004, px * 0.996
        out.append([t + i * 900_000, px * 0.999, hi, lo, px, 1000 + (i % 13) * 90])
    return out


def test_shadow_carries_a_real_divergence_reading(monkeypatch):
    """Drive the fee-floor branch DIRECTLY. An earlier version of this test
    built a synthetic series and hoped it would arrive here; it died at the
    triangle and skipped, which is a test that cannot fail."""
    ohlcv = _candles()
    monkeypatch.setattr(S4, "REQUIRE_DIVERGENCE", True)
    # Walk the earlier gates deterministically instead of hoping for them.
    import strategy2_meter
    monkeypatch.setattr(strategy2_meter, "compute_signal",
                        lambda *a, **k: {"signal": "long", "score": 70})
    monkeypatch.setattr(S4, "ema_trending", lambda *a, **k: (True, 0.5))
    monkeypatch.setattr(S4, "structure_level",
                        lambda *a, **k: {"level": ohlcv[-1][4] * 0.99, "bars_ago": 8})
    # …and reject the plan on cost, which is exactly what makes a shadow.
    monkeypatch.setattr(S4, "plan", lambda *a, **k: {
        "rejected": True, "stop_pct": 0.4, "floor_pct": 1.0})

    seen = {}
    real = S4.divergence_scan
    monkeypatch.setattr(S4, "divergence_scan",
                        lambda *a, **k: (seen.setdefault("called", True), real(*a, **k))[1])

    out = S4.evaluate(ohlcv, side="long")
    assert out.get("shadow") is True, f"expected the shadow branch, got: {out.get('reason')}"
    assert seen.get("called"), \
        "shadow returned before divergence_scan ran — div_sources=[] is a fabricated zero"
    assert "div_sources" in out and isinstance(out["div_sources"], list)
    assert out.get("cvd") != {} and "cvd" in out, "shadow must carry the flow reading too"


def test_cvd_flow_abstains_rather_than_reporting_flat():
    # Too few bars is NOT 'flow was flat'. An empty dict says 'not measured',
    # which is the distinction this repo has now gotten wrong five times.
    assert S4.cvd_flow(_candles(40)) == {}
    full = S4.cvd_flow(_candles())
    assert set(full) == {"norm", "slope", "rising"}


def test_cvd_is_recorded_but_never_gates():
    """The gate list must not consult the flow reading. Measured null:
    lift -0.475R looked real until Spearman (rho -0.037, p 0.77) and the
    score confound killed it."""
    import ast
    import inspect
    src = inspect.getsource(S4.evaluate)
    tree = ast.parse(src.lstrip())
    # Every `return out` guarded by a condition mentioning cvd would be a gate.
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            cond = ast.dump(node.test)
            assert "cvd" not in cond.lower(), \
                "CVD became a gate — it was measured and does not survive"
