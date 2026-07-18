"""🪞 S1 → Bybit mirror — every path fully stubbed (the conftest guard kills
strategy3_exec.client() and forces S1_BYBIT_MIRROR off; tests opt in and build
their own fakes on top).

The two safety properties that must never regress:
  1. The mirror only CLOSES quantity it opened itself — the account also holds
     Strategy-3 and the owner's manual positions.
  2. A symbol with an existing untracked position is never mirrored (One-Way
     mode would merge, and our attached stop would govern the merged position).
"""
import config
import s1_bybit_mirror as M
import strategy3_exec as X


def _wire(monkeypatch, tmp_path, *, live=True, markets=("ETH/USDT:USDT",),
          position=None, avail=1000.0):
    monkeypatch.setattr(M, "STATE_FILE", str(tmp_path / "pos.json"))
    monkeypatch.setattr(M, "_unlisted_warned", set())
    monkeypatch.setattr(config, "S1_BYBIT_MIRROR", True)
    monkeypatch.setattr(config, "S1_BYBIT_ORDER_USDT", 100.0)
    monkeypatch.setattr(config, "S1_BYBIT_LEVERAGE", 10)
    monkeypatch.setattr(X, "keys_present", lambda: True)
    monkeypatch.setattr(X, "is_live", lambda: live)

    class _Ex:
        def __init__(self):
            self.markets = {m: {} for m in markets}
        def fetch_balance(self):
            return {"info": {"result": {"list": [
                {"totalAvailableBalance": str(avail)}]}}}
        def create_order(self, *a, **k):
            calls["orders"].append((a, k))
            return {"id": "x"}

    calls = {"orders": [], "opens": [], "stops": [], "tg": []}
    monkeypatch.setattr(X, "client", lambda: _Ex())
    monkeypatch.setattr(X, "get_position", lambda s: position)
    monkeypatch.setattr(X, "_market_limits", lambda s: (0.01, 0.01, 5.0))
    monkeypatch.setattr(X, "set_stop", lambda s, p: calls["stops"].append((s, p)))

    def fake_open(sym, d, price, sl, margin, lev):
        calls["opens"].append({"sym": sym, "dir": d, "price": price, "sl": sl,
                               "margin": margin, "lev": lev})
        return {"ok": True, "dry": not live, "qty": margin * lev / price}
    monkeypatch.setattr(X, "open_flip", fake_open)
    monkeypatch.setattr(M, "_tg", lambda msg: calls["tg"].append(msg))
    return calls


# ── the off switch ───────────────────────────────────────────────────────────
def test_disabled_by_default_does_nothing(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "S1_BYBIT_MIRROR", False)
    assert not M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    assert not M.mirror_close("ETH/USDT:USDT", "sl")
    assert calls["opens"] == [] and calls["orders"] == []


# ── entries ──────────────────────────────────────────────────────────────────
def test_open_uses_fixed_100_usdt_order_value(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    assert M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    o = calls["opens"][0]
    assert o["margin"] * o["lev"] == 100.0            # notional = 100 USDT exactly
    assert o["sym"] == "ETH/USDT:USDT" and o["dir"] == "long"
    assert o["sl"] == 3430.0
    assert M._load()["ETH/USDT:USDT"]["side"] == "long"   # tracked for later close


def test_open_maps_binance_symbol_and_short(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, markets=("SOL/USDT:USDT",))
    assert M.mirror_open("SOL/USDT", "SHORT", 150.0, 155.0)
    assert calls["opens"][0]["sym"] == "SOL/USDT:USDT"
    assert calls["opens"][0]["dir"] == "short"


def test_open_skips_symbol_with_untracked_position(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path,
                  position={"side": "long", "qty": 5.0, "entry": 100.0, "sl": None})
    assert not M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    assert calls["opens"] == []                        # protected, no order
    assert any("保護" in m for m in calls["tg"])


def test_open_skips_unlisted_symbol_with_one_note(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, markets=("BTC/USDT:USDT",))
    assert not M.mirror_open("PEPE1000/USDT:USDT", "long", 0.01, 0.009)
    assert not M.mirror_open("PEPE1000/USDT:USDT", "long", 0.01, 0.009)
    assert calls["opens"] == []
    assert sum("未上市" in m for m in calls["tg"]) == 1   # noted once, not twice


def test_open_skips_on_insufficient_margin(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, avail=5.0)    # need 10×1.3 = 13
    assert not M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    assert calls["opens"] == []
    assert any("保證金不足" in m for m in calls["tg"])


def test_open_no_duplicate_when_already_tracked(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    assert M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    assert not M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    assert len(calls["opens"]) == 1


# ── TP1: half off + breakeven ────────────────────────────────────────────────
def test_tp1_closes_half_and_moves_stop_to_entry(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)     # qty ≈ 0.02857
    assert M.mirror_tp1("ETH/USDT:USDT", "LONG", 3500.0)
    (a, k), = calls["orders"]
    assert a[2] == "sell" and k["params"]["reduceOnly"] is True
    assert abs(a[3] - 0.01) < 1e-9                     # half of 0.0285, floored to step
    assert calls["stops"] == [("ETH/USDT:USDT", 3500.0)]
    assert M._load()["ETH/USDT:USDT"]["qty"] < 0.02    # tracked qty reduced


def test_tp1_untracked_symbol_is_ignored(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    assert not M.mirror_tp1("XAUT/USDT:USDT", "long", 2000.0)  # S3's symbol!
    assert calls["orders"] == [] and calls["stops"] == []


# ── exits ────────────────────────────────────────────────────────────────────
def test_close_only_touches_tracked_qty(monkeypatch, tmp_path):
    # Bybit reports 5.0 contracts (we opened 0.0285, the rest is manual) —
    # the mirror must close min(tracked, live), never the whole 5.0.
    calls = _wire(monkeypatch, tmp_path)
    M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    monkeypatch.setattr(X, "get_position",
                        lambda s: {"side": "long", "qty": 5.0, "entry": 3500.0, "sl": None})
    assert M.mirror_close("ETH/USDT:USDT", "tp2")
    (a, k), = calls["orders"]
    assert a[3] < 0.03 and k["params"]["reduceOnly"] is True
    assert M._load() == {}                             # untracked after close


def test_close_reconciles_when_bybit_stop_already_fired(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    monkeypatch.setattr(X, "get_position", lambda s: None)     # server-side SL filled
    assert M.mirror_close("ETH/USDT:USDT", "sl")
    assert calls["orders"] == []                       # nothing left to close
    assert M._load() == {}
    assert any("已自行出場" in m for m in calls["tg"])


def test_close_keeps_tracking_on_failure(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    monkeypatch.setattr(X, "get_position",
                        lambda s: {"side": "long", "qty": 0.02, "entry": 3500.0, "sl": None})
    monkeypatch.setattr(M, "_reduce", lambda s, side, q: "network down")
    assert not M.mirror_close("ETH/USDT:USDT", "sl")
    assert "ETH/USDT:USDT" in M._load()                # still tracked → retry-able
    assert any("請檢查 Bybit" in m for m in calls["tg"])


# ── dry-run obeys the master switch ──────────────────────────────────────────
def test_dry_run_places_no_orders_and_tracks_nothing(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, live=False)
    assert M.mirror_open("ETH/USDT:USDT", "long", 3500.0, 3430.0)
    assert calls["opens"][0]                           # open_flip called (its own dry-run)
    assert M._load() == {}                             # nothing tracked in dry-run
    assert calls["orders"] == []
