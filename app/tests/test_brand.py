"""🐺 The brand mark, and the header it sits in.

The mark used to be the letter W in a gold box and an 🐺 emoji on the landing
page — a placeholder in two different fonts, neither of which was the product.
It is now one SVG that every surface renders, including the app icons, which
are generated FROM it so the launcher icon and the sidebar can never disagree.
"""
import os

import app as APP

APP_DIR = os.path.dirname(os.path.abspath(APP.__file__))


def _read(rel, binary=False):
    mode, kw = ("rb", {}) if binary else ("r", {"encoding": "utf-8"})
    with open(os.path.join(APP_DIR, rel), mode, **kw) as f:
        return f.read()


def _client():
    APP.app.config["TESTING"] = True
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def test_the_mark_is_one_path_so_it_survives_being_shrunk():
    """Stacked shapes separate at favicon size and can be clipped apart by a
    maskable crop. evenodd keeps the eyes and nose as holes in ONE path."""
    svg = _read("static/logo.svg")
    assert svg.count("<path") == 1, "the mark is more than one shape"
    assert 'fill-rule="evenodd"' in svg
    assert "viewBox" in svg and "role=\"img\"" in svg and "aria-label" in svg


def test_no_surface_still_shows_the_placeholder():
    nav = _read("templates/_nav.html")
    assert ">W<" not in nav, "the letter-W mark is still in the sidebar"
    assert "logo.svg" in nav
    wel = _read("templates/welcome.html")
    # BODY only. The 🐺 in the og:title is deliberate — that string is what
    # Telegram renders in the link unfurl, where an emoji is the point and an
    # SVG cannot go.
    body = wel[wel.index("<body"):]
    assert "🐺" not in body, "the emoji placeholder is still on the landing page"
    assert "logo.svg" in body


def test_the_app_icons_are_all_present_and_served():
    c = _client()
    for name in ("logo.svg", "icon-192.png", "icon-512.png",
                 "apple-touch-icon.png", "icon-maskable.png"):
        r = c.get("/static/" + name)
        assert r.status_code == 200, f"{name} is missing"
        assert len(r.get_data()) > 500, f"{name} looks empty"


def test_the_icons_were_regenerated_with_the_mark():
    """A logo change that leaves month-old PNGs behind ships two brands: the
    new one in the app and the old one on the home screen."""
    logo = os.path.getmtime(os.path.join(APP_DIR, "static/logo.svg"))
    for name in ("icon-192.png", "icon-512.png", "apple-touch-icon.png",
                 "icon-maskable.png"):
        ico = os.path.getmtime(os.path.join(APP_DIR, "static", name))
        assert ico >= logo - 300, f"{name} is older than the mark it should show"


def test_the_header_groups_status_with_the_title_not_the_buttons():
    h = _client().get("/").get_data(as_text=True)
    # The action group is bounded by the </header> that comes AFTER it — not
    # by whatever markup follows the header (the 專區 navigator moved in there
    # and put six unrelated buttons inside "the action group"), and not by the
    # FIRST </header> either, because _nav.html's mobile bar is itself a
    # <header> nested above this one.
    start = h.index('class="pg-head"')
    rstart = h.index('class="pg-head-r"', start)
    end = h.index("</header>", rstart)
    left = h[h.index('class="pg-head-l"', start):rstart]
    right = h[rstart:end]
    # status is information about the page…
    assert 'id="bot-status-badge"' in left and 'id="last-update"' in left
    # …and the action group holds only things you can press.
    assert right.count("<button") == 2
    assert "last-update" not in right and "bot-status-badge" not in right


def test_the_polled_badge_matches_the_rendered_one():
    """The JS replaced the pill with a Bootstrap badge, so the header changed
    shape the first time it polled."""
    h = _read("templates/index.html")
    assert 'badgeEl.innerHTML = \'<span class="dotpill' in h
    assert "badge bg-success" not in h and "badge bg-warning" not in h
