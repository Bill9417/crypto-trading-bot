"""Telegram command bot — pure-logic tests (no network, no thread)."""
import time

import tg_commands as TG


# ── parsing / auth ───────────────────────────────────────────────────────────
def test_parse_command_variants():
    assert TG.parse_command("/winrate") == ("winrate", "")
    assert TG.parse_command("/winrate@WolfBot") == ("winrate", "")
    assert TG.parse_command("/positions  now ") == ("positions", "now")
    assert TG.parse_command("hello") is None
    assert TG.parse_command("") is None
    assert TG.parse_command("/") is None


def test_allowed_chats_only(monkeypatch):
    monkeypatch.setattr(TG, "ALLOWED_CHATS", {"-100123", "555"})
    assert TG.allowed(-100123) and TG.allowed("555")
    assert not TG.allowed(999) and not TG.allowed(None)


def test_unknown_command_stays_silent():
    assert TG.handle("definitely_not_a_command") is None


def test_api_telegram_timeout_param_does_not_collide(monkeypatch):
    """Regression (live 2026-07-11): Telegram's getUpdates long-poll parameter
    is named `timeout`, and _api's HTTP timeout arg shadowed it — every poll
    died with 'got multiple values for argument timeout' and the bot never
    answered. Both timeouts must land in their own places."""
    captured = {}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "result": []}

    def fake_post(url, data=None, timeout=None):
        captured.update(url=url, data=data, http=timeout)
        return _R()

    monkeypatch.setattr(TG.requests, "post", fake_post)
    TG._api("getUpdates", http_timeout=45, timeout=25, offset=7)
    assert captured["http"] == 45                      # HTTP-layer timeout
    assert captured["data"]["timeout"] == 25           # Telegram long-poll param
    assert captured["data"]["offset"] == 7
    assert captured["url"].endswith("/getUpdates")


# ── /winrate formatting ──────────────────────────────────────────────────────
def _summary(**kw):
    base = {"ok": True, "n_trades": 10, "wins": 6, "losses": 4, "win_rate": 60.0,
            "net": 4.2, "profit_factor": 1.31, "avg_win": 1.5, "avg_loss": -1.2,
            "max_drawdown": -3.1, "streak": 2, "streak_type": "W",
            "daily": [{"date": "d", "net": 0.5}] * 7}
    base.update(kw)
    return base


def test_fmt_winrate_reports_both_accounts():
    msg = TG.fmt_winrate(_summary(), _summary(n_trades=40, win_rate=57.5, net=-104.29))
    assert "Binance" in msg and "Bybit" in msg
    assert "6W / 4L" in msg and "60.0%" in msg
    assert "-104.29" in msg
    assert "PF 1.31" in msg and "7d +3.50" in msg
    assert "win rate alone means nothing" in msg      # the honesty footer


def test_fmt_winrate_degrades_on_error():
    msg = TG.fmt_winrate({"ok": False, "error": "boom"}, _summary(n_trades=0))
    assert "unavailable (boom)" in msg
    assert "no closed trades yet" in msg


# ── /positions /signals /alerts formatting ───────────────────────────────────
def test_fmt_positions():
    binance = {"ok": True, "positions": [
        {"symbol": "ETH/USDT:USDT", "side": "LONG", "entry": 1800.0,
         "unrealized_pnl": 0.42, "pnl_pct": 2.1}]}
    msg = TG.fmt_positions(binance, {"ok": True, "positions": []})
    assert "ETH LONG" in msg and "+0.42" in msg and "flat" in msg


def test_fmt_signals_with_plan():
    payload = {"timeframe": "15m", "signals": [
        {"base": "ETH", "direction": "long", "score": 85, "ts": time.time() - 300,
         "entry": 1800.0, "sl": 1782.0, "tp1": 1818.0, "tp2": 1836.0}]}
    msg = TG.fmt_signals(payload)
    assert "ETH LONG" in msg and "85/100" in msg
    assert "SL 1,782" in msg and "TP2 1,836" in msg


def test_fmt_signals_empty():
    assert "No S2 signals" in TG.fmt_signals({"signals": []})


def test_fmt_alerts():
    alerts = [{"base": "BTC", "direction": "above", "price": 70000.0, "triggered": None},
              {"base": "ETH", "direction": "below", "price": 1700.0,
               "triggered": 1.0, "triggered_price": 1699.0}]
    msg = TG.fmt_alerts(alerts)
    assert "BTC ▲ above 70,000" in msg and "ETH fired @ 1,699" in msg
    assert "No price alerts" in TG.fmt_alerts([])
