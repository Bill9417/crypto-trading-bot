"""
📰 News gist — a one-line summary under every headline, so nobody has to tap
through to know what a story says.

FREE and local: RSS/Atom feeds already ship a <description>/<summary> next to
each headline (news ledes ARE summaries — inverted-pyramid writing). We strip
the HTML, fix the spacing and trim to one clean sentence-ish line. No API, no
key, no extra network call — the text arrived with the feed fetch itself.

(An AI-translation tier was considered and deliberately dropped: the owner
doesn't want any paid API dependency. If that ever changes, this module is
the single place a smarter summarizer would slot into — gist() is already the
one entry point tech_news and event_radar call.)
"""
import html as _html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_snippet(item: dict, max_len: int = 150) -> str:
    """The feed's own description → clean plain text, sentence-trimmed.
    '' when the feed ships none (HN often doesn't)."""
    raw = (item or {}).get("desc") or ""
    text = _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub(" ", raw))).strip()
    text = re.sub(r"\s+([.,;:!?%)\]。，！？])", r"\1", text)   # "deal ." → "deal."
    if not text or text.lower().startswith(("comments", "article url")):
        return ""                          # HN's desc is just link boilerplate
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for stop in (". ", "。", "! ", "? "):
        pos = cut.rfind(stop)
        if pos > max_len // 2:
            return cut[:pos + 1].strip()
    return cut.rstrip() + "…"


def gist(items: list) -> list:
    """One gist line per item ('' when the feed has none). Matches len(items).
    The single entry point the digest/alert builders call."""
    return [clean_snippet(it) for it in items]
