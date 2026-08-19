"""Ownership must survive a second engine holding the same symbol.

record_open() deduped on the SYMBOL alone, so one stale row from another
engine made the call a silent no-op: the second engine's real position was
never registered, its trades fell through to infer() as 手動交易, and any
per-strategy count of open positions undercounted. Found 2026-08-19, when S4
had been placing real Bybit orders for days and every one of them was filed as
manual.
"""
import json

import strategy_ledger as L
import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "LEDGER_FILE", str(tmp_path / "ledger.json"))


def _rows():
    with open(L.LEDGER_FILE, encoding="utf-8") as f:
        return json.load(f)["rows"]


def test_a_second_engine_registers_behind_a_stale_row():
    L.record_open("S3", "ETH/USDT:USDT", "long")      # never closed
    L.record_open("S4", "ETH/USDT:USDT", "short")
    owners = {r["strategy"] for r in _rows() if r["symbol"] == "ETHUSDT"}
    assert owners == {"S3", "S4"}, \
        "the second engine's position was dropped — it will read as 手動交易"


def test_calling_twice_is_still_a_no_op():
    L.record_open("S4", "ETH/USDT:USDT", "short")
    L.record_open("S4", "ETH/USDT:USDT", "short")
    assert len([r for r in _rows() if r["strategy"] == "S4"]) == 1


def test_close_ends_the_callers_own_row_not_someone_elses():
    # S4 FIRST, so its row is not the one reversed() reaches first. With both
    # orders the symbol-only match happens to pick S4 and the test cannot
    # fail — which is what the first version of it did.
    L.record_open("S4", "ETH/USDT:USDT", "short")
    L.record_open("S3", "ETH/USDT:USDT", "long")
    L.record_close("S4", "ETH/USDT:USDT")
    by = {r["strategy"]: r["closed"] for r in _rows() if r["symbol"] == "ETHUSDT"}
    assert by["S4"] is not None, "S4's own row was not closed"
    assert by["S3"] is None, "S4 closed S3's record"


def test_close_still_falls_back_when_the_owner_is_unknown():
    """Every existing caller relies on symbol-only matching; a reconciler that
    does not know the owner must still close something."""
    L.record_open("S3", "ETH/USDT:USDT", "long")
    L.record_close("S1", "ETH/USDT:USDT", by=L.MANUAL)
    row = next(r for r in _rows() if r["symbol"] == "ETHUSDT")
    assert row["closed"] is not None and row["closed_by"] == L.MANUAL


def test_s4_is_a_known_strategy_everywhere_it_is_matched():
    assert "S4" in L.STRATEGIES
    assert "S4" in L._LABEL
    import strategy4_exec as E
    assert E.STRAT in L.STRATEGIES, \
        "S4 writes rows under a name the report does not recognise"


def test_a_recorded_s4_trade_is_attributed_to_s4_not_manual():
    """The whole point. Before 2026-08-19 S4 wrote no row, so attribute() took
    the `unrecorded` branch and filed every live S4 trade as 手動交易."""
    L.record_open("S4", "PLTR/USDT:USDT", "short", ts=1_700_000_000.0)
    L.record_close("S4", "PLTR/USDT:USDT", ts=1_700_003_600.0)
    trades = [{"symbol": "PLTRUSDT", "time": 1_700_003_600_000, "pnl": 1.5,
               "notional": 40.0, "lev": 5.0}]
    tagged = L.attribute(trades)
    assert tagged[0]["strategy"] == "S4", tagged[0]
    assert tagged[0]["attrib"] == "recorded"
    stats = L.split_summary(trades)
    assert "S4" in stats, "S4 trades vanish from the per-strategy breakdown"
    assert "S4 TradFi" in L.report(trades), "the report has no label for S4"


def test_an_unrecorded_s4_trade_is_what_the_bug_looked_like():
    """Pins the failure mode itself: no ledger row and S4's size fingerprint
    matches nothing, so it lands in manual."""
    assert L.infer("PLTR/USDT:USDT", 40.0, 5) == L.MANUAL


def test_an_unknown_strategy_name_is_loud_but_still_recorded(capsys):
    """S4 wrote "s4" for a day while every matcher used "S4": a record that
    existed and was invisible. Warn, but never drop the row — losing the
    attribution is worse than a name that can be migrated."""
    L.record_open("s4", "VRT/USDT:USDT", "short")
    out = capsys.readouterr().out
    assert "unknown strategy" in out and "'s4'" in out
    assert any(r["strategy"] == "s4" for r in _rows()), \
        "the row was dropped — the trade is now unattributable"
