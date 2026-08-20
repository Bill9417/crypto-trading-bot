"""📰 One rate-limited publisher must not cost its headlines or cry wolf.

Reported 2026-08-20: "⚠ Some sources had issues: CryptoSlate: HTTP Error 429".
Two things were wrong with that. The source's headlines vanished from the
merged stream for the whole cache window, and a TRANSIENT rate limit was shown
with the same weight as a dead feed — so the page said something was broken
when the only thing that happened was "not so fast". And retrying on the next
5-minute tick is what earns the next 429.
"""
import json
import os

import market_intel as M
import pytest

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin rallies to a record high</title><link>http://a</link>
<pubDate>Wed, 20 Aug 2026 10:00:00 GMT</pubDate></item></channel></rss>"""


@pytest.fixture
def feeds(monkeypatch, tmp_path):
    # ONE seam — the same one the degradation test uses.
    monkeypatch.setattr(M, "_DISK_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(M, "NEWS_FEEDS", [("Good", "http://x/good"),
                                          ("Limited", "http://x/limited")])
    state = {"mode": "ok", "calls": 0}

    def fake_get(url, timeout=8.0):
        if "limited" in url:
            state["calls"] += 1
            if state["mode"] == "429":
                raise Exception("HTTP Error 429: Too Many Requests")
        return RSS

    monkeypatch.setattr(M, "_get_text", fake_get)
    return state


def test_a_rate_limited_feed_keeps_its_headlines(feeds):
    t = 1_000_000.0
    assert len(M._news(now=t)["items"]) == 2
    feeds["mode"] = "429"
    r = M._news(now=t + 400)
    assert len(r["items"]) == 2, "the rate-limited source lost its headlines"
    assert r["stale_sources"] == ["Limited"]


def test_a_transient_limit_is_not_reported_as_a_broken_source(feeds):
    t = 1_000_000.0
    M._news(now=t)
    feeds["mode"] = "429"
    assert M._news(now=t + 400)["errors"] == [], \
        "a 429 with recent headlines on hand was shown as an error"


def test_it_stops_asking_instead_of_earning_another_429(feeds):
    t = 1_000_000.0
    M._news(now=t)
    feeds["mode"] = "429"
    M._news(now=t + 400)
    before = feeds["calls"]
    for k in range(6):                       # half an hour of 5-minute ticks
        M._news(now=t + 500 + k * 300)
    assert feeds["calls"] == before, "still hammering the publisher"


def test_it_recovers_on_its_own(feeds):
    t = 1_000_000.0
    M._news(now=t)
    feeds["mode"] = "429"
    M._news(now=t + 400)
    feeds["mode"] = "ok"
    r = M._news(now=t + M.FEED_BACKOFF_MAX + 1000)
    assert not r["errors"] and not r["stale_sources"]
    assert len(r["items"]) == 2


def test_a_feed_dark_for_hours_is_still_reported(feeds):
    """Serving memory is only honest while the memory is recent. Past that the
    page must say the source is missing rather than quietly ageing."""
    t = 1_000_000.0
    M._news(now=t)
    with open(M._feed_state_path(), encoding="utf-8") as f:
        st = json.load(f)
    st["Limited"]["ts"] = t - (M.FEED_STALE_REPORT_SEC + 5000)
    st["Limited"]["until"] = 0
    with open(M._feed_state_path(), "w", encoding="utf-8") as f:
        json.dump(st, f)
    feeds["mode"] = "429"
    assert M._news(now=t + 100)["errors"], "a long-dark source went unmentioned"


def test_the_state_is_shared_between_processes(feeds):
    """The web process and the scanner both fetch news. In-memory backoff
    would let each hold the publisher to its own limit."""
    M._news(now=1_000_000.0)
    assert os.path.exists(M._feed_state_path())
