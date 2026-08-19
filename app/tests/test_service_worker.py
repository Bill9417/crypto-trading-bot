"""📦 Service worker caching scope.

The worker's comment said it cached "only the app icons"; the condition was
startsWith('/static/'), which is every stylesheet and every script, cache-first
with no expiry and no cleanup. Versioned URLs still busted it, so it was not
what stranded the chart on 2026-08-19 — but a phone that cannot be talked out
of an old build is the worst failure mode a trading dashboard has, and nothing
here was stopping it.
"""
import re

SW = "static/sw.js"


def _src():
    with open(SW, encoding="utf-8") as f:
        return f.read()


def test_code_and_javascript_only_ever_come_from_the_network():
    src = _src()
    m = re.search(r"const CACHEABLE = (/.*?/i);", src)
    assert m, "no explicit allow-list — a path prefix is what swallowed the app's code"
    pattern = m.group(1)
    for ext in (".js", ".css", ".html", ".json"):
        assert ext.lstrip(".") not in pattern, f"{ext} is cacheable again"
    for ext in ("png", "ico", "svg", "webmanifest"):
        assert ext in pattern, f"{ext} should still be cached for installability"


def test_the_fetch_handler_requires_both_the_prefix_and_the_allow_list():
    src = _src()
    assert "startsWith('/static/') && CACHEABLE.test" in src, \
        "the prefix alone decides again"


def test_html_and_apis_are_never_cached():
    src = _src()
    # Only ONE respondWith, inside the guarded branch.
    assert src.count("respondWith") == 1


def test_old_caches_are_deleted_on_activate():
    """Bumping the name without deleting the old one leaves the previous
    build's app.css and wolf_chart.js on the device indefinitely."""
    src = _src()
    assert "caches.keys()" in src and "caches.delete" in src
    assert re.search(r"const CACHE = 'wolf-static-v[2-9]", src), \
        "the cache name must change, or activate has nothing to clear"
