"""🔘 壓力/支撐 visibility toggle on the 幣種分析 chart.

Asked for 2026-08-19. The awkward part is not the button — it is that the
chart is REBUILT on every symbol (render() replaces the container), so both
the preference and the listener have to outlive the element.
"""
import app as APP


def _client():
    APP.app.config["TESTING"] = True
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def _page():
    r = _client().get("/coin/BTC")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _chart_js():
    with open("static/wolf_chart.js", encoding="utf-8") as f:
        return f.read()


def test_the_button_is_in_the_chart_header():
    h = _page()
    assert 'id="lv-toggle"' in h
    # aria-pressed, not just a colour: the state has to be readable by
    # something other than an eye.
    assert 'aria-pressed' in h


def test_the_choice_survives_a_symbol_change_and_a_reload():
    h = _page()
    assert "localStorage.getItem(LV_KEY)" in h and "localStorage.setItem(LV_KEY" in h
    # Default is SHOWN: absence of a stored value must not hide the lines.
    assert "!== '0'" in h
    # Applied BEFORE the first draw, or the lines flash on and then vanish.
    assert "_chart.levelsOn = _levelsOn;" in h


def test_a_refusing_localstorage_does_not_break_the_page():
    """Private mode and a full quota both throw on read AND on write."""
    h = _page()
    seg = h[h.index("var LV_KEY"):h.index("function mountChart")]
    assert seg.count("try {") >= 2 and seg.count("catch") >= 2


def test_the_listener_is_delegated_not_bound_to_a_replaced_node():
    h = _page()
    assert "closest('#lv-toggle')" in h, \
        "binding directly leaves the handler on a detached node after a re-render"


def test_hiding_keeps_the_levels_so_showing_needs_no_refetch():
    js = _chart_js()
    assert "if (levels !== undefined) this.levels = levels || [];" in js, \
        "setLevels() with no argument must not wipe the remembered set"
    assert "this.levelsOn === false ? [] : (this.levels || [])" in js


def test_showlevels_returns_the_state_it_set():
    js = _chart_js()
    seg = js[js.index("WolfChart.prototype.showLevels"):]
    seg = seg[:seg.index("};")]
    assert "return this.levelsOn;" in seg, \
        "a caller labelling its button needs one source of truth, not a second copy"


def test_the_legend_agrees_with_the_chart():
    """Leaving 壓力/支撐 lit while the lines are hidden describes something
    that is not drawn — the same class of bug as the Vegas legend entry."""
    h = _page()
    # BOTH items, not "at least one" — 壓力 and 支撐 are hidden together, and a
    # check for one of them passes while the other stays lit.
    assert h.count("lg lg-lv") == 2, "壓力 and 支撐 must both be marked"
    assert ".lwc-legend.lv-off .lg-lv" in h
    # Toggled in BOTH places: the click handler, and the legend rebuild that
    # happens on every symbol load.
    assert h.count("lv-off") >= 3
