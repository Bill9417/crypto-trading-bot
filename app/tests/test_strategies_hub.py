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
            retired=app.RETIRED_STRATEGIES,
        )


def test_template_renders_all_sections():
    html = _render(is_admin=False)
    for must in ("策略一", "策略二", "策略三", "規則", "即時狀況", "實測紀錄",
                 "精選 PREMIUM", "誠實勝率", "/funnel", "/strategy2", "已淘汰"):
        assert must in html, f"missing section: {must}"


# ── the measured record (2026-09-27) ─────────────────────────────────────────
# The hub showed rules and live status and never the verdict. Each engine now
# carries its record; every branch reads disk only and degrades to a dict.
def test_every_engine_carries_a_record():
    st = app.build_strategies_status()
    for k in ("s1", "s2", "s3", "s4"):
        assert isinstance(st[k].get("record"), dict), f"{k} has no record"
    s1 = st["s1"]["record"]
    assert s1["walk_forward"]["exp"] < 0 and s1["walk_forward"]["folds_positive"] < s1["walk_forward"]["folds"]
    assert [v["key"] for v in s1["paper"]] == list(__import__("paper_tracker").VARIANTS)
    assert "hold" in st["s2"]["record"] and "premium" in st["s2"]["record"]
    assert "intervention" in st["s3"]["record"]


def test_the_retired_list_names_the_number_that_killed_each_one():
    for r in app.RETIRED_STRATEGIES:
        assert r["when"] and r["name"] and r["name_zh"]
        assert any(ch.isdigit() for ch in r["why"]), f"{r['name']}: no measurement in the reason"
        assert r["why_zh"]
    names = " ".join(r["name"] for r in app.RETIRED_STRATEGIES)
    assert "S2 live" in names and "RSI2" in names and "dump" in names.lower()


def test_the_retired_card_renders_every_entry():
    html = _render(is_admin=False)
    for r in app.RETIRED_STRATEGIES:
        assert r["name_zh"] in html


def test_the_ledger_endpoint_is_admin_only_and_never_500s(monkeypatch):
    import strategy3_exec
    monkeypatch.setattr(strategy3_exec, "closed_pnl_summary",
                        lambda *a, **k: {"ok": False, "error": "no keys"})
    app._LEDGER_CACHE.update(ts=0.0, data=None)
    with app.app.test_client() as c:
        r = c.get("/api/strategies/ledger")
        assert r.status_code in (302, 401, 403), "the ledger is the owner's money"


def test_bybit_deeplink_is_admin_only():
    assert "/bybit" not in _render(is_admin=False)
    assert "/bybit" in _render(is_admin=True)
