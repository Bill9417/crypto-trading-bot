"""📱 Phone layout — the things that only fail at 390px, in a real browser.

Every defect this file pins was invisible in the source and obvious in the
rendered page:

  · a CSS rule that loses on specificity or source order is indistinguishable
    from a rule nobody wrote. `.smc-zone-pill` lost to `.cell-smc .smc-zone-pill`
    and 211 pills stayed at 9.3px; `.sect-row` lost to a SECOND `.sect-row`
    defined 1,500 lines later for /market, so two separate attempts to widen
    the sector columns changed nothing at all.
  · enlarging text without enlarging the box it sits in moves the problem
    rather than fixing it — the watchlist badge overflowed its column and the
    description ran underneath it; every sector value overflowed its cell.

So these assertions are made against computed styles and measured geometry,
never against the stylesheet text. Reading the CSS is what missed all of it.
"""
import shutil
import threading
import time

import pytest

PHONE = {"width": 390, "height": 844}
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="browser tests need a JS runtime")


@pytest.fixture(scope="module")
def page():
    pw = pytest.importorskip("playwright.sync_api")
    import app as APP

    APP.app.config["TESTING"] = True
    port = 5399
    threading.Thread(
        target=lambda: APP.app.run(port=port, threaded=True, use_reloader=False),
        daemon=True).start()
    time.sleep(2.5)

    client = APP.app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True

    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport=PHONE, is_mobile=True, has_touch=True)
        for (_d, _p, name), ck in client._cookies.items():
            pg.context.add_cookies([{"name": name, "value": ck.value,
                                     "domain": "127.0.0.1", "path": "/"}])
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        pg.wait_for_timeout(5000)
        pg.errors = errors
        yield pg
        browser.close()


def test_the_page_does_not_scroll_sideways(page):
    assert not page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")


def test_no_javascript_errors(page):
    assert not page.errors, page.errors


def test_nothing_is_clipped_inside_its_cell(page):
    """scrollWidth > clientWidth means the text is wider than the box holding
    it. Every sector value was in that state at 46px, twice, while the rule
    meant to fix it sat in the file doing nothing."""
    clipped = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll(
          '#sector-body .sect-val, #sector-body .sect-name, .watch-eng, .watch-base'
        ).forEach(e => {
          if (e.scrollWidth > e.clientWidth + 1)
            out.push((e.className||'') + ': ' + e.textContent.trim().slice(0, 24));
        });
        return out; }""")
    assert not clipped, f"text wider than its cell: {clipped}"


def test_nothing_in_a_watchlist_row_overlaps_anything_else(page):
    """The badge and the description were columns 3 and 4 of a four-column
    grid. Enlarging the badge pushed it over its neighbour — the overlap in
    the 2026-08-21 screenshot.

    Checked against EVERY pair in the row, not just badge-vs-description. The
    first version of this test only compared those two and missed a mutation
    that put the four-column grid back: with `justify-self:end` the oversized
    badge overflows LEFTWARD into the coin name instead, which is the same
    defect pointing the other way. The invariant is that no two cells in a row
    occupy the same pixels — nothing narrower survives contact with a layout
    that can break in either direction.
    """
    bad = page.evaluate("""() => {
        const out = [];
        [...document.querySelectorAll('.watch-row')].slice(0, 8).forEach((row, ri) => {
          const cells = [...row.children]
            .filter(c => c.getBoundingClientRect().width > 0)
            .map(c => ({n: c.className, r: c.getBoundingClientRect()}));
          for (let i = 0; i < cells.length; i++)
            for (let j = i + 1; j < cells.length; j++) {
              const a = cells[i].r, b = cells[j].r;
              if (a.right > b.left + 1 && a.left < b.right - 1 &&
                  a.bottom > b.top + 1 && a.top < b.bottom - 1)
                out.push(`row ${ri}: ${cells[i].n} over ${cells[j].n}`);
            }
        });
        return out; }""")
    assert not bad, f"cells overlap: {bad[:5]}"


def test_the_sector_rows_open_to_show_their_coins(page):
    """A median over N coins is a summary of a bucket you cannot open. Asked
    for 2026-08-21."""
    page.eval_on_selector('[data-card="sectors"]', "e => e.scrollIntoView()")
    page.wait_for_timeout(300)
    before = page.eval_on_selector_all(
        "#sector-body .sect-row .sect-coin",
        "e => e.filter(x => x.offsetParent !== null).length")
    assert before == 0, "members were visible before anything was tapped"

    page.eval_on_selector_all("#sector-body .sect-row", "e => e[0].click()")
    page.wait_for_timeout(300)
    opened = page.eval_on_selector_all(
        "#sector-body .sect-row.open .sect-coin",
        "e => e.map(x => x.getAttribute('href'))")
    assert opened, "tapping a sector showed no coins"
    assert all(h.startswith("/coin/") for h in opened), opened


def test_tapping_a_coin_does_not_collapse_the_sector(page):
    """The row toggles on click; a link inside it must reach the coin page
    rather than closing the list under the thumb."""
    page.eval_on_selector_all("#sector-body .sect-row", """e => {
        e.forEach(r => r.classList.remove('open'));
        e[0].click(); }""")
    page.wait_for_timeout(250)
    page.eval_on_selector("#sector-body .sect-row.open .sect-coin",
                          "e => e.addEventListener('click', ev => ev.preventDefault())")
    page.eval_on_selector("#sector-body .sect-row.open .sect-coin", "e => e.click()")
    page.wait_for_timeout(250)
    still = page.eval_on_selector_all("#sector-body .sect-row.open", "e => e.length")
    assert still > 0, "tapping a coin collapsed its sector"


def test_the_navigator_is_one_scrollable_strip(page):
    """It used to wrap into three labelled rows and cost 183px before any
    data. One strip that scrolls sideways is 54px."""
    nav = page.evaluate("""() => {
        const n = document.getElementById('zones-nav');
        if (!n) return null;
        return {h: Math.round(n.getBoundingClientRect().height),
                scrolls: n.scrollWidth > n.clientWidth + 1}; }""")
    assert nav, "the 專區 navigator is gone"
    assert nav["h"] <= 80, f"navigator is {nav['h']}px — it is wrapping again"
    assert nav["scrolls"], "the strip no longer scrolls, so tabs are unreachable"


def test_text_is_readable(page):
    """Roughly 1,000 elements sat under 11px before this pass — 212 at 8.6px,
    207 at 8.8px. 57 remain under 10px: a scattered tail of one-off labels,
    each 2-14 elements, in cards this pass did not restyle.

    So the bound is a REGRESSION bound, not a claim of perfection. It sits
    above what is there today and far below what was there before, which
    makes it fail if the phone rules stop applying — the failure that
    actually happened three times while writing them, because a rule that
    loses on specificity is invisible in the source."""
    tiny = page.evaluate("""() => {
        let n = 0;
        document.querySelectorAll('*').forEach(e => {
          if (!e.children.length && (e.textContent || '').trim()) {
            const fs = parseFloat(getComputedStyle(e).fontSize);
            if (fs > 0 && fs < 10) n++;
          }});
        return n; }""")
    assert tiny <= 70, f"{tiny} elements under 10px — the phone pass has regressed"


def test_controls_are_thumb_sized(page):
    """44px is the documented target; 32 is the floor this asserts, because a
    handful of inline links legitimately sit below it."""
    small = page.evaluate("""() => {
        let n = 0;
        document.querySelectorAll('.zn-tab, .zn-all, .sect-coin').forEach(e => {
          const r = e.getBoundingClientRect();
          if (r.width > 0 && r.height > 0 && r.height < 32) n++;
        });
        return n; }""")
    assert small == 0, f"{small} navigation/coin controls under 32px tall"
