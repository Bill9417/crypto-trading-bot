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
    assert "6勝 / 4敗" in msg and "60.0%" in msg
    assert "-104.29" in msg
    assert "PF 1.31" in msg and "7 日 +3.50" in msg
    assert "勝率不代表賺錢" in msg                     # the honesty footer


def test_fmt_winrate_degrades_on_error():
    msg = TG.fmt_winrate({"ok": False, "error": "boom"}, _summary(n_trades=0))
    assert "暫時無法取得（boom）" in msg
    assert "尚無平倉紀錄" in msg


# ── /positions /signals /alerts formatting ───────────────────────────────────
def test_fmt_positions():
    binance = {"ok": True, "positions": [
        {"symbol": "ETH/USDT:USDT", "side": "LONG", "entry": 1800.0,
         "unrealized_pnl": 0.42, "pnl_pct": 2.1}]}
    msg = TG.fmt_positions(binance, {"ok": True, "positions": []})
    assert "ETH 做多" in msg and "+0.42" in msg and "無持倉" in msg


def test_fmt_signals_with_plan():
    payload = {"timeframe": "15m", "signals": [
        {"base": "ETH", "direction": "long", "score": 85, "ts": time.time() - 300,
         "entry": 1800.0, "sl": 1782.0, "tp1": 1818.0, "tp2": 1836.0,
         "premium": True}]}
    msg = TG.fmt_signals(payload)
    assert "ETH 做多 ⭐" in msg and "信心 85" in msg and "85/100" not in msg
    assert "停損 1,782" in msg and "目標2 1,836" in msg


def test_fmt_signals_empty():
    assert "沒有 S2 訊號" in TG.fmt_signals({"signals": []})


def test_fmt_alerts():
    alerts = [{"base": "BTC", "direction": "above", "price": 70000.0, "triggered": None},
              {"base": "ETH", "direction": "below", "price": 1700.0,
               "triggered": 1.0, "triggered_price": 1699.0}]
    msg = TG.fmt_alerts(alerts)
    assert "BTC ▲ 突破 70,000" in msg and "ETH 已觸發 @ 1,699" in msg
    assert "尚未設定到價提醒" in TG.fmt_alerts([])


# ── /guide + welcome (the promo surface) ─────────────────────────────────────
def test_guide_is_returned_and_honest():
    msg = TG.handle("guide")
    assert "群組導覽" in msg and "主題頻道" in msg
    assert "/outcomes" in msg and "/help" in msg
    assert "非投資建議" in msg and "勝率不是保證" in msg   # honesty policy leads
    # the guide is PUBLIC promo text — it must never mention account facts
    for banned in ("餘額", "USDT", "持倉部位"):
        assert banned not in msg


def test_guide_aliases():
    assert TG.handle("about") == TG.handle("guide") == TG.handle("intro")


def test_welcome_text_greets_and_points_to_guide():
    msg = TG.welcome_text(["小明", "Ada"])
    assert "歡迎 小明、Ada" in msg
    assert "/guide" in msg and "/help" in msg and "非投資建議" in msg


def test_welcome_text_handles_empty_names():
    assert "歡迎 新朋友" in TG.welcome_text(["", None])


def test_maybe_welcome_rate_limited(monkeypatch):
    sent = []
    monkeypatch.setattr(TG, "_reply", lambda c, t, m: sent.append(m) or True)
    monkeypatch.setattr(TG, "_last_welcome", 0.0)
    assert TG._maybe_welcome(1, None, [{"first_name": "Bob"}])
    assert not TG._maybe_welcome(1, None, [{"first_name": "Eve"}])  # inside gap
    assert len(sent) == 1 and "Bob" in sent[0]


# ── /price formatting ────────────────────────────────────────────────────────
def test_fmt_price_reply_mixes_hits_and_misses():
    rows = [("BTC", {"last": 108432.1, "pct": 1.23}),
            ("ETH", {"last": 3520.5, "pct": -2.0}),
            ("NOPE", None)]
    msg = TG.fmt_price_reply(rows)
    assert "BTC 108,432.1 · +1.2%" in msg
    assert "ETH 3,520.5 · −2.0%" in msg          # real minus glyph
    assert "NOPE — 查無此幣" in msg


def test_handle_price_defaults_and_cap(monkeypatch):
    asked = []
    monkeypatch.setattr(TG, "_fetch_price",
                        lambda b: asked.append(b) or {"last": 1.0, "pct": 0.0})
    TG.handle_price("")
    assert asked == list(TG.PRICE_DEFAULT)
    asked.clear()
    TG.handle_price("btc eth sol bnb doge pepe xrp ada")   # 8 asked → capped
    assert len(asked) == TG.PRICE_MAX and asked[0] == "BTC"
