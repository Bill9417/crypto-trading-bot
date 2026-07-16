"""📈 S1 交易訊號 copy-trade feed — the Strategy-1 follow signals.

Covers the 2026-07-16 addition (a followable S1 feed so others can mirror the
live trades):
  • s1_follow_card  — 中文 entry/queued card: 方向, 進場/停損/目標 block, 槓桿,
    R:R, Bybit 現價 with an honest Binance fallback, 免責聲明
  • s1_follow_exit   — TP1/停損/止盈/保本 one-liners, correct sign handling
  • s1_follow_cancel — 取消掛單 notice so a mirrored limit gets pulled
  • notify_s1_follow — routes to its own topic (channel="s1signals")

Bybit is offline in tests (conftest stubs bybit_data._exchange), so the cards
exercise the honest fallback path exactly as they would if Bybit were down.
"""
import bot
import telegram_utils


# ── entry / queued card ──────────────────────────────────────────────────────
def test_follow_card_long_is_chinese_bybit_and_complete():
    msg = bot.s1_follow_card("⏳ 掛單 待成交 QUEUED", "ETH/USDT:USDT", "LONG",
                             3500.0, 3430.0, 3552.5, 3640.0, timeframe="1h")
    assert "🟢 做多 LONG" in msg and "ETH/USDT" in msg
    assert "進場 Entry" in msg and "停損 SL" in msg
    assert "目標1 TP1" in msg and "目標2 TP2" in msg
    assert f"本倉槓桿 {bot.LEVERAGE}x" in msg
    assert "風險報酬 2.0R" in msg           # reward 140 / risk 70
    assert "非投資建議" in msg
    # Bybit offline in tests → honest fallback + TradingView chart link, no crash
    assert "參考價" in msg and "(Binance)" in msg
    assert "圖表" in msg


def test_follow_card_short_signs_and_rr():
    # short: SL ABOVE entry (unfavourable −%), TPs BELOW entry (favourable +%)
    msg = bot.s1_follow_card("✅ 進場成交 FILLED", "SOL/USDT:USDT", "SHORT",
                             100.0, 104.0, 97.0, 92.0)
    assert "🔴 做空 SHORT" in msg
    assert "(-4.0%)" in msg                  # SL moved against the short
    assert "(+3.0%)" in msg and "(+8.0%)" in msg   # TP1 / TP2 favourable
    assert "風險報酬 2.0R" in msg            # reward 8 / risk 4


# ── exit one-liners ──────────────────────────────────────────────────────────
def test_follow_exit_tp_and_sl_sign_handling():
    tp1 = bot.s1_follow_exit("tp1", "ETH/USDT:USDT", "LONG", 1.5,
                             note="先平一半")
    assert "🎯 TP1 達標" in tp1 and "做多" in tp1 and "+1.5%" in tp1 and "先平一半" in tp1

    sl = bot.s1_follow_exit("sl", "ETH/USDT:USDT", "LONG", -2.0)
    assert "🛑 停損出場" in sl and "-2.0%" in sl and "+-" not in sl   # no double sign

    tp2 = bot.s1_follow_exit("tp2", "ETH/USDT:USDT", "SHORT", 4.0, note="全部平倉")
    assert "🏆 止盈達標" in tp2 and "做空" in tp2 and "全部平倉" in tp2

    be = bot.s1_follow_exit("be", "ETH/USDT:USDT", "LONG", 0.7)
    assert "⚖️ 保本出場" in be and "+0.7%" in be


# ── cancel notice ────────────────────────────────────────────────────────────
def test_follow_cancel_tells_followers_to_pull_limit():
    msg = bot.s1_follow_cancel("ETH/USDT:USDT", "LONG", "價格已先觸及目標、未成交")
    assert "🚫 取消掛單" in msg and "做多" in msg
    assert "價格已先觸及目標" in msg and "如已掛單請一併取消" in msg


# ── routing ──────────────────────────────────────────────────────────────────
def test_notify_s1_follow_routes_to_its_own_topic(monkeypatch):
    captured = {}

    def _fake_send(message, parse_mode=None, *, channel="alerts", **kw):
        captured.update(channel=channel, parse_mode=parse_mode)
        return True

    monkeypatch.setattr(bot, "send_message", _fake_send)
    bot.notify_s1_follow("hi")
    assert captured["channel"] == "s1signals"
    assert captured["parse_mode"] == "HTML"


def test_s1signals_channel_is_registered():
    assert "s1signals" in telegram_utils._TOPIC_THREAD
