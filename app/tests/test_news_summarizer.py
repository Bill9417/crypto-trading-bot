"""📰 News gist — the ↳ one-line summary under headlines, free tier only
(the feed's own description, cleaned). No API, no network — by owner request.
"""
import news_summarizer as NS
import tech_news as TN


def _item(title, desc="", source="TechCrunch"):
    return {"title": title, "link": "https://x.test/a", "source": source,
            "published": "", "desc": desc}


def test_clean_snippet_strips_html_and_trims():
    it = _item("t", desc="<p>Apple&amp;Google  reach a <b>deal</b>. "
                         + "More words here. " * 30 + "</p>")
    s = NS.clean_snippet(it)
    assert s.startswith("Apple&Google reach a deal.")
    assert "<" not in s and len(s) <= 160


def test_clean_snippet_trims_at_sentence_boundary():
    s = NS.clean_snippet(_item("t", desc="First sentence here. " * 20))
    assert s.endswith(".") and not s.endswith("…")


def test_clean_snippet_drops_hn_boilerplate_and_empty():
    assert NS.clean_snippet(_item("t", desc="Comments URL: https://x")) == ""
    assert NS.clean_snippet(_item("t")) == ""


def test_gist_matches_length_and_falls_back_to_empty():
    out = NS.gist([_item("a", desc="Fed cuts rates by 50bps today."),
                   _item("b")])
    assert out[0].startswith("Fed cuts rates") and out[1] == ""


# ── integration: digest carries the gist line ────────────────────────────────
def test_digest_shows_gist_lines():
    import time
    items = [_item("Claude ships MCP marketplace",
                   desc="Anthropic launches a marketplace for MCP plugins.")]
    msg = TN.build_digest(items, {}, time.time(), summarizer=NS.gist)
    assert "↳ Anthropic launches a marketplace" in msg
    assert msg.index("↳") < msg.index("https://")       # gist above the link


def test_digest_survives_summarizer_none():
    import time
    msg = TN.build_digest([_item("Some story")], {}, time.time())
    assert "Some story" in msg and "↳" not in msg


def test_event_alert_carries_gist():
    import time
    import event_radar as ER
    state = {"seeded": True, "seen": {}}
    items = [{"title": "Fed cuts interest rates by 50bps", "link": "https://x/a",
              "source": "CNBC", "published": "",
              "desc": "The Federal Reserve cut its benchmark rate."}]
    out = ER._news_alerts(items, state, time.time(), summarizer=NS.gist)
    assert len(out) == 1
    assert "↳ The Federal Reserve cut its benchmark rate." in out[0]
