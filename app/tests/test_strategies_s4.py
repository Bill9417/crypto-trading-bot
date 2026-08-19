"""S4 on the /strategies hub.

S4 places real Bybit orders and was the only live engine missing from the page
that documents what each strategy does. Two things here are less obvious than
the panel itself:

  · the numbers come from the MODULES, not a second copy in app.py — several
    S4 thresholds moved this month, and a rules page quoting a literal lies
    the first time one changes.
  · the panel is GUARDED. p.s4 comes from app.py, which does not hot-reload
    while templates do, so an unguarded reference 500s the whole page for the
    minutes between deploy and restart. That is not hypothetical: it happened
    when this panel was added.
"""
import re

import app as APP


def _client():
    APP.app.config["TESTING"] = True
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def _page():
    r = _client().get("/strategies")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_the_hub_has_a_fourth_tab_and_panel():
    h = _page()
    assert 'data-tab="s4"' in h and 'id="panel-s4"' in h
    assert 'id="live-s4"' in h and "renderS4" in h
    assert "三大策略" not in h, "the header still counts three strategies"


def test_the_rules_quote_the_modules_not_a_second_copy(monkeypatch):
    """Equality against today's value passes whether the page READS the module
    or hardcodes the same number. So move the module and check the page moves:
    that is the only version of this test that can fail."""
    import strategy4 as S4
    import strategy4_exec as X
    monkeypatch.setattr(S4, "TP_R", 3.5)
    monkeypatch.setattr(S4, "MIN_STOP_PCT", 0.017)
    monkeypatch.setattr(S4, "MAX_STOP_PCT", 0.05)
    monkeypatch.setattr(X, "ORDER_USDT", 55.0)
    monkeypatch.setattr(X, "MAX_CONCURRENT", 6)
    p = APP._s4_params()
    assert p["tp_r"] == 3.5
    assert p["min_stop"] == 1.7
    assert p["order_usdt"] == 55.0
    assert p["max_concurrent"] == 6
    # Derived, so the page cannot state a worst case the cap contradicts.
    assert p["worst_case"] == round(6 * 55.0 * 0.05, 1)


def test_the_panel_is_guarded_against_a_stale_app_py():
    """Templates hot-reload; app.py does not. Without the guard the page 500s
    on 'dict object has no attribute s4' until someone restarts."""
    with open("templates/strategies.html", encoding="utf-8") as f:
        tpl = f.read()
    assert "p.s4 is defined" in tpl, "the panel would 500 on a stale process"
    assert re.search(r"\{%\s*if s4\s*%\}", tpl), "the panel is not conditional"
    # …and nothing inside the panel reaches past the guarded alias.
    body = tpl[tpl.index("STRATEGY 4"):]
    assert "p.s4." not in body


def test_the_status_reports_an_empty_record_rather_than_the_archive():
    """The book was reset 2026-08-19. A hub quietly showing the archived
    numbers would be reporting a strategy nobody is running."""
    st = APP.build_strategies_status()
    assert "s4" in st
    rec = st["s4"].get("record") or {}
    assert "n" in rec, "no record block at all"
    import strategy4_outcomes as O
    live = ((O.web_view() or {}).get("stats") or {}).get("all") or {}
    assert rec["n"] == live.get("n", 0), "the hub and the store disagree"


def test_it_says_why_nothing_fired():
    """'0 signals' and 'the scan is broken' look identical without the gate
    breakdown — an ambiguity that has cost this repo three days of silence."""
    st = APP.build_strategies_status()["s4"]
    assert "rejected" in st and "checked" in st
    with open("templates/strategies.html", encoding="utf-8") as f:
        tpl = f.read()
    assert "卡在" in tpl and "rejZH" in tpl


def test_rejection_reasons_are_translated_but_never_swallowed():
    with open("templates/strategies.html", encoding="utf-8") as f:
        tpl = f.read()
    seg = tpl[tpl.index("var S4_REJ"):tpl.index("function renderS4")]
    assert "'no short triangle'" in seg
    # An unknown gate must show as ITSELF rather than disappearing.
    assert "return k;" in seg


def test_a_broken_s4_does_not_blank_the_other_strategies(monkeypatch):
    monkeypatch.setattr(APP, "_s4_status",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    st = APP.build_strategies_status()
    assert st["s4"].get("error"), "the failure was swallowed silently"
    assert st["s1"] and st["s3"] is not None, "one engine took the others down"
