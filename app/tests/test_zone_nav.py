"""🧭 專區 — the grouped navigator over the dashboard panels.

The board is sixteen panels of scroll. The strip groups them so you can jump
to the one you came for. It FILTERS existing sections and renders none of them
itself, which is the property that stops a panel appearing twice or drifting
out of step with its own tab.
"""
import os
import re

import app as APP
import dashboard_layout as D
import pytest

APP_DIR = os.path.dirname(os.path.abspath(APP.__file__))


def _dash():
    APP.app.config["TESTING"] = True
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    r = c.get("/")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def _card_js():
    with open(os.path.join(APP_DIR, "templates/index.html"), encoding="utf-8") as f:
        h = f.read()
    seg = h[h.index("/* 🧭 專區 filter."):]
    return seg[:seg.index("</script>")]


def test_every_panel_has_exactly_one_home():
    """A navigator that silently omits a panel is worse than none: the panel
    is still on the page and now looks missing."""
    D.assert_groups_cover_every_card()


def test_a_new_panel_without_a_group_fails_loudly(monkeypatch):
    monkeypatch.setattr(D, "CARD_IDS", D.CARD_IDS + ("brand_new",))
    with pytest.raises(AssertionError) as e:
        D.assert_groups_cover_every_card()
    assert "brand_new" in str(e.value)


def test_a_panel_in_two_groups_fails_loudly(monkeypatch):
    dupe = D.GROUPS + (("dupe", (("oi", "again"),)),)
    monkeypatch.setattr(D, "GROUPS", dupe)
    with pytest.raises(AssertionError) as e:
        D.assert_groups_cover_every_card()
    assert "oi" in str(e.value)


def test_the_strip_only_offers_panels_this_user_has():
    """A tab pointing at a card hidden in the layout editor lands on a blank
    screen, which reads as a broken page rather than a hidden panel."""
    lay = D.load("1")
    visible = set(lay.get("order") or []) - set(lay.get("hidden") or [])
    h = _dash()
    offered = set(re.findall(r'class="zn-tab" data-zone="([a-z0-9_]+)"', h))
    assert offered <= visible, f"tabs for hidden panels: {offered - visible}"
    assert offered, "no tabs rendered at all"


def test_the_filter_only_toggles_classes():
    """It must not render panels — that is what keeps tab and panel in step."""
    js = _card_js()
    assert "classList.toggle" in js
    assert "innerHTML" not in js, "the navigator is rendering content"


def test_an_empty_filter_falls_back_to_the_whole_board():
    """Selecting a panel that is not on the page must not blank the board."""
    js = _card_js()
    assert "if (zone && !any)" in js, "a filter that matches nothing empties the page"


def test_the_layout_editor_still_shows_every_panel():
    """You cannot re-order something the filter has hidden."""
    with open(os.path.join(APP_DIR, "templates/index.html"), encoding="utf-8") as f:
        css = f.read()
    assert re.search(r"body\.layout-editing \.page-wrap > \[data-card\] \{ display:block !important", css)


def test_the_page_links_are_links_not_filters():
    """美股專區 points at other PAGES; treating them as filters would hide the
    whole board and navigate nowhere."""
    h = _dash()
    for href in ("/us", "/tw", "/stocks", "/universe"):
        assert f'class="zn-tab zn-link" href="{href}"' in h
    js = _card_js()
    assert "if (!b) return;" in js, "the ↗ links do not fall through to navigation"
