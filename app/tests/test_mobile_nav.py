"""📱 The mobile top bar — the only way around the site on a phone.

body.has-sidebar has reserved 52px for a top bar since the 2026-08-09 sidebar
redesign, and nothing ever painted one: the off-canvas drawer left a single
unlabelled ☰ floating on the page background. Reported 2026-08-19 from the
幣種分析 page — "I can't go back to dashboard, no buttons or settings" — and it
was true of every authenticated page, not just that one.

These pin the three things one icon could not say: where you are, how to get
home, and how to reach everything else.
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


def _html(path):
    r = _client().get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}"
    return r.get_data(as_text=True)


def test_every_authenticated_page_has_the_bar_and_a_way_home():
    for path in ("/coin", "/market", "/s4", "/reality", "/universe"):
        h = _html(path)
        assert 'class="wsb-top"' in h, f"{path} has no mobile top bar"
        assert 'class="wsb-top-home"' in h, f"{path} has no way back to the dashboard"
        assert 'id="wsb-burger"' in h, f"{path} has no drawer toggle"


def test_the_bar_says_which_page_you_are_on():
    """Scoped to the bar's own span. Searching the whole page for the label
    matches the SIDEBAR item of the same name, so the assertion passed with
    the title hardcoded to a constant — it could not fail."""
    def title(path):
        h = _html(path)
        m = re.search(r'class="wsb-top-title">([^<]*)<', h)
        assert m, f"{path} has no title element in the bar"
        return m.group(1).strip()

    assert title("/coin") == "幣種分析"
    assert title("/s4") == "S4 Radar"
    assert title("/market") == "Market Pulse"


def test_the_dashboard_does_not_link_to_itself():
    """A control that goes nowhere is the one people learn to ignore."""
    assert 'class="wsb-top-home"' not in _html("/")
    assert 'class="wsb-top"' in _html("/")          # …but the bar is still there


def test_no_nav_destination_falls_back_to_the_generic_title():
    """A page added to the sidebar without a title entry silently shows
    'Wolf Scanner' on mobile — the drift this test exists to catch."""
    with open("templates/_nav.html", encoding="utf-8") as f:
        nav = f.read()
    endpoints = set(re.findall(r"request\.endpoint == '([a-z0-9_]+)'", nav))
    endpoints |= {e for grp in re.findall(r"request\.endpoint in \(([^)]+)\)", nav)
                  for e in re.findall(r"'([a-z0-9_]+)'", grp)}
    # Scope to the dict, then take EVERY key — several sit on one line, and a
    # per-line regex quietly saw only the first of each.
    block = nav[nav.index("_wsb_titles = {"):]
    block = block[:block.index("} %}")]
    titled = set(re.findall(r"'([a-z0-9_]+)'\s*:", block))
    missing = sorted(endpoints - titled)
    assert not missing, f"sidebar destinations with no mobile title: {missing}"


def test_the_embedded_pop_out_still_has_no_chrome():
    """The dashboard shows /coin?embed=1 in an iframe; a nav inside that modal
    would be a second navigation stacked on the first."""
    h = _html("/coin?embed=1")
    assert 'class="wsb-top"' not in h and 'id="wsb-burger"' not in h


def test_the_css_only_shows_the_bar_on_small_screens():
    with open("static/app.css", encoding="utf-8") as f:
        css = f.read()
    assert re.search(r"\.wsb-top\s*\{\s*display:\s*none", css), \
        "the bar must be hidden by default or it doubles the desktop sidebar"
    mobile = css[css.index("@media (max-width: 1020px)"):]
    mobile = mobile[:mobile.index("@media (prefers-reduced-motion")]
    assert ".wsb-top {" in mobile and "display: flex" in mobile
    # The drawer must sit BELOW the bar, or the bar covers its brand…
    assert re.search(r"\.wsb\s*\{\s*top:\s*52px", mobile)
    # …and the scrim must not cover the bar, or ☰→✕ becomes untappable.
    assert re.search(r"\.wsb-scrim\s*\{[^}]*inset:\s*52px", mobile, re.S)
