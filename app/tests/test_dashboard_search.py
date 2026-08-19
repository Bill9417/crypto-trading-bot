"""🔍 幣種分析 search in the dashboard header.

Types a ticker, opens the SAME pop-out the OI and flip cards already use. The
value of it is entirely in NOT being a second implementation: suggestions come
from /api/coin_search (what the /coin page uses) and the analysis is the
existing iframe, so neither can drift from the page it mirrors.
"""
import app as APP


def _client():
    APP.app.config["TESTING"] = True
    c = APP.app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = "1"
        s["_fresh"] = True
    return c


def _dash():
    r = _client().get("/")
    assert r.status_code == 200
    return r.get_data(as_text=True)


def test_the_search_box_is_in_the_page_header():
    h = _dash()
    assert 'id="pgs-q"' in h and 'id="pgs-sugg"' in h
    # In the header, not floating somewhere below the fold.
    head = h[h.index('class="pg-head"'):h.index('class="status-row"')]
    assert 'id="pgs-q"' in head


def test_it_reuses_the_existing_search_endpoint():
    h = _dash()
    assert "/api/coin_search?q=" in h, "a second autocomplete would drift from /coin"


def test_it_opens_the_existing_popout_rather_than_a_new_window():
    h = _dash()
    assert "window.wolfCoinPopout = show" in h, "the pop-out is not exposed"
    assert "window.wolfCoinPopout(b)" in h, "the search does not use it"
    # …and still degrades to a normal navigation if the modal is ever absent.
    assert "window.location.href = '/coin/'" in h


def test_the_search_endpoint_answers():
    d = _client().get("/api/coin_search?q=bt").get_json()
    assert "matches" in d and isinstance(d["matches"], list)


def test_a_blank_query_is_not_a_search():
    """An empty box must not fire a request per keystroke while it clears."""
    h = _dash()
    assert "if (!v) { close(); return; }" in h
