"""🧩 Dashboard layout — the saved order, and what happens to it over time.

A layout is trivial data with one non-trivial property: it has to survive the
dashboard CHANGING. Every interesting failure here is silent — the page still
renders, it just quietly stops showing something.

The one that would actually bite: a saved layout must be a PREFERENCE, not a
whitelist. Treat it as a whitelist and the next panel I add is invisible to
exactly the people who used this feature, and nothing in the UI hints at why.
"""
import json

import pytest

import dashboard_layout as L


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "STORE_FILE", str(tmp_path / "layout.json"))
    return tmp_path / "layout.json"


# ── normalise: surviving a changing dashboard ───────────────────────────────
def test_a_new_panel_appears_for_someone_who_already_saved_a_layout():
    """THE bug this guards. Someone saves a layout today; I ship a panel next
    month. Dropping unknown-to-them ids would hide it from them permanently,
    and the page would look completely normal."""
    old = {"order": [c for c in L.CARD_IDS if c != "oi"], "hidden": []}
    out = L.normalise(old)
    assert "oi" in out["order"], "a newly added panel vanished for existing users"
    assert len(out["order"]) == len(L.CARD_IDS)


def test_several_new_panels_keep_their_default_order_relative_to_each_other():
    """Appending in save order would scramble a batch of new panels into
    whatever order the dict happened to iterate."""
    out = L.normalise({"order": ["news"], "hidden": []})
    rest = [c for c in out["order"] if c != "news"]
    assert rest == [c for c in L.CARD_IDS if c != "news"]


def test_a_removed_panel_is_dropped_rather_than_emitted_as_dead_css():
    out = L.normalise({"order": ["ghost", "pulse"], "hidden": ["ghost"]})
    assert "ghost" not in out["order"] and "ghost" not in out["hidden"]
    assert out["order"][0] == "pulse"


def test_a_duplicate_id_cannot_produce_two_rules_for_one_row():
    """Two `order:` rules for one selector is last-wins, so a duplicate would
    silently move the row somewhere neither entry asked for."""
    out = L.normalise({"order": ["oi", "oi", "pulse"]})
    assert out["order"].count("oi") == 1
    assert out["order"][0] == "oi"


def test_garbage_in_does_not_produce_a_broken_page():
    for junk in (None, [], "pulse", {"order": "notalist"}, {"order": [1, 2, None]}):
        out = L.normalise(junk)
        assert out["order"] == list(L.CARD_IDS)
        assert out["hidden"] == []


def test_hiding_everything_is_allowed():
    """An empty dashboard is a legitimate choice, and the edit bar lives
    outside the reorderable rows — so it is always recoverable."""
    out = L.normalise({"order": list(L.CARD_IDS), "hidden": list(L.CARD_IDS)})
    assert len(out["hidden"]) == len(L.CARD_IDS)


def test_every_card_id_has_a_title():
    assert all(L.CARD_TITLE.get(c) for c in L.CARD_IDS)
    assert len(set(L.CARD_IDS)) == len(L.CARD_IDS), "duplicate card id"


# ── the CSS ─────────────────────────────────────────────────────────────────
def test_the_style_block_orders_every_row():
    css = L.style_block(L.default_layout())
    for i, cid in enumerate(L.CARD_IDS, start=1):
        assert f'[data-card="{cid}"]{{order:{i}}}' in css


def test_a_hidden_row_is_actually_hidden():
    css = L.style_block({"order": list(L.CARD_IDS), "hidden": ["whale"]})
    assert '[data-card="whale"]{display:none}' in css
    assert '[data-card="oi"]{display:none}' not in css


def test_the_selector_only_matches_top_level_rows():
    """`.page-wrap > [data-card]` — without the child combinator a nested
    element carrying the same attribute would be reordered inside its parent."""
    css = L.style_block(L.default_layout())
    assert ".page-wrap > [data-card=" in css
    assert css.count(".page-wrap > ") == css.count("[data-card=")


def test_the_style_block_cannot_inject_markup():
    """It is written into a <style> with |safe. Ids come from CARD_IDS and
    never from the request, and normalise is what enforces that — so a hostile
    order list produces no CSS at all rather than an escaped mess."""
    payload = "</style><script>alert(1)</script>"
    css = L.style_block({"order": [payload]})
    # `>` on its own is legitimate here — it is the child combinator in every
    # selector this emits — so the property is that the PAYLOAD does not
    # survive, not that the output is angle-bracket free.
    assert payload not in css
    assert "<script" not in css and "</style" not in css
    assert css == L.style_block(L.default_layout()), \
        "an unknown id must contribute nothing at all"


# ── persistence ─────────────────────────────────────────────────────────────
def test_a_saved_layout_comes_back():
    L.save("7", {"order": ["oi", "pulse"], "hidden": ["pulse"]})
    got = L.load("7")
    assert got["order"][:2] == ["oi", "pulse"]
    assert got["hidden"] == ["pulse"]


def test_users_do_not_share_a_layout():
    L.save("1", {"order": ["oi"]})
    L.save("2", {"order": ["news"]})
    assert L.load("1")["order"][0] == "oi"
    assert L.load("2")["order"][0] == "news"


def test_saving_one_user_does_not_drop_another(store):
    L.save("1", {"order": ["oi"]})
    L.save("2", {"order": ["news"]})
    raw = json.loads(store.read_text())
    assert set(raw) == {"1", "2"}


def test_an_unknown_user_gets_the_default_not_an_error():
    assert L.load("nobody") == L.default_layout()


def test_reset_removes_only_that_user(store):
    L.save("1", {"order": ["oi"]})
    L.save("2", {"order": ["news"]})
    assert L.reset("1") == L.default_layout()
    assert L.load("1") == L.default_layout()
    assert L.load("2")["order"][0] == "news"


def test_a_corrupt_store_falls_back_instead_of_500ing(store):
    store.write_text("{ not json")
    assert L.load("1") == L.default_layout()
    L.save("1", {"order": ["oi"]})            # and recovers on the next write
    assert L.load("1")["order"][0] == "oi"


def test_save_returns_what_was_actually_stored():
    """The client repaints from the response, so returning the raw request
    would show an order the server did not keep."""
    out = L.save("1", {"order": ["ghost", "oi"], "hidden": ["ghost"]})
    assert "ghost" not in out["order"]
    assert out == L.load("1")


# ── the editor's view ───────────────────────────────────────────────────────
def test_the_editor_lists_every_card_in_the_users_order():
    cards = L.cards_for_editor({"order": ["oi", "pulse"], "hidden": ["pulse"]})
    assert [c["id"] for c in cards][:2] == ["oi", "pulse"]
    assert len(cards) == len(L.CARD_IDS)
    assert cards[1]["hidden"] is True and cards[0]["hidden"] is False
    assert all(c["title"] for c in cards)


# ── the template actually carries the ids ───────────────────────────────────
def test_every_card_id_exists_in_the_dashboard_template():
    """The ids are a contract between this module and index.html. A rename on
    either side produces CSS that matches nothing — the row simply falls to the
    bottom, with no error anywhere."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(here, "templates", "index.html"),
                encoding="utf-8").read()
    missing = [c for c in L.CARD_IDS if f'data-card="{c}"' not in html]
    assert not missing, f"no row in index.html carries: {missing}"


def test_the_template_has_no_rows_this_module_does_not_know_about():
    """The reverse gap: a tagged row missing from CARDS never appears in the
    editor and cannot be moved or hidden."""
    import os
    import re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(here, "templates", "index.html"),
                encoding="utf-8").read()
    tagged = set(re.findall(r'data-card="([a-z0-9_]+)"', html))
    assert tagged - set(L.CARD_IDS) == set(), "row in the page but not in CARDS"


# ── the route ───────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch, tmp_path):
    import app as APP
    monkeypatch.setattr(L, "STORE_FILE", str(tmp_path / "layout.json"))
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def _token(client):
    import re
    html = client.get("/").data.decode()
    m = re.search(r'var CSRF = "([^"]+)"', html)
    return m.group(1) if m else ""


def test_saving_requires_the_csrf_header(client):
    """A state change on a same-origin session. Exempting the route because
    the payload looks harmless is how an exemption habit starts."""
    r = client.post("/api/dashboard_layout", json={"order": ["oi"]})
    assert r.status_code == 400
    assert b"CSRF" in r.data


def test_the_page_hands_the_browser_a_usable_token(client):
    """If the template stopped emitting the token, every save would fail with
    a 400 that only shows up by clicking the button."""
    assert _token(client), "index.html no longer renders a CSRF token"


def test_a_layout_round_trips_through_the_api(client):
    import json as J
    h = {"X-CSRFToken": _token(client)}
    r = client.post("/api/dashboard_layout",
                    json={"order": ["oi", "radar"], "hidden": ["whale"]}, headers=h)
    assert J.loads(r.data)["ok"] is True
    got = J.loads(client.get("/api/dashboard_layout").data)
    assert got["order"][:2] == ["oi", "radar"]
    assert got["hidden"] == ["whale"]


def test_the_saved_order_is_rendered_into_the_page_not_fetched(client):
    """The whole reason this is CSS: the first paint must already be right.
    Fetching after load would show the default order and then jump."""
    h = {"X-CSRFToken": _token(client)}
    client.post("/api/dashboard_layout",
                json={"order": ["oi"], "hidden": ["whale"]}, headers=h)
    html = client.get("/").data.decode()
    assert '[data-card="oi"]{order:1}' in html
    assert '[data-card="whale"]{display:none}' in html


def test_reset_returns_the_default(client):
    import json as J
    h = {"X-CSRFToken": _token(client)}
    client.post("/api/dashboard_layout", json={"order": ["oi"]}, headers=h)
    d = J.loads(client.delete("/api/dashboard_layout", headers=h).data)
    assert d["order"] == list(L.CARD_IDS)


def test_the_layout_endpoint_needs_a_login():
    import app as APP
    anon = APP.app.test_client()
    r = anon.get("/api/dashboard_layout")
    assert r.status_code in (301, 302, 401), r.status_code


def test_a_broken_layout_store_does_not_take_the_dashboard_down(client, monkeypatch):
    """The dashboard is the home page. A layout is a nicety and must never be
    able to 500 it — which it DID once, when the template referenced a
    variable the running app.py did not yet pass."""
    monkeypatch.setattr(L, "load", lambda uid: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.get("/").status_code == 200


# ── the control cannot live inside the thing it rebuilds ────────────────────
def _dash_html():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(here, "templates", "index.html"), encoding="utf-8").read()


def test_the_save_button_is_not_inside_the_list_that_gets_rebuilt():
    """The bug the owner hit. paint() rebuilds #layout-list with
    innerHTML='' on every move, and the 儲存 button was a child of it — so the
    FIRST time you reordered anything the save button was destroyed, leaving a
    "還沒儲存" warning and no way to act on it.

    Structural, not cosmetic: a control may not live inside the container it
    controls when that container is rebuilt from scratch.
    """
    html = _dash_html()
    assert 'id="layout-save"' in html, "the save button is gone entirely"
    bar_at = html.index('id="layout-save"')
    list_at = html.index('id="layout-list"')
    assert bar_at < list_at, \
        "layout-save is rendered inside/after #layout-list — a repaint will eat it"


def test_the_save_button_lives_in_the_pinned_bar():
    """The bar is order:-1, so the button is reachable at the top of the page
    without scrolling back up after a long reorder."""
    html = _dash_html()
    bar = html[html.index('class="layout-bar"'):html.index('id="layout-list"')]
    assert 'id="layout-save"' in bar and 'id="layout-reset"' in bar


def test_paint_only_ever_rebuilds_the_row_list():
    """If paint() started clearing a wider container it would eat the buttons
    again from a different direction."""
    html = _dash_html()
    assert "LIST.innerHTML = ''" in html
    assert "BAR.innerHTML" not in html


def test_leaving_edit_mode_dirty_points_at_a_button_that_exists():
    """The old warning said 'refresh loses it' and named no action, while the
    only button that could save was hidden with the list."""
    html = _dash_html()
    assert "還沒儲存" in html
    warn_line = [l for l in html.split("\n") if "還沒儲存" in l and "HINT" in l]
    assert warn_line, "the unsaved warning is gone"
    assert "儲存版面" in warn_line[0], "the warning does not name the button"


# ── the identity a layout is filed under ────────────────────────────────────
def test_a_layout_survives_a_password_change():
    """flask_login's get_id() here is "<id>|<session_token>", and that token is
    ROTATED by new_session_token() on a password change and on log-out-
    everywhere. Keyed on the whole string, changing your password silently
    orphans your dashboard — it reverts to default with nothing to explain it,
    which reads as the feature being broken rather than as a key change.
    """
    L.save("1|oldtoken", {"order": ["oi"], "hidden": ["whale"]})
    after_rotation = L.load("1|BRAND-NEW-TOKEN-AFTER-PASSWORD-CHANGE")
    assert after_rotation["order"][0] == "oi", "layout lost when the token rotated"
    assert after_rotation["hidden"] == ["whale"]


def test_the_bare_id_and_the_long_form_are_the_same_user():
    L.save("1", {"order": ["news"]})
    assert L.load("1|whatever")["order"][0] == "news"
    L.save("1|other", {"order": ["radar"]})
    assert L.load("1")["order"][0] == "radar"


def test_a_layout_written_under_a_legacy_key_is_adopted_not_abandoned(store):
    """The one row this feature had already written before the fix."""
    store.write_text(json.dumps(
        {"1|sometoken": {"order": ["hunting"], "hidden": ["pulse"]}}))
    got = L.load("1")
    assert got["order"][0] == "hunting"
    assert got["hidden"] == ["pulse"]


def test_reset_clears_the_layout_whatever_form_the_id_arrives_in(store):
    store.write_text(json.dumps({"1|tok": {"order": ["hunting"], "hidden": []}}))
    L.reset("1")
    assert L.load("1|tok") == L.default_layout()


def test_two_users_are_still_two_users_after_the_split():
    """The split must not collapse distinct accounts — "1|x" and "2|x" are
    different people, not one person with two tokens."""
    L.save("1|a", {"order": ["oi"]})
    L.save("2|b", {"order": ["news"]})
    assert L.load("1|zzz")["order"][0] == "oi"
    assert L.load("2|zzz")["order"][0] == "news"


def test_a_corrupt_store_that_is_not_a_dict_is_survivable(store):
    store.write_text('["not", "a", "dict"]')
    assert L.load("1") == L.default_layout()
