"""Price Alerts — file-store + cross-detection tests (no network)."""
import time

import pytest

import price_alerts as PA


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(PA, "FILE", str(tmp_path / "price_alerts.json"))


SYM = "ETH/USDT:USDT"


# ── create / remove ──────────────────────────────────────────────────────────
def test_add_infers_direction():
    up = PA.add_alert(SYM, 3500, ref_price=3000)
    dn = PA.add_alert(SYM, 2500, ref_price=3000)
    assert up["direction"] == "above" and dn["direction"] == "below"
    assert up["base"] == "ETH"
    assert len(PA.load_alerts()) == 2


def test_add_rejects_bad_input():
    with pytest.raises(ValueError):
        PA.add_alert(SYM, 0, ref_price=3000)
    with pytest.raises(ValueError):
        PA.add_alert(SYM, 3000, ref_price=3000)   # target == market


def test_remove():
    a = PA.add_alert(SYM, 3500, ref_price=3000)
    assert PA.remove_alert(a["id"]) is True
    assert PA.remove_alert(a["id"]) is False      # already gone
    assert PA.load_alerts() == []


# ── cross detection (pure) ───────────────────────────────────────────────────
def test_fires_on_cross_only():
    now = time.time()
    above = PA.add_alert(SYM, 3500, ref_price=3000)
    alerts = PA.load_alerts()
    assert PA.check_alerts(alerts, {SYM: 3499.0}, now) == []      # not there yet
    msgs = PA.check_alerts(alerts, {SYM: 3501.0}, now)
    assert len(msgs) == 1 and "ETH crossed above 3,500" in msgs[0]
    assert alerts[0]["triggered"] == now
    # a triggered alert never fires again
    assert PA.check_alerts(alerts, {SYM: 4000.0}, now) == []
    assert above["id"] == alerts[0]["id"]


def test_below_direction_fires_downward():
    now = time.time()
    PA.add_alert(SYM, 2500, ref_price=3000)
    alerts = PA.load_alerts()
    assert PA.check_alerts(alerts, {SYM: 2600.0}, now) == []
    assert len(PA.check_alerts(alerts, {SYM: 2400.0}, now)) == 1


def test_missing_price_is_skipped():
    PA.add_alert(SYM, 3500, ref_price=3000)
    assert PA.check_alerts(PA.load_alerts(), {}, time.time()) == []


# ── retention ────────────────────────────────────────────────────────────────
def test_triggered_alerts_purge_after_retention():
    now = time.time()
    a = PA.add_alert(SYM, 3500, ref_price=3000)
    alerts = PA.load_alerts()
    alerts[0]["triggered"] = now - PA.TRIGGERED_RETAIN_SEC - 60
    fresh = PA._purge(alerts, now)
    assert fresh == []
    # an untriggered alert survives any age
    a["triggered"] = None
    assert PA._purge([a], now) == [a]
