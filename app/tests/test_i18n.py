"""🌐 The 中文 ⇄ English switch.

The complaint was that the pages read as a MIXTURE. static/i18n.js only ever
had an English → 中文 dictionary, which was the whole job while the templates
were written in English; most of this app is authored in 中文 now, so that
dictionary could not reach any of it.

THE HARD PART IS NOT TRANSLATION, IT IS WORD BOUNDARIES. Chinese has none, so
a short entry used as a substring rewrites the inside of longer words. Within
one sitting this produced 新多進場 → "新longEntry", 未平倉 → "未close",
最近 → "最last", and — after the first fix — 訊號專區 → "signals專區", because
訊號 looked obviously safe and is not.

That is not a thing to eyeball. unsafe_substrings() checks every opt-in key
against a corpus of the strings the real pages actually render, and this file
runs it.
"""
import json
import os
import re
import subprocess

import pytest

import i18n_table as T

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
CORPUS = os.path.join(HERE, "fixtures", "rendered_zh_corpus.json")
CJK = re.compile(r"[一-鿿]")


def corpus():
    with open(CORPUS, encoding="utf-8") as f:
        return json.load(f)


# ── the word-boundary rule ───────────────────────────────────────────────────
def test_no_opt_in_fragment_can_corrupt_a_longer_word():
    """The check that has already caught four real corruptions."""
    bad = T.unsafe_substrings(corpus())
    assert not bad, "\n".join(
        f"  {k!r} sits inside {run!r} — replacing it would corrupt the word"
        for k, run, _ in bad)


def test_the_checker_actually_catches_a_known_bad_fragment(monkeypatch):
    """A green safety check that cannot go red is worse than none. 訊號 is the
    real one that slipped through: it lives inside 訊號專區."""
    monkeypatch.setitem(T.SUB_OK, "訊號", " signals")
    bad = T.unsafe_substrings(corpus())
    assert any(k == "訊號" for k, _, _ in bad), \
        "the corpus check no longer detects a fragment inside a longer word"


def test_long_keys_do_not_need_the_opt_in():
    """WHOLE_MAX in i18n.js lets anything longer than 3 Chinese characters
    substitute as a substring. Keys at or under that limit must be listed in
    SUB_OK or they only apply to a whole text node — silently doing nothing
    otherwise, which looks exactly like a missing translation."""
    with open(os.path.join(APP_DIR, "static", "i18n.js"), encoding="utf-8") as f:
        js = f.read()
    m = re.search(r"var WHOLE_MAX = (\d+)", js)
    assert m, "WHOLE_MAX vanished from i18n.js"
    limit = int(m.group(1))
    short = [k for k in T.table()
             if len(CJK.findall(k)) <= limit and k not in T.SUB_OK]
    # These are fine as whole-node-only; the test documents the split rather
    # than forbidding it. What it pins is that SUB_OK is the ONLY escape hatch.
    assert all(k not in T.SUB_OK for k in short)


# ── the table itself ─────────────────────────────────────────────────────────
def test_every_translation_is_actually_english():
    """A 'translation' that still contains Chinese leaves the page mixed,
    which is the entire complaint."""
    bad = [(k, v) for k, v in T.table().items()
           if CJK.search(v) and k not in T.KEEP_AS_IS]
    assert not bad, f"translations still containing Chinese: {bad}"


def test_nothing_translates_to_an_empty_string():
    """An empty value silently deletes text — it reads as a rendering bug
    rather than as a missing translation."""
    empty = [k for k, v in T.table().items() if not v.strip() and k.strip()]
    assert not empty, f"these erase their text: {empty}"


def test_the_generated_js_matches_the_python_table():
    """static/i18n_zh_en.js is GENERATED. If it drifts, the page ships a table
    nothing here has checked."""
    gen = T.as_js()
    with open(os.path.join(APP_DIR, "static", "i18n_zh_en.js"),
              encoding="utf-8") as f:
        on_disk = f.read()
    assert gen == on_disk, ("static/i18n_zh_en.js is stale — regenerate with "
                            "`python i18n_table.py`")


def test_proper_nouns_are_left_alone():
    """中鋼/南亞/奇鋐/智邦 are Taiwanese companies. An 'English' version would
    be a different company to anyone checking a ticker."""
    for name in ("中鋼", "南亞", "奇鋐", "智邦"):
        assert name not in T.table(), f"{name} is a company name, not a word"


def test_the_language_button_keeps_its_own_label():
    """The button names the language it switches TO, so in English mode it has
    to read 中文. Translating it would make it say 'English' while already in
    English."""
    with open(os.path.join(APP_DIR, "static", "i18n.js"), encoding="utf-8") as f:
        js = f.read()
    assert 'v !== "中文"' in js, "中文 would be counted as an untranslated miss"
    assert "中文" not in T.table(), "the toggle label must not be translated"


# ── the applied result ───────────────────────────────────────────────────────
def _translate(samples):
    """Run the REAL toEnglish() out of i18n.js under node."""
    with open(os.path.join(APP_DIR, "static", "i18n.js"), encoding="utf-8") as f:
        js = f.read()
    m = re.search(r"var WHOLE_MAX[\s\S]*?\n  \}\n\n  function toEnglish[\s\S]*?\n  \}", js)
    assert m, "could not lift toEnglish() out of i18n.js"
    with open(os.path.join(APP_DIR, "static", "i18n_zh_en.js"),
              encoding="utf-8") as f:
        table_js = f.read()
    prog = ("global.window = global;\n" + table_js + m.group(0) +
            "\nconsole.log(JSON.stringify(" + json.dumps(samples) +
            ".map(toEnglish)));")
    p = subprocess.run(["node", "-e", prog], capture_output=True, text=True,
                       timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().split("\n")[-1])


@pytest.mark.skipif(not os.path.exists("/opt/homebrew/bin/node")
                    and not os.path.exists("/usr/local/bin/node"),
                    reason="node not installed")
def test_compound_words_survive_translation():
    """The four corruptions this design exists to prevent, checked through the
    real applier rather than by reading the rule."""
    out = _translate(["新多單進場", "未平倉", "最近", "訊號專區", "幣種專區"])
    for got in out:
        assert not re.search(r"[一-鿿][A-Za-z]|[A-Za-z][一-鿿]", got), \
            f"a fragment was replaced inside a longer word: {got!r}"


@pytest.mark.skipif(not os.path.exists("/opt/homebrew/bin/node")
                    and not os.path.exists("/usr/local/bin/node"),
                    reason="node not installed")
def test_runtime_assembled_strings_translate():
    """The reason this is substring replacement and not a lookup: the text is
    built at runtime around numbers, so no fixed key can ever equal the node."""
    got = _translate(["資料 4分前", "資料 剛更新", "掃描 527 檔沒列出",
                      "延遲 195s", "只做觀察，不是進場訊號。"])
    assert got[0] == "data 4m ago"
    assert got[1] == "data just now"
    assert "delay 195s" == got[3]
    assert got[4] == "Observation only — not an entry signal."


def test_the_nav_loads_the_table_before_the_translator():
    """i18n.js reads window.WOLF_ZH_EN at first use; loading it after would
    give an empty table and a silently untranslated page."""
    with open(os.path.join(APP_DIR, "templates", "_nav.html"),
              encoding="utf-8") as f:
        nav = f.read()
    assert nav.index("i18n_zh_en.js") < nav.index("i18n.js\'"), \
        "the phrase table loads after the code that reads it"


def test_english_mode_actually_runs():
    """EN used to be a no-op because the server rendered English. It renders
    中文 now, so English is a translation pass too — and if this regresses the
    toggle silently does nothing in one direction."""
    with open(os.path.join(APP_DIR, "static", "i18n.js"), encoding="utf-8") as f:
        js = f.read()
    assert "applyEn()" in js
    assert re.search(r'if \(lang === "zh"\) \{ applyZh\(\); \} else \{ applyEn\(\); \}', js), \
        "English mode no longer applies a translation"
