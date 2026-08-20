"""🎬 The landing hero.

The cinematic treatment asked for after a TikTok of agency sites belongs on
/welcome and nowhere else: it is opened once, from a Telegram link, and decides
in five seconds whether someone joins. The dashboard got the opposite kind of
motion (see test_motion.py) for the opposite reason.

The constraints worth pinning are honesty ones. This page's entire argument is
that it publishes LOSING trades, so:
  · the backdrop must stay abstract — never a symbol, never a price, nothing
    that could be read as live data or as a prediction
  · the counters must land on the number the SERVER rendered. A landing page
    that animates to a figure of its own invention would be fabricating the
    statistic on the page selling its honesty.
"""
import os
import re

import app as APP

APP_DIR = os.path.dirname(os.path.abspath(APP.__file__))


def _read(rel):
    with open(os.path.join(APP_DIR, rel), encoding="utf-8") as f:
        return f.read()


def _page():
    c = APP.app.test_client()
    r = c.get("/welcome")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_the_counters_animate_to_the_servers_own_numbers():
    """hero.js reads the target out of the element's existing text. Nothing
    generates a figure, and nothing is passed in from JS."""
    js = _read("static/hero.js")
    seg = js[js.index("function countUp"):]
    seg = seg[:seg.index("\n    function init")]
    assert "el.textContent.trim()" in seg, "the target is not the rendered value"
    assert "roll(el, target" in seg, "it animates to something other than the target"
    # …and the page still prints the real values server-side. The COUNT is
    # asserted on the template, not the render: each stat card is conditional
    # on its data being available, so a live page legitimately shows one, two
    # or three of them.
    assert _read("templates/welcome.html").count("data-count") == 3
    h = _page()
    rendered = re.findall(r'data-count>([^<]+)<', h)
    assert rendered, "no counter rendered at all"
    for v in rendered:
        assert v.strip() and v.strip() != "0", f"a counter shipped empty: {v!r}"


def test_the_backdrop_is_decorative_and_unlabelled():
    h = _page()
    assert 'id="hero-field"' in h and 'aria-hidden="true"' in h
    js = _read("static/hero.js")
    # No symbol, no price, no axis — it must not be mistakable for market data.
    for word in ("BTC", "ETH", "USDT", "fillText", "toFixed"):
        assert word not in js, f"the backdrop draws {word} — it reads as real data"


def test_the_headline_is_complete_without_javascript():
    """kinetic() splits text that is ALREADY in the HTML. If hero.js never
    loads, the headline is plain and whole rather than missing."""
    h = _page()
    assert re.search(r"<h1 data-kinetic>\s*WOLF SCANNER\s*</h1>", h), \
        "the headline text is not server-rendered"
    css = _read("templates/welcome.html")
    # the masking styles apply only once JS adds .kinetic-on
    assert "h1.kinetic-on .kw i" in css
    assert re.search(r"h1\.kinetic-on .kw i \{[^}]*translateY\(105%\)", css, re.S)


def test_reduced_motion_skips_every_effect():
    js = _read("static/hero.js")
    assert js.count("REDUCED") >= 3, "not every effect consults it"
    seg = js[js.index("function countUp"):]
    assert "if (REDUCED || !roll) { el.textContent = target; return; }" in seg, \
        "the counter would start at 0 and never move"
    assert "if (REDUCED) { draw(); stop(); return; }" in js, \
        "the backdrop keeps animating under reduced motion"


def test_the_backdrop_stops_when_the_tab_is_hidden():
    """This page is opened from a link and left open; a canvas must not spin a
    phone's GPU behind a tab nobody is looking at."""
    js = _read("static/hero.js")
    # The LISTENER, not the word: a bare string match passes against
    # (function(){})('visibilitychange', ...).
    assert "document.addEventListener('visibilitychange'" in js
    assert "cancelAnimationFrame" in js


def test_scripts_load_in_dependency_order_on_this_public_page():
    """/welcome does not include _nav.html, so it loads the motion layer
    itself — and hero.js needs WolfMotion.rollTo."""
    h = _page()
    # Compare the SCRIPT TAGS. "hero.js" also appears in the comment above
    # them, which made an index() comparison match the prose instead.
    m = h.index('src="/static/motion.js')
    ho = h.index('src="/static/hero.js')
    assert m < ho, "hero.js runs before the motion layer it depends on"
    assert h.count("defer") >= 2, "a blocking script in front of the hero"
