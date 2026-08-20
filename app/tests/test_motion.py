"""✨ Motion layer — arrival and change only, and never at the cost of content.

Asked for after a TikTok of agency landing pages. Most of that vocabulary is
wrong here: those pages are read once for five seconds, this one is read
twenty times a day, and motion that moves a number while someone reads it
costs them the reading.

The rule these tests exist to hold: a decoration must never be able to hide
content. A full-width dashboard panel DID stay at opacity 0 after scrolling to
the bottom during development — IntersectionObserver does not fire for every
way an element can come into view.
"""
import os
import re

import app as APP

APP_DIR = os.path.dirname(os.path.abspath(APP.__file__))


def _read(rel):
    with open(os.path.join(APP_DIR, rel), encoding="utf-8") as f:
        return f.read()


def test_the_end_state_is_the_default_so_a_dead_script_shows_everything():
    """.will-reveal is what HIDES. If motion.js never loads, no element is
    ever given that class and the page is simply already in place — the
    opposite arrangement is one broken script away from a blank dashboard."""
    css = _read("static/app.css")
    assert ".will-reveal {" in css and "opacity: 0" in css
    # nothing in the SERVER html may ship the hiding class
    for tpl in ("templates/index.html", "templates/market.html", "templates/strategies.html"):
        assert "will-reveal" not in _read(tpl), f"{tpl} hides content before JS runs"


def test_there_is_a_sweep_that_cannot_leave_content_hidden():
    js = _read("static/motion.js")
    assert "setInterval" in js and "getBoundingClientRect" in js
    # zero-box elements (display:none cards) are revealed rather than left
    # pending, so they cannot pop in wrong later
    assert "if (!r.width && !r.height)" in js
    # …and the sweep stops instead of polling forever
    assert "clearInterval" in js


def test_reduced_motion_turns_it_off_rather_than_shortening_it():
    js = _read("static/motion.js")
    css = _read("static/app.css")
    # The FLAG has to come from the media query, not from a constant. Checking
    # only that the string appears passes against `REDUCED = false && ...`.
    assert re.search(r"var REDUCED = global\.matchMedia &&\s*\n?\s*global\.matchMedia\(", js), \
        "the reduced-motion flag is not read from the media query"
    assert js.count("if (REDUCED)") >= 2, "reveal and roll must both honour it"
    assert "prefers-reduced-motion" in css
    block = css[css.index("@media (prefers-reduced-motion: reduce) {"):]
    block = block[:block.index("}\n\n")]
    assert "opacity: 1 !important" in block and "transition: none !important" in block


def test_the_number_roll_keeps_the_units():
    """A roll that drops the % or the $ changed the meaning of the figure."""
    js = _read("static/motion.js")
    m = re.search(r"var NUM_RE = (/.*?/);", js)
    assert m, "no number pattern"
    pat = m.group(1)
    assert pat.count("(") >= 3, "prefix/number/suffix are not captured separately"
    assert "to[1] +" in js and "+ to[3]" in js, "prefix and suffix are not re-applied"


def test_motion_is_loaded_once_for_every_authenticated_page():
    nav = _read("templates/_nav.html")
    assert "motion.js" in nav and "defer" in nav
    # loaded from the shared partial, not copied per page
    for tpl in ("templates/index.html", "templates/market.html"):
        assert "motion.js" not in _read(tpl), "a second copy will drift"


def test_the_market_kpis_are_wired_to_roll():
    h = _read("templates/market.html")
    assert h.count("data-roll") >= 5
