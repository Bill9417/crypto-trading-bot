"""🐳 Whale tracker — Hyperliquid position following into the 清算 topic.

Covers the 2026-07-17 addition. Network is fully stubbed (fetch_positions is
monkeypatched) — a test must NEVER hit the real Hyperliquid API, matching the
autouse live-side-effect guard in conftest.
"""
import telegram_utils
import whale_tracker as WT

ADDR = "0x" + "a" * 40
ADDR2 = "0x" + "b" * 40


def _pos(side, szi, notional, entry=100.0, upnl=0.0, lev=5, liq=None):
    return {"side": side, "szi": szi, "notional": notional,
            "entry": entry, "upnl": upnl, "lev": lev, "liq": liq}


def _wire(monkeypatch, tmp_path):
    monkeypatch.setattr(WT, "ADDR_FILE", str(tmp_path / "addr.json"))
    monkeypatch.setattr(WT, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(WT, "POLL_SEC", 0)
    monkeypatch.setattr(WT, "_last_poll", 0.0)


# ── address list ─────────────────────────────────────────────────────────────
def test_valid_address():
    assert WT.valid_address(ADDR)
    assert WT.valid_address("0x0DDF9bae2af4b874b96d287a5ad42eb47138A902")
    assert not WT.valid_address("0x123")
    assert not WT.valid_address("nope")


def test_add_dedupe_remove(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    assert "已加入" in WT.add_address(ADDR, "鯨魚A")
    assert "已在追蹤" in WT.add_address(ADDR.upper(), "dup")   # case-insensitive dedupe
    assert "格式錯誤" in WT.add_address("0xbad")
    rows = WT.load_addresses()
    assert sum(1 for r in rows if r["address"] == WT._norm(ADDR)) == 1
    assert "已移除" in WT.remove_address(ADDR)
    assert all(r["address"] != WT._norm(ADDR) for r in WT.load_addresses())


# ── change detection ─────────────────────────────────────────────────────────
def test_diff_open_close_flip_resize():
    prev = {"BTC": _pos("long", 10, 6e6), "ETH": _pos("short", -100, 3e6)}
    cur = {
        "BTC": _pos("long", 10, 6e6),        # unchanged
        "ETH": _pos("long", 90, 3e6),        # flipped short→long
        "SOL": _pos("long", 5000, 1e6),      # newly opened
    }                                        # (HYPE prev-only → closed)
    prev["HYPE"] = _pos("long", 200, 2e6)
    kinds = {e["coin"]: e["kind"] for e in WT.diff_positions(prev, cur)}
    assert kinds["ETH"] == "flip"
    assert kinds["SOL"] == "open"
    assert kinds["HYPE"] == "close"
    assert "BTC" not in kinds                # unchanged → no event


def test_diff_resize_uses_size_not_price():
    # notional doubled but szi unchanged (pure price move) → NO resize alert
    prev = {"BTC": _pos("long", 10, 3e6)}
    cur = {"BTC": _pos("long", 10, 6e6)}
    assert WT.diff_positions(prev, cur) == []
    # szi +50% → add
    cur2 = {"BTC": _pos("long", 15, 3e6)}
    assert WT.diff_positions(prev, cur2)[0]["kind"] == "add"
    # szi -50% → trim
    cur3 = {"BTC": _pos("long", 5, 3e6)}
    assert WT.diff_positions(prev, cur3)[0]["kind"] == "trim"


def test_diff_ignores_new_dust():
    prev = {}
    cur = {"PEPE": _pos("long", 1, WT.MIN_NOTIONAL_USD - 1)}
    assert WT.diff_positions(prev, cur) == []


# ── tick lifecycle ───────────────────────────────────────────────────────────
def test_tick_seeds_silently_then_alerts(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    WT._save_addresses([{"address": WT._norm(ADDR), "label": "鯨魚A"}])
    sent = []
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **k: sent.append((k.get("channel"), msg)) or True)

    seq = [
        {"BTC": _pos("long", 10, 6e6)},                       # first sight
        {"BTC": _pos("long", 10, 6e6), "ETH": _pos("short", -50, 4e6)},  # +ETH
    ]
    box = {"i": 0}
    monkeypatch.setattr(WT, "fetch_positions",
                        lambda a: (seq[box["i"]], 1e7))

    monkeypatch.setattr(WT, "_last_poll", 0.0)
    assert WT.tick() == 0                    # first sight seeds, no flood
    box["i"] = 1
    monkeypatch.setattr(WT, "_last_poll", 0.0)
    assert WT.tick() == 1                    # the new ETH short alerts once
    channel, msg = sent[-1]
    assert channel == "liq"
    assert "🐳" in msg and "ETH" in msg and "做空" in msg


def test_tick_respects_poll_pacing(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(WT, "POLL_SEC", 999999)
    monkeypatch.setattr(WT, "_last_poll", 9.9e12)   # "just polled" → skip
    called = {"n": 0}
    monkeypatch.setattr(WT, "fetch_positions",
                        lambda a: (called.__setitem__("n", called["n"] + 1), ({}, 0))[1])
    assert WT.tick() == 0 and called["n"] == 0       # paced out, no fetch


def test_tick_burst_cap(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(WT, "ALERTS_PER_POLL", 2)
    WT._save_addresses([{"address": WT._norm(ADDR), "label": "鯨魚A"}])
    sent = []
    monkeypatch.setattr(telegram_utils, "send_message",
                        lambda msg, **k: sent.append(msg) or True)
    seq = [{}, {c: _pos("long", 100, 5e6) for c in ("BTC", "ETH", "SOL", "XRP")}]
    box = {"i": 0}
    monkeypatch.setattr(WT, "fetch_positions", lambda a: (seq[box["i"]], 1e7))
    monkeypatch.setattr(WT, "_last_poll", 0.0)
    WT.tick()                                # seed empty
    box["i"] = 1
    monkeypatch.setattr(WT, "_last_poll", 0.0)
    assert WT.tick() == 2                    # 4 opens, capped at 2


# ── honesty + formatting ─────────────────────────────────────────────────────
def test_alert_has_coinglass_link_and_no_fake_winrate(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    pos = {"ETH": _pos("short", -50000, 91e6, entry=1700, upnl=-6.4e6, lev=3, liq=2177)}
    msg = WT.build_alert("巨鯨#1", ADDR, {"coin": "ETH", "kind": "open", "side": "short"},
                         pos, acct=12.6e6)
    assert "coinglass.com/hyperliquid/" in msg
    assert "$91.00M" in msg and "−$6.40M" in msg     # notional + signed uPnL
    assert "3x" in msg and "非投資建議" in msg
    # we must NOT invent a win rate (fills-based WR is meaningless)
    assert "勝率" not in msg and "win rate" not in msg.lower()


def test_consensus_aggregates_only_shared_coins():
    books = [
        ("A", "0x1", {"ETH": _pos("short", -10, 50e6), "BTC": _pos("long", 5, 10e6)}, 1e7),
        ("B", "0x2", {"ETH": _pos("short", -20, 40e6)}, 1e7),
        ("C", "0x3", {"ETH": _pos("long", 5, 10e6)}, 1e7),
    ]
    joined = "\n".join(WT.whale_consensus(books))
    assert "ETH" in joined and "2空" in joined and "1多" in joined  # 3 whales on ETH
    assert "淨空" in joined                                         # net short $80M
    assert "BTC" not in joined                                     # only 1 whale → excluded


def test_report_offline(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    WT._save_addresses([{"address": WT._norm(ADDR), "label": "鯨魚A"}])
    monkeypatch.setattr(WT, "fetch_positions",
                        lambda a: ({"BTC": _pos("long", 10, 6e6, entry=60000, upnl=5e5)}, 1e7))
    rep = WT.build_report()
    assert "巨鯨追蹤" in rep and "鯨魚A" in rep
    assert "做多" in rep and "BTC" in rep and "coinglass.com" in rep
    assert "<pre>" in rep                        # aligned positions table


def test_report_caps_detail_and_ranks_biggest_first(monkeypatch, tmp_path):
    """15 full books blow past Telegram's 4096 chars, so /whale details the
    biggest few and folds the rest into a line — the consensus header above
    still counts every tracked whale."""
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(WT, "REPORT_MAX_WHALES", 3)
    addrs = [f"0x{i:040x}" for i in range(1, 8)]
    WT._save_addresses([{"address": a, "label": f"鯨{i}"}
                        for i, a in enumerate(addrs, 1)])
    sizes = {a: (i + 1) * 1e6 for i, a in enumerate(addrs)}   # 鯨7 is biggest
    monkeypatch.setattr(WT, "fetch_positions",
                        lambda a: ({"ETH": _pos("short", -1, sizes[WT._norm(a)])}, 1e7))
    rep = WT.build_report()
    assert "共 7 個地址" in rep
    assert "鯨7" in rep and "鯨6" in rep and "鯨5" in rep       # 3 biggest detailed
    assert "鯨1" not in rep and "鯨2" not in rep                # smallest folded away
    assert "另外 4 個較小的巨鯨" in rep
    assert rep.index("鯨7") < rep.index("鯨5")                  # biggest book first
    assert "7空" in rep                                        # consensus counts ALL 7


def test_report_tail_absent_when_everything_fits(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    WT._save_addresses([{"address": WT._norm(ADDR), "label": "鯨魚A"}])
    monkeypatch.setattr(WT, "fetch_positions",
                        lambda a: ({"BTC": _pos("long", 10, 6e6)}, 1e7))
    assert "另外" not in WT.build_report()


def test_state_keeps_cost_basis_without_disturbing_diffs(monkeypatch, tmp_path):
    """entry/lev/liq ride along so the dashboard can price a whale's P&L, but
    change detection still keys on side and szi alone."""
    _wire(monkeypatch, tmp_path)
    WT._save_addresses([{"address": WT._norm(ADDR), "label": "鯨魚A"}])
    pos = {"ETH": _pos("long", 10, 5e6, entry=1800.0, lev=5, liq=1450.0)}
    monkeypatch.setattr(WT, "fetch_positions", lambda a: (pos, 1e7))
    monkeypatch.setattr(telegram_utils, "send_message", lambda *a, **k: True)
    WT.tick()                                        # first sight → seeds silently
    saved = WT._load_state()[WT._norm(ADDR)]
    eth = saved["ETH"]
    assert eth["entry"] == 1800.0 and eth["lev"] == 5 and eth["liq"] == 1450.0
    assert eth["side"] == "long" and eth["szi"] == 10
    # a pure price move (same side, same size) is still not an event
    assert WT.diff_positions(saved, {"ETH": _pos("long", 10, 9e6, entry=1900.0)}) == []
