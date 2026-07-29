"""🧩 Front-end asset wiring — the failure mode here is silent.

wolfPoll() lives in static/poll.js, which is pulled in by _nav.html. A page that
calls wolfPoll() without including the nav throws ReferenceError at load and
simply stops refreshing — no server error, nothing in the logs, and the page
still renders. Only a user noticing stale numbers would catch it.

Same shape of bug for the ?v= cache-bust: a static file linked without it is
pinned in browsers for 7 days by SEND_FILE_MAX_AGE_DEFAULT, which has already
shipped an unstyled page to users once.
"""
import re
from pathlib import Path

import app as APP

TEMPLATES = Path(APP.__file__).resolve().parent / "templates"
STATIC = Path(APP.__file__).resolve().parent / "static"


def _pages():
    return sorted(p for p in TEMPLATES.glob("*.html") if not p.name.startswith("_"))


def test_every_page_using_wolfpoll_pulls_in_the_nav_that_defines_it():
    offenders = []
    for page in _pages():
        src = page.read_text(encoding="utf-8")
        if "wolfPoll" not in src:
            continue
        if "_nav.html" not in src:
            offenders.append(page.name)
    assert not offenders, f"call wolfPoll() but never load poll.js: {offenders}"


def test_nav_loads_poll_js():
    src = (TEMPLATES / "_nav.html").read_text(encoding="utf-8")
    assert "poll.js" in src
    assert (STATIC / "poll.js").exists()


def test_poll_js_exposes_the_helper_and_a_stop_handle():
    src = (STATIC / "poll.js").read_text(encoding="utf-8")
    assert "window.wolfPoll" in src
    assert "visibilitychange" in src
    assert "stop:" in src, "callers store the handle and call .stop()"


def test_no_page_clears_a_wolfpoll_handle_with_clearinterval():
    """wolfPoll returns an object, not a numeric timer id. clearInterval() on it
    is a no-op that leaves the poller running AND registered for catch-up."""
    bad = []
    for page in _pages():
        src = page.read_text(encoding="utf-8")
        for m in re.finditer(r"(\w+)\s*=\s*wolfPoll\(", src):
            handle = m.group(1)
            if re.search(rf"clearInterval\(\s*{re.escape(handle)}\s*\)", src):
                bad.append(f"{page.name}:{handle}")
    assert not bad, f"clearInterval() used on a wolfPoll handle: {bad}"


def test_static_assets_are_cache_busted():
    """Anything linked from a template must carry ?v= or a 7-day cache pins it."""
    missing = []
    for page in list(_pages()) + [TEMPLATES / "_nav.html"]:
        src = page.read_text(encoding="utf-8")
        for m in re.finditer(r"""(?:href|src)=["']([^"']*static[^"']*\.(?:css|js))([^"']*)["']""", src):
            path, query = m.group(1), m.group(2)
            if "?v=" not in path + query:
                missing.append(f"{page.name}: {path}")
    assert not missing, f"static assets linked without ?v= cache-bust: {missing}"


def test_asset_ver_changes_when_static_files_do():
    """A soft guard: ASSET_VER should look like a date, so bumping it is
    obviously required rather than an arbitrary token someone forgets."""
    assert re.fullmatch(r"\d{8}", APP.ASSET_VER), APP.ASSET_VER
