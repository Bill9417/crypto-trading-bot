"""🎨 Ground, surfaces and the primary action.

Reground 2026-08-19 from indigo-violet to near-black with gold as the
structural accent. Two things are worth pinning, and neither is taste:

  · depth must come from LUMINANCE, not hue. The old surfaces were violet
    tints of a violet ground, so a card, a hovered card and the page sat
    within a few points of each other and survived only because the hue
    differed — which a phone at half brightness does not preserve.
  · the primary action exists TWICE. app.css defines .btn-wolf and index.html
    redefines it inline; changing the token in one left the button people
    press most still cyan, which is exactly how this was found.
"""
import re

CSS = "static/app.css"
DASH = "templates/index.html"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _token(name, src=None):
    src = src if src is not None else _read(CSS)
    m = re.search(rf"{re.escape(name)}:\s*([^;]+);", src)
    assert m, f"{name} is not defined"
    return m.group(1).strip()


def _rgb(v):
    """#rrggbb or rgba(r,g,b,a) → (r, g, b). Alpha ignored: these all sit on
    the same near-black ground, so the composite ordering is what matters and
    the relative test below holds either way."""
    v = v.strip()
    if v.startswith("#"):
        v = v.lstrip("#")
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    n = [float(x) for x in re.findall(r"[\d.]+", v)]
    return tuple(n[:3])


def _lum(rgb):
    def c(x):
        x /= 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = (c(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_the_ground_is_near_black():
    for name in ("--wolf-slate", "--wolf-bg"):
        assert _lum(_rgb(_token(name))) < 0.02, f"{name} is not a near-black ground"
    assert _token("--wolf-slate") == _token("--wolf-bg"), \
        "the alias drifted from the token it aliases"


def test_depth_comes_from_luminance_not_hue():
    ground = _lum(_rgb(_token("--wolf-slate")))
    card = _lum(_rgb(_token("--wolf-card")))
    hi = _lum(_rgb(_token("--wolf-card-hi")))
    assert card > ground, "cards are not lighter than the page"
    assert hi > card, "a raised/hovered card is not lighter than a resting one"


def test_text_and_muted_carry_against_the_ground():
    ground = _lum(_rgb(_token("--wolf-slate")))
    for name, floor in (("--wolf-text", 12.0), ("--wolf-muted", 4.5)):
        lum = _lum(_rgb(_token(name)))
        ratio = (max(lum, ground) + 0.05) / (min(lum, ground) + 0.05)
        assert ratio >= floor, f"{name} is {ratio:.1f}:1 against the ground, want {floor}"


def test_direction_colours_are_not_the_chrome_colour():
    """Green/red mean direction and gold means chrome. If any of them collide
    the page loses the one distinction a trader reads fastest."""
    gold = _rgb(_token("--wolf-gold"))
    for name in ("--long", "--short"):
        assert _rgb(_token(name)) != gold


def test_both_copies_of_the_primary_button_agree():
    shared = _read(CSS)
    dash = _read(DASH)
    # The inline copy in index.html overrides the shared rule, so a token
    # change that misses it leaves the most-pressed button the old colour.
    for src, label in ((shared, "app.css"), (dash, "index.html")):
        m = re.search(r"\.btn-wolf\s*\{[^}]*background:\s*([^;]+);", src)
        assert m, f".btn-wolf has no background in {label}"
        assert "ffdf7a" in m.group(1).lower(), \
            f"{label}'s .btn-wolf is not the gold primary"


def test_no_page_pins_the_retired_indigo_ground():
    """#120c26 was the old ground. A literal left behind reappears as a violet
    block on a near-black page."""
    import glob
    stale = []
    for f in glob.glob("templates/*.html") + [CSS]:
        body = _read(f)
        if "#120c26" in body or "#1d1440" in body:
            stale.append(f)
    assert not stale, f"retired ground colour still hardcoded in: {stale}"
