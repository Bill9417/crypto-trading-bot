"""🇹🇼 Public /tw page — dad's plain-Chinese 'good entry' view of the TW scan.

Two properties matter:
  1. It renders WITHOUT auth (dad opens it straight from the LINE link).
  2. It is account-free — only the setups already broadcast to the LINE group,
     never balances / positions / per-account P&L.
"""
import app as APP
import tw_stocks


def _get(path="/tw"):
    return APP.app.test_client().get(path)


def _stub(monkeypatch, view):
    monkeypatch.setattr(tw_stocks, "web_view", lambda *a, **k: view)


_VIEW = {
    "as_of": "2026-07-24", "generated_at": 0, "regime_ok": True,
    "regime": {"ok": True}, "open_count": 1, "new_count": 1, "buy_zone_count": 1,
    "sl_atr": 3.0, "tp_atr": 5.0, "max_hold": 40,
    "setups": [{
        "code": "2330", "name": "台積電", "date": "2026-07-24", "days": 0,
        "ref": 1000.0, "sl": 900.0, "tp": 1200.0,
        "ref_s": "1,000", "sl_s": "900", "tp_s": "1,200",
        "risk_pct": 10.0, "gain_pct": 20.0, "rr": 2.0, "status": "new",
        "hit": None, "price": 1010.0, "price_s": "1,010",
        "change_pct": 1.5, "dist_pct": 1.0, "buy_zone": True,
    }],
}


def test_tw_renders_without_login(monkeypatch):
    _stub(monkeypatch, _VIEW)
    r = _get()
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "台股．好進場點" in html
    assert "台積電" in html and "接近進場價" in html
    assert "1,200" in html                          # _px-formatted target
    assert "今天可以進場" in html                    # bullish regime banner


def test_tw_shows_observe_banner_when_bearish(monkeypatch):
    view = dict(_VIEW, regime_ok=False, regime={"ok": False},
                new_count=0,
                setups=[dict(_VIEW["setups"][0], status="tracking")])
    _stub(monkeypatch, view)
    html = _get().get_data(as_text=True)
    assert "今天先觀望" in html
    assert "沒有新增訊號" in html                    # the observe-day clarifier
    assert "訊號日參考價" in html                    # tracking entry sublabel


def test_tw_never_leaks_account_words(monkeypatch):
    _stub(monkeypatch, _VIEW)
    html = _get().get_data(as_text=True)
    for banned in ("餘額", "可用保證金", "未實現", "帳戶淨值", "USDT"):
        assert banned not in html


def test_tw_route_failsoft_on_view_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("state gone")

    monkeypatch.setattr(tw_stocks, "web_view", boom)
    r = _get()
    assert r.status_code == 200                     # degraded, not a 500
    assert "台股．好進場點" in r.get_data(as_text=True)


def test_api_tw_returns_json(monkeypatch):
    _stub(monkeypatch, _VIEW)
    r = _get("/api/tw")
    assert r.status_code == 200
    assert r.json["setups"][0]["code"] == "2330"


# ── track record + sizing (2026-08-11) ──────────────────────────────────────
def test_tw_shows_the_running_track_record(monkeypatch):
    view = dict(_VIEW, record={"total": 12, "wins": 5, "losses": 6, "timeouts": 1,
                               "scored": 12, "win_pct": 45.5, "total_r": 2.4,
                               "avg_r": 0.2})
    _stub(monkeypatch, view)
    html = _get().get_data(as_text=True)
    assert '<div class="record">' in html and "已結算 12 檔" in html
    assert "+2.4" in html and "+0.20R" in html
    assert "樣本太少" in html                        # n<30 must be caveated


def test_track_record_hidden_until_something_has_settled(monkeypatch):
    """An empty ledger shows nothing, rather than a 0-0 card that reads as
    'no losses yet'. Keyed on the element, not the words — the stylesheet
    comment mentions 累計成績 too."""
    _stub(monkeypatch, dict(_VIEW, record={"total": 0, "wins": 0, "losses": 0,
                                           "timeouts": 0, "total_r": 0.0}))
    assert '<div class="record">' not in _get().get_data(as_text=True)


def test_small_sample_warning_drops_once_the_record_is_meaningful(monkeypatch):
    _stub(monkeypatch, dict(_VIEW, record={"total": 40, "wins": 18, "losses": 20,
                                           "timeouts": 2, "scored": 40,
                                           "win_pct": 47.4, "total_r": 6.1,
                                           "avg_r": 0.15}))
    assert "樣本太少" not in _get().get_data(as_text=True)


def test_tw_shows_share_count_for_a_fixed_risk(monkeypatch):
    """The 南亞 problem: a −24% stop is unusable as 'just buy some', but fine
    as a share count sized so the stop costs a fixed amount."""
    view = dict(_VIEW, risk_budget=10000,
                setups=[dict(_VIEW["setups"][0],
                             size={"shares": 218, "lots": 0.22,
                                   "cost": 41420, "risk": 9974})])
    _stub(monkeypatch, view)
    html = _get().get_data(as_text=True)
    assert "10,000" in html and "218" in html and "41,420" in html


def test_timeout_setup_is_not_rendered_as_a_stop_out(monkeypatch):
    """'timeout' used to fall through the tp/sl branches and print 已停損,
    reporting a trade that ran out of clock as a loss."""
    view = dict(_VIEW, setups=[dict(_VIEW["setups"][0], status="timeout",
                                    hit={"kind": "timeout", "date": "2026-08-01",
                                         "time": "—"})])
    _stub(monkeypatch, view)
    html = _get().get_data(as_text=True)
    assert "到期出場" in html and "已停損" not in html


# ── the public pages cannot see app.css (2026-08-15) ────────────────────────
def test_public_pages_do_not_reference_tokens_they_cannot_see():
    """/tw and /us ship SELF-CONTAINED — they include _public_style.html and
    deliberately never link app.css, so a cold phone open is one request.

    That means any `var(--token)` naming something defined only in app.css
    resolves to NOTHING here. The site-wide restyle pointed --bg at
    --wolf-slate and the background-image at --page-bg-glow; both evaporated,
    `background: var(--bg)` became invalid, and the body fell back to WHITE
    while --text stayed #f2f6fb. White page, near-white text: the title, the
    index value and the whole watchlist were unreadable — on the only two
    pages opened from a LINE link by someone who will not file a bug report.

    Nothing failed. Every test passed. It is only visible to an eye.
    """
    import os
    import re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(here, "templates", "_public_style.html"),
               encoding="utf-8").read()
    # Strip CSS and Jinja comments FIRST. Without this the scan matches the
    # prose explaining the bug — "LITERAL, not var(--wolf-slate)" — and reports
    # the very thing that was fixed. This repo has shipped four tests that
    # matched their own commentary; this is the shape of that mistake.
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"\{#.*?#\}", " ", src, flags=re.S)
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", src, re.M))
    used = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", src))
    dangling = sorted(used - defined)
    assert not dangling, (
        f"_public_style.html uses {dangling} but does not define them, and "
        f"these pages never load app.css — they will resolve to nothing")


def test_the_public_background_still_matches_the_rest_of_the_site():
    """The values are mirrored from app.css by hand (they must be — see
    above), so pin the one that decides whether the page is dark. A restyle
    that moves the site to a new base colour and forgets these two pages
    leaves them on the old one, which is the subtler half of the same bug."""
    import os
    import re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pub = open(os.path.join(here, "templates", "_public_style.html"),
               encoding="utf-8").read()
    css = open(os.path.join(here, "static", "app.css"), encoding="utf-8").read()
    shared = re.search(r"--wolf-slate:\s*(#[0-9a-fA-F]{3,8})", css)
    assert shared, "could not find --wolf-slate in app.css"
    mine = re.search(r"--bg:\s*(#[0-9a-fA-F]{3,8})", pub)
    assert mine, "_public_style.html no longer sets --bg to a literal colour"
    assert mine.group(1).lower() == shared.group(1).lower(), (
        f"public pages are on {mine.group(1)} while the site moved to "
        f"{shared.group(1)}")


def test_public_pages_never_link_app_css():
    """If this ever changes the two tests above become unnecessary — and
    keeping them would then be misleading. Fail loudly so the decision is
    made deliberately rather than discovered."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for page in ("tw.html", "us.html"):
        html = open(os.path.join(here, "templates", page), encoding="utf-8").read()
        assert "app.css" not in html, (
            f"{page} now links app.css — the self-contained assumption behind "
            f"_public_style.html's inlined tokens no longer holds")
