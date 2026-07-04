"""executor.ensure_stop_losses() — guardian failure MUST be alerted, not silently
discarded.

SAFETY: every test here monkeypatches the executor / exchange, so NOTHING
reaches Binance — safe to run while the live bot trades.

Regression for a 2026-07-04 finding: a failed set_protection() call landed in
the `skipped` list, but the ONLY caller (strategy2_scanner.py's guard loop)
discards the return value entirely — so a naked position that the guardian
tried and FAILED to protect had zero notification, for as long as the failure
persisted. `protected` (success) already sent a Telegram message; failure did
not. Fixed by alerting on real failures too (with a cooldown so a persistent
failure doesn't spam a message every ~5 min sweep)."""
import executor as E


def _naked_position(symbol="SOL/USDT:USDT", side="long"):
    return {"symbol": symbol, "side": side, "markPrice": 100.0, "entryPrice": 100.0,
            "contracts": 1.0}


def test_guardian_alerts_on_protection_failure(monkeypatch):
    sent = []
    monkeypatch.setattr(E, "is_live", lambda: True)

    class FakeEx:
        def fetch_positions(self):
            return [_naked_position()]
    monkeypatch.setattr(E, "_get_exchange", lambda: FakeEx())
    monkeypatch.setattr(E, "_fetch_protective_orders", lambda ex, sym: [])  # no existing SL
    monkeypatch.setattr(E, "set_protection", lambda symbol, sl=None, tp=None: {
        "ok": False, "error": "Binance rejected: precision"})
    monkeypatch.setattr(E, "send_message", lambda msg: sent.append(msg))
    E._last_guardian_fail_alert.clear()

    result = E.ensure_stop_losses()

    assert result["ok"] is True
    assert result["protected"] == []
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"] == "Binance rejected: precision"
    # the critical assertion: the failure MUST reach Telegram, not just the
    # discarded return value
    assert len(sent) == 1
    assert "FAILED to protect" in sent[0]
    assert "SOL/USDT:USDT" in sent[0]


def test_guardian_does_not_alert_for_already_protected(monkeypatch):
    """A position that already HAS a stop is routine, not a failure — must
    never trigger the failure alert."""
    sent = []
    monkeypatch.setattr(E, "is_live", lambda: True)

    class FakeEx:
        def fetch_positions(self):
            return [_naked_position()]
    monkeypatch.setattr(E, "_get_exchange", lambda: FakeEx())
    monkeypatch.setattr(E, "_fetch_protective_orders", lambda ex, sym: [
        {"side": "sell", "triggerPrice": 95.0}])
    monkeypatch.setattr(E, "_trigger_role", lambda side, trig, mark: "sl")
    monkeypatch.setattr(E, "send_message", lambda msg: sent.append(msg))
    E._last_guardian_fail_alert.clear()

    result = E.ensure_stop_losses()

    assert result["protected"] == []
    assert result["skipped"][0]["reason"] == "already protected"
    assert sent == []                       # no alert for the routine case


def test_guardian_failure_alert_has_a_cooldown(monkeypatch):
    """A persistent failure must not spam a message on every single sweep."""
    sent = []
    monkeypatch.setattr(E, "is_live", lambda: True)

    class FakeEx:
        def fetch_positions(self):
            return [_naked_position()]
    monkeypatch.setattr(E, "_get_exchange", lambda: FakeEx())
    monkeypatch.setattr(E, "_fetch_protective_orders", lambda ex, sym: [])
    monkeypatch.setattr(E, "set_protection", lambda symbol, sl=None, tp=None: {
        "ok": False, "error": "still failing"})
    monkeypatch.setattr(E, "send_message", lambda msg: sent.append(msg))
    E._last_guardian_fail_alert.clear()

    E.ensure_stop_losses()
    E.ensure_stop_losses()                  # immediately again — same cooldown window
    assert len(sent) == 1                   # only the first call alerts

    E._last_guardian_fail_alert["SOL/USDT:USDT"] = 0   # simulate the cooldown elapsing
    E.ensure_stop_losses()
    assert len(sent) == 2                   # elapsed cooldown → alerts again
