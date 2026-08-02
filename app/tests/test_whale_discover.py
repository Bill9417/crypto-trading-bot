"""whale_discover — the filters that separate a followable trader from an
account that merely looks good on a PnL sort.

Every exclusion here corresponds to a real account seen in the live 41k-row
leaderboard, not a hypothetical: the market maker was #1 by equity with a
NEGATIVE all-time PnL on $119B of volume; the airdrop holder showed $445M of
"PnL" on ~$0 of volume.
"""
import json

import pytest

import whale_discover
import whale_tracker


def _row(addr, equity=5_000_000, pnl=20_000_000, vlm=1_000_000_000,
         month_vlm=50_000_000, month_pnl=1_000_000, name=None):
    return {
        "ethAddress": addr,
        "accountValue": str(equity),
        "displayName": name,
        "windowPerformances": [
            ["day", {"pnl": "0", "roi": "0", "vlm": "0"}],
            ["week", {"pnl": "0", "roi": "0", "vlm": "0"}],
            ["month", {"pnl": str(month_pnl), "roi": "0", "vlm": str(month_vlm)}],
            ["allTime", {"pnl": str(pnl), "roi": "0", "vlm": str(vlm)}],
        ],
    }


A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No network, no writes to the real address list or candidate cache."""
    monkeypatch.setattr(whale_discover, "CACHE_FILE", str(tmp_path / "cand.json"))
    monkeypatch.setattr(whale_tracker, "ADDR_FILE", str(tmp_path / "addr.json"))
    monkeypatch.setattr(whale_tracker, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(whale_discover, "fetch_leaderboard",
                        lambda *a, **k: pytest.fail("test hit the network"))


# ── filters ──────────────────────────────────────────────────────────────────
def test_market_maker_is_excluded():
    """$119B volume for a tiny/negative PnL is inventory, not a view."""
    mm = _row(A, equity=59_000_000, pnl=1_000_000, vlm=119_000_000_000)
    assert whale_discover.distill([mm]) == []


def test_negative_pnl_is_excluded():
    assert whale_discover.distill([_row(A, pnl=-3_000_000)]) == []


def test_airdrop_holder_is_excluded():
    """$445M of PnL on ~$0 of volume never came from trading."""
    drop = _row(A, equity=225_000_000, pnl=445_000_000, vlm=16, month_vlm=0)
    assert whale_discover.distill([drop]) == []


def test_edge_ceiling_excludes_non_trading_pnl():
    """Volume is high enough to pass the floor, but the ratio is impossible."""
    weird = _row(A, pnl=900_000_000, vlm=100_000_000)      # 900% edge
    assert whale_discover.distill([weird]) == []


def test_dormant_account_is_excluded():
    """No volume this month → it would never fire an alert."""
    assert whale_discover.distill([_row(A, month_vlm=0)]) == []


def test_small_account_is_excluded():
    assert whale_discover.distill([_row(A, equity=50_000)]) == []


def test_malformed_address_is_excluded():
    assert whale_discover.distill([_row("not-an-address")]) == []
    assert whale_discover.distill([_row("0xdeadbeef")]) == []


def test_good_trader_survives_and_carries_its_numbers():
    got = whale_discover.distill([_row(A, equity=16_500_000, pnl=98_600_000,
                                       vlm=1_742_000_000)])
    assert len(got) == 1
    assert got[0]["address"] == A
    assert got[0]["pnl"] == pytest.approx(98_600_000)
    assert got[0]["edge"] == pytest.approx(98_600_000 / 1_742_000_000)


def test_ranked_by_all_time_pnl():
    rows = [_row(A, pnl=10_000_000), _row(B, pnl=90_000_000),
            _row(C, pnl=50_000_000)]
    assert [c["address"] for c in whale_discover.distill(rows)] == [B, C, A]


def test_missing_window_does_not_crash():
    row = {"ethAddress": A, "accountValue": "5000000", "windowPerformances": []}
    assert whale_discover.distill([row]) == []


# ── verification against live state ──────────────────────────────────────────
def _positions(monkeypatch, mapping):
    def fake(addr):
        return mapping.get(whale_tracker._norm(addr), ({}, 0.0))
    monkeypatch.setattr(whale_tracker, "fetch_positions", fake)


def test_discover_drops_addresses_with_no_live_position(monkeypatch):
    """A profitable history with an empty book produces no alerts — useless."""
    _positions(monkeypatch, {
        A: ({"ETH": {"side": "short", "notional": 42_000_000}}, 24_000_000),
        B: ({}, 30_000_000),
    })
    got = whale_discover.discover(limit=10, rows=[_row(A), _row(B)],
                                  use_cache=False)
    assert [c["address"] for c in got] == [A]


def test_discover_drops_dust_only_books(monkeypatch):
    _positions(monkeypatch, {A: ({"ETH": {"side": "long", "notional": 900}},
                                 5_000_000)})
    assert whale_discover.discover(rows=[_row(A)], use_cache=False) == []


def test_discover_orders_by_live_size(monkeypatch):
    """PnL ranks the shortlist; the live book ranks what's shown."""
    _positions(monkeypatch, {
        A: ({"BTC": {"side": "short", "notional": 1_000_000}}, 5_000_000),
        B: ({"ETH": {"side": "short", "notional": 80_000_000}}, 9_000_000),
    })
    got = whale_discover.discover(rows=[_row(A, pnl=90_000_000),
                                        _row(B, pnl=10_000_000)],
                                  use_cache=False)
    assert [c["address"] for c in got] == [B, A]


def test_tracked_flag_marks_existing_watchlist(monkeypatch):
    whale_tracker._save_addresses([{"address": A, "label": "已有"}])
    _positions(monkeypatch, {
        A: ({"ETH": {"side": "short", "notional": 5_000_000}}, 5_000_000),
        B: ({"BTC": {"side": "long", "notional": 4_000_000}}, 5_000_000),
    })
    got = {c["address"]: c["tracked"]
           for c in whale_discover.discover(rows=[_row(A), _row(B)],
                                            use_cache=False)}
    assert got == {A: True, B: False}


# ── labels ───────────────────────────────────────────────────────────────────
def test_label_describes_the_book_not_a_win_rate():
    label = whale_discover.suggest_label({
        "live_equity": 55_390_000,
        "book": [{"coin": "ETH", "side": "short", "notional": 58_000_000}],
    })
    assert label == "ETH空巨鯨 · $55M"
    assert "%" not in label and "勝率" not in label


def test_label_prefers_a_real_display_name():
    label = whale_discover.suggest_label(
        {"display_name": "Machi", "live_equity": 12_000_000, "book": []})
    assert label.startswith("Machi")


def test_label_survives_an_empty_book():
    assert whale_discover.suggest_label({"equity": 3_200_000}) == "巨鯨 · $3M"


# ── cache ────────────────────────────────────────────────────────────────────
def test_cache_is_reused_within_ttl(monkeypatch):
    _positions(monkeypatch, {A: ({"ETH": {"side": "short",
                                          "notional": 9_000_000}}, 5_000_000)})
    whale_discover.discover(rows=[_row(A)], use_cache=True)
    # rows=None + a live fetch that would fail proves the cache answered
    got = whale_discover.discover(limit=5)
    assert [c["address"] for c in got] == [A]


def test_stale_cache_is_ignored(monkeypatch, tmp_path):
    with open(whale_discover.CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": 1.0, "rows": [{"address": A}]}, f)
    assert whale_discover._read_cache() is None


def test_corrupt_cache_is_ignored():
    with open(whale_discover.CACHE_FILE, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert whale_discover._read_cache() is None


# ── surfaces ─────────────────────────────────────────────────────────────────
def test_report_carries_the_honesty_disclaimer(monkeypatch):
    _positions(monkeypatch, {A: ({"ETH": {"side": "short",
                                          "notional": 58_000_000}}, 55_000_000)})
    whale_discover.discover(rows=[_row(A)], use_cache=True)
    body = whale_discover.build_report()
    assert "不是勝率" in body and "不是進場訊號" in body
    assert "ETH空" in body


def test_report_survives_the_leaderboard_being_down(monkeypatch):
    monkeypatch.setattr(whale_discover, "discover",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert "抓不到" in whale_discover.build_report()


def test_sync_dry_run_writes_nothing(monkeypatch):
    _positions(monkeypatch, {A: ({"ETH": {"side": "short",
                                          "notional": 9_000_000}}, 5_000_000)})
    whale_discover.discover(rows=[_row(A)], use_cache=True)
    before = whale_tracker.load_addresses()
    out = whale_discover.sync(count=3, dry_run=True)
    assert "預覽" in out
    assert whale_tracker.load_addresses() == before


def test_sync_adds_untracked_candidates(monkeypatch):
    whale_tracker._save_addresses([{"address": B, "label": "已有"}])
    _positions(monkeypatch, {
        A: ({"ETH": {"side": "short", "notional": 9_000_000}}, 5_000_000),
        B: ({"BTC": {"side": "long", "notional": 8_000_000}}, 5_000_000),
    })
    whale_discover.discover(rows=[_row(A), _row(B)], use_cache=True)
    whale_discover.sync(count=5)
    tracked = {whale_tracker._norm(r["address"])
               for r in whale_tracker.load_addresses()}
    assert tracked == {A, B}


def test_sync_reports_when_nothing_is_new(monkeypatch):
    whale_tracker._save_addresses([{"address": A, "label": "已有"}])
    _positions(monkeypatch, {A: ({"ETH": {"side": "short",
                                          "notional": 9_000_000}}, 5_000_000)})
    whale_discover.discover(rows=[_row(A)], use_cache=True)
    assert "沒有新的候選" in whale_discover.sync(count=5)
