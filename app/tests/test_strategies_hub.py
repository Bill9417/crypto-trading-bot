"""📊 Strategies hub — the single /strategies page that concludes S1/S2/S3.

Covers the 2026-07-17 addition:
  • _strategies_params — live rule NUMBERS mirror config (so retuning a
    threshold updates the page, never drifts into a stale hardcoded value)
  • build_strategies_status — consolidated live snapshot, failure-safe per branch
  • strategies.html renders with all three tabs + the誠實 win-rate disclaimer,
    and the Bybit deep-link is admin-only
"""
import app
import config
import strategy2_meter


def test_params_mirror_live_config():
    p = app._strategies_params()
    assert p["s1"]["lights_long"] == config.MIN_LIGHTS_FOR_ENTRY
    assert p["s1"]["lights_short"] == config.MIN_LIGHTS_SHORT
    assert p["s1"]["leverage"] == config.LEVERAGE
    assert p["s2"]["long_th"] == strategy2_meter.LONG_THRESHOLD
    assert p["s2"]["premium_score"] == config.STRATEGY2_PREMIUM_MIN_SCORE
    assert p["s3"]["adx_th"] == config.STRATEGY3_ADX_TH
    # every S3 symbol carries an engine + timeframe
    for s in p["s3"]["symbols"]:
        assert s["engine"] in ("flagflip", "occ") and s["tf"]


def test_status_has_every_strategy_and_is_failsafe():
    """S4 joined the hub 2026-08-19. The exact-set assertion is kept exact on
    purpose — it is what caught the addition, and a strategy silently missing
    from the page that documents them is the failure worth catching."""
    st = app.build_strategies_status()
    assert set(st.keys()) == {"s1", "s2", "s3", "s4"}
    # each branch is a dict (never a bare exception bubbling up)
    assert isinstance(st["s1"], dict) and isinstance(st["s2"], dict)
    assert isinstance(st["s3"].get("symbols"), list)
    assert isinstance(st["s4"], dict)
    # must be JSON-serialisable for |tojson / the API
    import json
    json.dumps(app._json_safe(st))


def test_status_s2_ranks_premium_by_conviction():
    st = app.build_strategies_status()
    prem = st["s2"].get("premium") or []
    convs = [s["conv"] for s in prem]
    assert convs == sorted(convs, reverse=True)     # highest conviction first
    for s in prem:
        assert s["conv"] >= config.STRATEGY2_PREMIUM_MIN_SCORE  # premium gate


def _render(is_admin):
    class U:
        pass
    u = U()
    u.is_admin = is_admin
    with app.app.test_request_context("/strategies"):
        return app.render_template(
            "strategies.html", user=u,
            params=app._strategies_params(),
            status=app._json_safe(app.build_strategies_status()),
        )


def test_template_renders_all_sections():
    html = _render(is_admin=False)
    for must in ("策略一", "策略二", "策略三", "規則", "即時狀況",
                 "精選 PREMIUM", "誠實勝率", "/funnel", "/strategy2"):
        assert must in html, f"missing section: {must}"


def test_bybit_deeplink_is_admin_only():
    assert "/bybit" not in _render(is_admin=False)
    assert "/bybit" in _render(is_admin=True)
