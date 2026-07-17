"""Tech News digest — pure-logic tests (no network, no state file)."""
import time

import tech_news as TN


def _item(title, source="HN"):
    return {"title": title, "link": "https://x.test/a", "source": source,
            "published": ""}


def test_is_ai_matches_llm_world():
    assert TN.is_ai("Anthropic ships MCP support in Claude Code")
    assert TN.is_ai("OpenAI releases GPT-5.2 with agentic tool use")
    assert TN.is_ai("Fine-tuning Llama on a laptop")
    assert not TN.is_ai("Rust 2.0 release candidate announced")
    assert not TN.is_ai("Apple unveils new MacBook Pro")   # 'ai' must not match inside words


def test_digest_sections_and_dedupe():
    now = time.time()
    state = {}
    items = [_item("Claude adds MCP marketplace"), _item("New Linux kernel released")]
    msg = TN.build_digest(items, state, now)
    assert "🤖 AI / LLM" in msg and "🌐 綜合" in msg
    assert msg.index("MCP marketplace") < msg.index("Linux kernel")
    # everything now seen → nothing new → no digest
    assert TN.build_digest(items, state, now) is None


def test_digest_caps_items():
    now = time.time()
    items = [_item(f"Story number {i} about databases") for i in range(30)]
    msg = TN.build_digest(items, {}, now)
    assert msg.count("• ") == TN.DIGEST_MAX


def test_digest_none_when_no_items():
    assert TN.build_digest([], {}, time.time()) is None
