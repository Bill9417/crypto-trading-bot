"""
Tech News digest — newest tech/AI headlines → the Telegram "tech" channel.

For the CS-student side of the group: every TECH_DIGEST_SEC (default 6h) it
pulls a handful of free tech feeds and sends ONE digest of the headlines that
are new since the last digest — AI/LLM stories (Claude, MCP, GPT, agents,
open-source models…) pinned to the top section, everything else below.

Sources (all free, no keys; a dead feed is skipped silently):
  • Hacker News front page (≥150 points — the community's quality filter)
  • TechCrunch / The Verge / Ars Technica
  • Simon Willison's blog — the best running commentary on LLM tooling & MCP

Runs inside the strategy2_scanner loop like event_radar. State (seen titles,
last digest time) persists in tech_news_state.json; the first ever run sends
a starter digest immediately so a fresh setup shows signs of life.
"""
import json
import os
import re
import time

import market_intel
import telegram_utils

STATE_FILE = os.path.join(os.path.dirname(__file__), "tech_news_state.json")

DIGEST_SEC = int(os.getenv("TECH_DIGEST_SEC", "21600"))     # one digest / 6h
DIGEST_MAX = int(os.getenv("TECH_DIGEST_MAX", "12"))        # headlines per digest
SEEN_RETAIN_SEC = 7 * 86400

FEEDS = [
    ("HN", "https://hnrss.org/frontpage?points=150&count=25"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
]

# Headlines matching this float to the 🤖 AI/LLM section at the top.
_AI_RE = re.compile(
    r"\b(ai|llms?|claude|anthropic|mcp|model context protocol|gpt-?[0-9a-z]*|"
    r"openai|chatgpt|gemini|deepseek|llama|mistral|qwen|copilot|cursor|"
    r"agents?|agentic|transformers?|neural|machine learning|deep learning|"
    r"fine-?tun(e|ing)|rag|inference|open[- ]source model|hugging ?face)\b", re.I)


def is_ai(title: str) -> bool:
    return bool(_AI_RE.search(title or ""))


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — missing/corrupt state = start fresh
        return {}


def _save_state(state: dict) -> None:
    cutoff = time.time() - SEEN_RETAIN_SEC
    state["seen"] = {k: v for k, v in (state.get("seen") or {}).items() if v >= cutoff}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def _title_key(item: dict) -> str:
    return re.sub(r"\W+", "", (item.get("title") or "").lower())[:120]


def _fetch_items() -> list:
    items = []
    for source, url in FEEDS:
        try:
            xml_text = market_intel._get_text(url, timeout=8.0)
            items.extend(market_intel._parse_rss(source, xml_text, 12))
        except Exception:  # noqa: BLE001 — one dead feed must not kill the digest
            continue
    items.sort(key=market_intel._pub_ts, reverse=True)
    return items


def build_digest(items: list, state: dict, now: float) -> str | None:
    """Digest text of headlines not seen before, AI section first; None when
    nothing new. Mutates state["seen"]."""
    seen = state.setdefault("seen", {})
    fresh = []
    for it in items:
        key = _title_key(it)
        if not key or key in seen:
            continue
        seen[key] = now
        fresh.append(it)
        if len(fresh) >= DIGEST_MAX:
            break
    if not fresh:
        return None
    ai = [i for i in fresh if is_ai(i["title"])]
    rest = [i for i in fresh if not is_ai(i["title"])]

    def _fmt(i):
        title = (i["title"] or "")[:110]
        return f"• {title} ({i['source']})\n  {i['link']}"

    lines = [f"💻 TECH DIGEST · {len(fresh)} new"]
    if ai:
        lines.append("\n🤖 AI / LLM")
        lines += [_fmt(i) for i in ai]
    if rest:
        lines.append("\n🌐 General")
        lines += [_fmt(i) for i in rest]
    return "\n".join(lines)


def tick() -> bool:
    """Send a digest when the cadence is due; returns True if one was sent."""
    now = time.time()
    state = _load_state()
    if now - state.get("last_digest", 0) < DIGEST_SEC:
        return False
    msg = build_digest(_fetch_items(), state, now)
    state["last_digest"] = now            # even an empty window resets the clock
    sent = False
    if msg:
        try:
            sent = bool(telegram_utils.send_message(msg, force=True, channel="tech"))
            print(f"[tech] digest sent ({len(msg)} chars)" if sent else "[tech] digest send failed")
        except Exception as exc:  # noqa: BLE001 — digest must never kill the loop
            print(f"[tech] digest send error: {exc}")
    _save_state(state)
    return sent
