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


# ── admin only (2026-08-14) ─────────────────────────────────────────────────
class _NonAdmin:
    is_active = True
    is_anonymous = False
    is_authenticated = True
    is_admin = False
    id = 9

    def get_id(self):
        return "9|tok"


@pytest.fixture
def as_member(monkeypatch):
    import flask_login.utils
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: _NonAdmin())
    import app as APP
    return APP.app.test_client()


def test_a_member_cannot_save_a_layout(as_member):
    """The route is what decides. Hiding the button is a suggestion — a hidden
    control plus an open endpoint is the shape of most access-control bugs, and
    the endpoint is one curl away regardless of what the page renders."""
    import re
    html = as_member.get("/").data.decode()
    m = re.search(r'var CSRF = "([^"]+)"', html)
    tok = m.group(1) if m else ""
    r = as_member.post("/api/dashboard_layout", json={"order": ["oi"]},
                       headers={"X-CSRFToken": tok})
    assert r.status_code == 403
    assert b"admin only" in r.data


def test_a_member_cannot_reset_a_layout(as_member):
    r = as_member.delete("/api/dashboard_layout")
    assert r.status_code == 403


def test_a_member_does_not_get_the_editor_markup(as_member):
    """Not merely hidden with CSS — not rendered. The save handler binds to
    these ids, so their absence removes the whole editor path for members."""
    html = as_member.get("/").data.decode()
    assert 'id="layout-toggle"' not in html
    assert 'id="layout-list"' not in html


def test_a_member_still_sees_the_dashboard(as_member):
    """Losing the editor must not lose the page."""
    assert as_member.get("/").status_code == 200


def test_a_member_still_gets_a_valid_layout(as_member):
    """The order is rendered server-side and never fetched, so a member gets
    the default ordering rather than an unstyled pile."""
    html = as_member.get("/").data.decode()
    assert '[data-card="pulse"]{order:1}' in html


def test_the_route_is_guarded_by_the_shared_admin_decorator():
    """Pinned by name: a future edit that swaps admin_required back to
    login_required to 'fix' a 403 would reopen it silently."""
    import inspect
    import app as APP
    src = inspect.getsource(APP)
    block = src[src.index('@app.route("/api/dashboard_layout"'):]
    head = block[:block.index("def api_dashboard_layout")]
    assert "@admin_required" in head
    assert "@login_required" not in head


# ── the admin publishes one layout for everyone (2026-08-15) ────────────────
def test_an_admin_save_becomes_the_layout_everyone_else_sees(tmp_path, monkeypatch):
    """Only admins may edit. Without this the owner's arrangement reached
    nobody: every non-admin sat on the built-in order permanently, with no
    editor and no route access to change it."""
    monkeypatch.setattr(L, "STORE_FILE", str(tmp_path / "l.json"))
    L.save("4|tok", {"order": ["oi", "flips"], "hidden": ["news"]}, is_admin=True)
    theirs = L.load("99|other")                    # never saved anything
    assert theirs["order"][:2] == ["oi", "flips"]
    assert theirs["hidden"] == ["news"]


def test_a_non_admin_save_changes_nothing_for_anyone_else(tmp_path, monkeypatch):
    """is_admin comes from the session; a non-admin write must stay private
    even if it somehow reaches save()."""
    monkeypatch.setattr(L, "STORE_FILE", str(tmp_path / "l.json"))
    L.save("5|tok", {"order": ["news"], "hidden": []}, is_admin=False)
    assert L.load("99|other")["order"] == list(L.CARD_IDS)   # untouched default


def test_a_personal_layout_beats_the_published_one(tmp_path, monkeypatch):
    """Other admins keep their own — the site default is a FALLBACK, not an
    override, or publishing would silently overwrite colleagues' choices."""
    monkeypatch.setattr(L, "STORE_FILE", str(tmp_path / "l.json"))
    L.save("4|a", {"order": ["oi"], "hidden": []}, is_admin=True)
    L.save("6|b", {"order": ["news"], "hidden": []}, is_admin=False)
    assert L.load("6|b")["order"][0] == "news"
    assert L.load("7|c")["order"][0] == "oi"


def test_an_admin_reset_also_withdraws_the_published_layout(tmp_path, monkeypatch):
    """Clearing only the personal copy would leave everyone else looking at a
    layout the admin just abandoned, with no control that removes it."""
    monkeypatch.setattr(L, "STORE_FILE", str(tmp_path / "l.json"))
    L.save("4|a", {"order": ["oi"], "hidden": []}, is_admin=True)
    assert L.load("99|x")["order"][0] == "oi"
    L.reset("4|a", is_admin=True)
    assert L.load("99|x")["order"] == list(L.CARD_IDS)


def test_the_site_default_key_cannot_collide_with_a_real_user(tmp_path, monkeypatch):
    """The reserved key is only safe while user ids stay numeric. get_id() is
    "<int>|<token>", so _key() yields digits — but if that ever changes, a user
    whose id keyed to the reserved string would silently become the site
    default for everyone."""
    monkeypatch.setattr(L, "STORE_FILE", str(tmp_path / "l.json"))
    assert not L.DEFAULT_KEY.isdigit()
    assert L._key(f"{L.DEFAULT_KEY}|tok") == L.DEFAULT_KEY, \
        "a crafted id maps onto the reserved key — ids are no longer numeric"


def test_the_published_layout_still_absorbs_new_cards(tmp_path, monkeypatch):
    """normalise's append rule has to survive the fallback path too, or adding
    a panel makes it invisible to every non-admin at once."""
    monkeypatch.setattr(L, "STORE_FILE", str(tmp_path / "l.json"))
    L.save("4|a", {"order": ["oi", "flips"], "hidden": []}, is_admin=True)
    got = L.load("99|x")
    assert set(got["order"]) == set(L.CARD_IDS)
    assert len(got["order"]) == len(L.CARD_IDS)


# ── 幣種 pop-out (2026-08-16) ────────────────────────────────────────────────
def test_embed_mode_drops_the_chrome_the_modal_supplies(client):
    """The pop-out shows /coin in an iframe rather than a second copy of the
    analysis — ~200 lines of JS plus its own card CSS, which is the fourth
    duplication this repo would be maintaining. An iframe cannot drift from
    the page it embeds; it just must not bring the nav and search with it."""
    full = client.get("/coin/BOME").get_data(as_text=True)
    emb = client.get("/coin/BOME?embed=1").get_data(as_text=True)
    assert "nav-standalone" in full, "the standalone page lost its nav"
    assert "nav-standalone" not in emb, "the pop-out is showing a nav inside a modal"
    assert 'class="wrap embed"' in emb
    # the analysis itself must still be there — that is the whole point
    for marker in ("coin-lwc", "wolf_chart.js", "/api/coin"):
        assert marker in emb, f"embed lost {marker}"


def test_the_dashboard_opens_coins_in_the_pop_out_not_a_new_page(client):
    """Cards link to /coin/<base>; the delegated handler turns that into the
    modal. Keeping a real href means middle-click and 'open in new tab' still
    work, and the page degrades to a normal navigation if the JS fails."""
    h = client.get("/").get_data(as_text=True)
    assert 'id="coin-modal"' in h
    assert "'/coin/' + encodeURIComponent" in h
    assert 'a[href^="/coin/"]' in h, "nothing binds the cards to the pop-out"


def test_the_pop_out_can_be_closed_every_way_a_phone_expects(client):
    """Back gesture, tap-outside, Esc, and the explicit button. The history
    entry is what makes the phone's BACK close the modal instead of throwing
    the user off the dashboard they were reading."""
    h = client.get("/").get_data(as_text=True)
    assert "history.pushState" in h and "popstate" in h
    assert "data-close" in h
    assert "Escape" in h


def test_the_pop_out_unloads_its_iframe_on_close(client):
    """An iframe left loaded keeps a second page's timers and fetches running
    behind the dashboard."""
    h = client.get("/").get_data(as_text=True)
    assert "about:blank" in h
