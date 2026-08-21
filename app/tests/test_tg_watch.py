"""👀 /watch — 每日觀察清單 on the phone.

The dashboard card ranks by how many INDEPENDENT engines flagged a coin today.
Three things about that are easy to lose in a Telegram reply and all three are
pinned here:

  · it is a BREADTH count, not a score. Every engine behind it has been
    measured individually in this repo and sits between negative and
    indistinguishable from zero.
  · a coin two engines DISAGREE about is not two engines agreeing. The web
    board ranks those below a clean single; the reply must not flatten it.
  · the cut is where the list stops, not a boundary — on a normal day the
    2-engine tier is dozens deep and ten fit.

And the one that is not cosmetic: this is a reply in a JOINABLE group, so it
must never carry account data.
"""
import re

import tg_commands as T


def _payload(**over):
    base = {
        "date": "2026-08-21", "refreshes": 12, "tracked": 88,
        "tier_engines": 2, "tier_hidden": 39,
        "top": [
            {"base": "JTO", "conflict": False, "side": "long", "engines": 3,
             "hits": 55, "last": 1,
             "why": [{"src": "zone", "label": "供需區", "n": 20},
                     {"src": "oi", "label": "OI 異常", "n": 20},
                     {"src": "s2", "label": "S2 訊號", "n": 15}]},
            {"base": "STABLE", "conflict": False, "side": "long", "engines": 2,
             "hits": 9, "last": 1,
             "why": [{"src": "vegas", "label": "隧道翻多", "n": 5},
                     {"src": "flip", "label": "壓力翻支撐", "n": 4}]},
            {"base": "SPLIT", "conflict": True, "side": None, "engines": 2,
             "hits": 8, "last": 1,
             "why": [{"src": "s2", "label": "S2 訊號", "n": 4},
                     {"src": "zone", "label": "供需區", "n": 4}]},
        ],
    }
    base.update(over)
    return base


def test_it_lists_the_coins_with_their_engines():
    out = T.fmt_watch(_payload())
    assert "每日觀察清單" in out and "2026-08-21" in out
    for base in ("JTO", "STABLE", "SPLIT"):
        assert base in out
    assert "隧道翻多" in out, "the vegas engine never reaches the phone"


def test_a_conflicted_coin_is_not_shown_as_agreement():
    """Two engines pointing opposite ways is the row a reader would act on."""
    out = T.fmt_watch(_payload())
    line = next(ln for ln in out.split("\n") if "SPLIT" in ln)
    assert "⚠️" in line, "a direction conflict rendered as a normal row"
    jto = next(ln for ln in out.split("\n") if "JTO" in ln)
    assert "⚠️" not in jto
    assert "方向不一致" in out, "the marker is never explained"


def test_the_cut_is_disclosed_as_a_cut():
    out = T.fmt_watch(_payload())
    assert "39" in out and "不是分界" in out


def test_no_hidden_tier_means_no_misleading_footnote():
    out = T.fmt_watch(_payload(tier_hidden=0))
    assert "不是分界" not in out, "claimed a hidden tier that does not exist"


def test_it_says_breadth_not_score():
    out = T.fmt_watch(_payload())
    assert "不是分數" in out
    assert "不是進場訊號" in out, "an observe list read as a trade list"


def test_an_empty_day_and_a_dead_scanner_read_differently():
    """'nothing was flagged twice' is a fact about the market. 'the scanners
    have not run' is not, and rendering them the same way is how this repo
    lost three days to a silent scan."""
    quiet = T.fmt_watch(_payload(top=[], tracked=88))
    cold = T.fmt_watch(_payload(top=[], tracked=0))
    assert "88" in quiet and "重複點名" in quiet
    assert "剛開始跑" in cold
    assert quiet != cold


def test_it_never_carries_account_data():
    """A joinable-group reply. send_message() defaults to a PUBLIC topic."""
    out = T.fmt_watch(_payload())
    banned = ("USDT", "餘額", "淨值", "保證金", "槓桿", "已實現", "未實現",
              "balance", "equity", "margin", "leverage", "qty", "PnL")
    hit = [w for w in banned if w.lower() in out.lower()]
    assert not hit, f"account data in a public reply: {hit}"


def test_the_command_is_wired_and_reachable():
    assert "watch" in T.REFRESHABLE_CMDS, "no 🔄 button like its siblings"
    assert "/watch" in T.HELP, "missing from /help"
    import set_bot_commands as B
    assert any(c == "watch" for c, _ in B.COMMANDS), "missing from the ☰ Menu"


def test_dispatch_returns_the_board(monkeypatch):
    """handle() must reach fmt_watch — a formatter nothing calls is dead code."""
    import daily_watch
    monkeypatch.setattr(daily_watch, "top", lambda *a, **k: _payload())
    out = T.handle("watch")
    assert "每日觀察清單" in out and "JTO" in out
    assert T.handle("w") == out, "the short alias diverged"


def test_the_reply_fits_a_telegram_message():
    """4096 bytes, and CJK is 3 bytes a character. Ten rows with long bases
    and three engine names each is the realistic worst case."""
    big = _payload(top=[
        {"base": f"LONGBASE{i}", "conflict": i % 3 == 0, "side": "long",
         "engines": 3, "hits": 99, "last": 1,
         "why": [{"src": "x", "label": "壓力翻支撐", "n": 9},
                 {"src": "y", "label": "隧道翻多", "n": 9},
                 {"src": "z", "label": "OI 異常", "n": 9}]}
        for i in range(10)])
    out = T.fmt_watch(big)
    assert len(out.encode("utf-8")) < 4096, len(out.encode("utf-8"))


def test_the_table_is_html_safe():
    """Sent with parse_mode HTML by the caller; one bare & 400s the send."""
    out = T.fmt_watch(_payload(top=[
        {"base": "A&B<X>", "conflict": False, "side": "long", "engines": 1,
         "hits": 1, "last": 1, "why": [{"src": "s2", "label": "S2 & 訊號", "n": 1}]}]))
    body = out[out.index("<pre>") + 5:out.index("</pre>")]
    assert "&amp;" in body and "&lt;" in body
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|#)", body), "bare & in the table"
