"""S4 spends real money, so its entry gates get the same treatment as S3's.

Three gaps this covers, all found on 2026-08-19:
  · the account-wide daily loss brake — every other live engine consulted it
  · a ceiling on how many S4 positions can be open at once
  · ownership recorded at open, so /winrate can tell S4 from manual

Mutation-checked: removing any one of the three turns a test here red.
"""
import strategy4_exec as E


class _Client:
    def __init__(self, positions=None, boom=False):
        self._p = positions or []
        self._boom = boom

    def fetch_positions(self, *a, **k):
        if self._boom:
            raise RuntimeError("bybit 10006")
        return self._p


def _pos(sym):
    return {"symbol": sym, "contracts": 1.0}


def _clear_earlier_gates(monkeypatch, client=None):
    """Let preflight reach the concurrency cap, which runs LAST on purpose."""
    import daily_risk
    monkeypatch.setattr(daily_risk, "entry_blocked", lambda: "")
    monkeypatch.setattr(E, "bybit_symbol", lambda s: s)
    monkeypatch.setattr(E.X, "get_position", lambda *a, **k: None)
    monkeypatch.setattr(E.X, "_market_limits", lambda s: (0.001, 0.001, 1.0))
    monkeypatch.setattr(E.X, "client", lambda: client or _Client())


def test_daily_loss_brake_blocks_new_entries(monkeypatch):
    import daily_risk
    monkeypatch.setattr(daily_risk, "entry_blocked", lambda: "今日虧損達上限")
    monkeypatch.setattr(E, "bybit_symbol", lambda s: s)
    # The brake must fire BEFORE any exchange call — nothing else is stubbed.
    ok, why = E.preflight("ETH/USDT:USDT", 1900.0)
    assert ok is False and why.startswith("daily_loss_limit:"), why


def test_a_broken_brake_does_not_stop_trading(monkeypatch):
    """A risk brake that fails must fail OPEN — otherwise one bad import
    silently halts the engine and looks exactly like a quiet market."""
    import daily_risk
    monkeypatch.setattr(daily_risk, "entry_blocked",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(E, "bybit_symbol", lambda s: s)
    monkeypatch.setattr(E, "MAX_CONCURRENT", 0)
    monkeypatch.setattr(E.X, "get_position", lambda *a, **k: None)
    monkeypatch.setattr(E.X, "client", lambda: _Client())
    monkeypatch.setattr(E.X, "_market_limits", lambda s: (0.001, 0.001, 1.0))
    ok, why = E.preflight("ETH/USDT:USDT", 1900.0)
    assert ok is True, why


def test_concurrency_cap_blocks_at_the_limit(monkeypatch):
    _clear_earlier_gates(monkeypatch)
    monkeypatch.setattr(E, "MAX_CONCURRENT", 3)
    monkeypatch.setattr(E, "s4_open_count", lambda: 3)
    ok, why = E.preflight("ETH/USDT:USDT", 1900.0)
    assert ok is False and why.startswith("max_concurrent:"), why


def test_an_uncountable_position_book_blocks_rather_than_reads_zero(monkeypatch):
    """-1 means 'could not count'. Treating that as 0 free slots would be the
    fabricated zero this repo keeps rediscovering — an unmade measurement
    standing in as a permissive one."""
    _clear_earlier_gates(monkeypatch, client=_Client(boom=True))
    monkeypatch.setattr(E, "MAX_CONCURRENT", 8)
    assert E.s4_open_count() == -1
    ok, why = E.preflight("ETH/USDT:USDT", 1900.0)
    assert ok is False and why == "position_count_failed", why


def test_count_ignores_ledger_rows_with_no_live_position(monkeypatch):
    """A hand-closed position left a stale row blocking its symbol forever once
    before. The exchange is what decides whether a slot is occupied."""
    import strategy_ledger
    monkeypatch.setattr(E.X, "client",
                        lambda: _Client([_pos("ETH/USDT:USDT")]))
    monkeypatch.setattr(strategy_ledger, "_load", lambda: {"rows": [
        {"strategy": E.STRAT, "symbol": "ETH/USDT:USDT", "closed": None},
        {"strategy": E.STRAT, "symbol": "GONE/USDT:USDT", "closed": None},
        {"strategy": "S3", "symbol": "XAUT/USDT:USDT", "closed": None},
    ]})
    assert E.s4_open_count() == 1, "stale rows and other engines must not occupy S4 slots"


def test_a_fill_records_ownership(monkeypatch):
    import strategy_ledger
    seen = []
    monkeypatch.setattr(strategy_ledger, "record_open",
                        lambda *a, **k: seen.append(a))
    # Force the gate open: this test is about the LEDGER, not about whether
    # execution is armed — and since 2026-08-20 the account runs in paper mode,
    # so open_trade() short-circuits at enabled() before it can record anything.
    monkeypatch.setattr(E, "enabled", lambda: True)
    monkeypatch.setattr(E, "preflight", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(E, "_send", lambda *a, **k: {"ok": True, "qty": 1.0})
    out = E.open_trade({"symbol": "ETH/USDT:USDT", "side": "long",
                        "plan": {"entry": 100.0, "sl": 98.0, "tp": 104.0}})
    assert out.get("ok") is True
    # E.STRAT, not a literal: the name has to be the one the ledger matches
    # on, and hardcoding it here is how it drifted to lowercase unnoticed.
    assert seen and seen[0][0] == E.STRAT and seen[0][1] == "ETH/USDT:USDT", \
        "S4 opened a real position without registering it — /winrate cannot attribute it"
