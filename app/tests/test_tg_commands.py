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
    assert "6勝 4敗" in msg and "60.0%" in msg
    assert "-104.29" in msg
    assert "PF 1.31" in msg and "+3.50" in msg
    assert "<pre>" in msg                             # aligned stat tables
    assert "勝率不代表賺錢" in msg                     # the honesty footer


def test_fmt_winrate_labels_the_bybit_panel_as_the_whole_account():
    """S1's mirror, S3 and manual trades all settle on that sub-account —
    calling the panel 'S3' claimed a track record that wasn't S3's."""
    msg = TG.fmt_winrate(_summary(), _summary())
    assert "S1+S3+手動" in msg


def test_fmt_winrate_split_is_owner_only():
    trades = [{"symbol": "XAUTUSDT", "pnl": 3.0, "time": 1_784_900_000_000,
               "notional": 1200.0, "lev": 50.0}]
    public = TG.fmt_winrate(_summary(), _summary(trades=trades))
    owner = TG.fmt_winrate(_summary(), _summary(trades=trades), owner=True)
    assert "各策略實際損益" not in public
    assert "各策略實際損益" in owner and "S3 翻轉引擎" in owner


def test_fmt_winrate_survives_a_broken_split(monkeypatch):
    import strategy_ledger
    monkeypatch.setattr(strategy_ledger, "report",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    msg = TG.fmt_winrate(_summary(), _summary(trades=[{"symbol": "X", "pnl": 1.0,
                                                       "time": 0}]), owner=True)
    assert "勝率不代表賺錢" in msg              # the report still arrives
    assert "拆帳暫時無法計算" in msg


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
    row = next(ln for ln in msg.splitlines() if "ETH" in ln)
    assert "做多" in row and "+0.42" in row
    assert "無持倉" in msg


def test_fmt_signals_with_plan():
    payload = {"timeframe": "15m", "signals": [
        {"base": "ETH", "direction": "long", "score": 85, "ts": time.time() - 300,
         "entry": 1800.0, "sl": 1782.0, "tp1": 1818.0, "tp2": 1836.0,
         "premium": True}]}
    msg = TG.fmt_signals(payload)
    row = next(ln for ln in msg.splitlines() if "ETH" in ln)
    assert "做多⭐" in row and "85" in row and "85/100" not in msg
    assert "1,782" in row and "1,836" in row      # 停損/目標 columns
    assert "<pre>" in msg


def test_fmt_signals_empty():
    assert "沒有 S2 訊號" in TG.fmt_signals({"signals": []})


def test_fmt_alerts():
    alerts = [{"base": "BTC", "direction": "above", "price": 70000.0, "triggered": None},
              {"base": "ETH", "direction": "below", "price": 1700.0,
               "triggered": 1.0, "triggered_price": 1699.0}]
    msg = TG.fmt_alerts(alerts)
    btc = next(ln for ln in msg.splitlines() if "BTC" in ln)
    eth = next(ln for ln in msg.splitlines() if "ETH" in ln)
    assert "▲ 突破" in btc and "70,000" in btc
    assert "已觸發" in eth and "1,699" in eth
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
    monkeypatch.setattr(TG, "_reply",
                        lambda c, t, m: (sent.append(m) or True, 999))
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
    btc = next(ln for ln in msg.splitlines() if "BTC" in ln)
    eth = next(ln for ln in msg.splitlines() if "ETH" in ln)
    assert "108,432.1" in btc and "+1.2%" in btc
    assert "3,520.5" in eth and "−2.0%" in eth   # real minus glyph
    assert "查無此幣" in msg and "<pre>" in msg


def test_handle_price_defaults_and_cap(monkeypatch):
    asked = []
    monkeypatch.setattr(TG, "_fetch_price",
                        lambda b: asked.append(b) or {"last": 1.0, "pct": 0.0})
    TG.handle_price("")
    assert asked == list(TG.PRICE_DEFAULT)
    asked.clear()
    TG.handle_price("btc eth sol bnb doge pepe xrp ada")   # 8 asked → capped
    assert len(asked) == TG.PRICE_MAX and asked[0] == "BTC"


# ── /guide auto-pin decision ─────────────────────────────────────────────────
def test_should_pin_guide_first_time(monkeypatch):
    monkeypatch.setattr(TG.config, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    assert TG._should_pin_guide({}, "guide", "-100123", True, 55) is True


def test_should_pin_guide_aliases_all_qualify(monkeypatch):
    monkeypatch.setattr(TG.config, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    for cmd in TG.GUIDE_CMDS:
        assert TG._should_pin_guide({}, cmd, "-100123", True, 55) is True


def test_should_not_pin_guide_twice(monkeypatch):
    monkeypatch.setattr(TG.config, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    state = {"guide_pinned_mid": 40}
    assert TG._should_pin_guide(state, "guide", "-100123", True, 55) is False


def test_should_not_pin_guide_in_a_dm(monkeypatch):
    monkeypatch.setattr(TG.config, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    assert TG._should_pin_guide({}, "guide", "777", True, 55) is False   # owner DM


def test_should_not_pin_non_guide_commands(monkeypatch):
    monkeypatch.setattr(TG.config, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    assert TG._should_pin_guide({}, "price", "-100123", True, 55) is False


def test_should_not_pin_when_delivery_failed_or_no_mid(monkeypatch):
    monkeypatch.setattr(TG.config, "TELEGRAM_GROUP_CHAT_ID", "-100123")
    assert TG._should_pin_guide({}, "guide", "-100123", False, 55) is False
    assert TG._should_pin_guide({}, "guide", "-100123", True, None) is False


def test_reply_returns_ok_and_message_id(monkeypatch):
    monkeypatch.setattr(TG.telegram_utils, "_pace", lambda: None)
    monkeypatch.setattr(TG.telegram_utils, "_record_sent", lambda *a, **kw: None)
    monkeypatch.setattr(TG.config, "BOT_TOKEN", "tok")

    class _R:
        status_code = 200
        ok = True

        def json(self):
            return {"result": {"message_id": 321}}

    monkeypatch.setattr(TG.requests, "post", lambda url, **kw: _R())
    ok, mid = TG._reply("-100123", None, "hi")
    assert ok is True and mid == 321


# ── 🔄 refresh keyboard (inline buttons) ──────────────────────────────────────
def test_refresh_keyboard_shape():
    kb = TG.refresh_keyboard("positions")
    assert kb == {"inline_keyboard": [[{"text": "🔄 Refresh",
                                        "callback_data": "r:positions:"}]]}


def test_refresh_keyboard_carries_args():
    kb = TG.refresh_keyboard("price", "btc eth")
    assert kb["inline_keyboard"][0][0]["callback_data"] == "r:price:btc eth"


def test_parse_refresh_callback_roundtrip():
    assert TG.parse_refresh_callback("r:positions:") == ("positions", "")
    assert TG.parse_refresh_callback("r:price:btc eth") == ("price", "btc eth")


def test_parse_refresh_callback_rejects_other_data():
    assert TG.parse_refresh_callback("something_else") is None
    assert TG.parse_refresh_callback("") is None
    assert TG.parse_refresh_callback(None) is None


def test_edit_message_success(monkeypatch):
    calls = []
    monkeypatch.setattr(TG.telegram_utils, "_pace", lambda: None)
    monkeypatch.setattr(TG.config, "BOT_TOKEN", "tok")

    class _R:
        ok = True

    monkeypatch.setattr(TG.requests, "post",
                        lambda url, **kw: calls.append((url, kw)) or _R())
    assert TG._edit_message("-100123", 55, "new text") is True
    assert calls[0][0].endswith("/editMessageText")
    assert calls[0][1]["data"]["message_id"] == 55


def test_edit_message_not_modified_is_not_an_error(monkeypatch):
    monkeypatch.setattr(TG.telegram_utils, "_pace", lambda: None)
    monkeypatch.setattr(TG.config, "BOT_TOKEN", "tok")

    class _R:
        ok = False
        text = '{"description": "Bad Request: message is not modified"}'

        def json(self):
            return {"description": "Bad Request: message is not modified"}

    monkeypatch.setattr(TG.requests, "post", lambda url, **kw: _R())
    assert TG._edit_message("-100123", 55, "same text") is True


def test_edit_message_real_failure_returns_false(monkeypatch):
    monkeypatch.setattr(TG.telegram_utils, "_pace", lambda: None)
    monkeypatch.setattr(TG.config, "BOT_TOKEN", "tok")

    class _R:
        ok = False
        text = '{"description": "Forbidden: bot was blocked"}'

        def json(self):
            return {"description": "Forbidden: bot was blocked"}

    monkeypatch.setattr(TG.requests, "post", lambda url, **kw: _R())
    assert TG._edit_message("-100123", 55, "x") is False


def test_handle_callback_edits_message_and_answers(monkeypatch):
    monkeypatch.setattr(TG, "ALLOWED_CHATS", {"-100123"})
    edited = []
    answered = []
    monkeypatch.setattr(TG, "_edit_message",
                        lambda *a, **kw: edited.append(a) or True)
    monkeypatch.setattr(TG, "_answer_callback",
                        lambda cb_id, text="": answered.append((cb_id, text)))
    monkeypatch.setattr(TG, "handle", lambda cmd, args: "🔔 到價提醒\n...")
    cb = {"id": "cbid1", "data": "r:alerts:",
          "message": {"chat": {"id": -100123}, "message_id": 77}}
    TG._handle_callback(cb)
    assert edited and edited[0][0] == -100123 and edited[0][1] == 77
    assert answered == [("cbid1", "✅ 已更新")]


def test_handle_callback_rejects_unknown_command(monkeypatch):
    monkeypatch.setattr(TG, "ALLOWED_CHATS", {"-100123"})
    answered = []
    monkeypatch.setattr(TG, "_answer_callback",
                        lambda cb_id, text="": answered.append(text))
    edited = []
    monkeypatch.setattr(TG, "_edit_message", lambda *a, **kw: edited.append(a))
    cb = {"id": "cbid2", "data": "r:halt:",       # not refreshable
          "message": {"chat": {"id": -100123}, "message_id": 77}}
    TG._handle_callback(cb)
    assert not edited
    assert answered == ["⛔ 無權限"]


def test_handle_callback_rejects_unallowed_chat(monkeypatch):
    monkeypatch.setattr(TG, "ALLOWED_CHATS", {"-100123"})
    answered = []
    monkeypatch.setattr(TG, "_answer_callback",
                        lambda cb_id, text="": answered.append(text))
    cb = {"id": "cbid3", "data": "r:positions:",
          "message": {"chat": {"id": -999999}, "message_id": 1}}
    TG._handle_callback(cb)
    assert answered == ["⛔ 無權限"]


def test_handle_callback_malformed_data_stays_silent(monkeypatch):
    answered = []
    monkeypatch.setattr(TG, "_answer_callback",
                        lambda cb_id, text="": answered.append(text))
    cb = {"id": "cbid4", "data": "garbage",
          "message": {"chat": {"id": -100123}, "message_id": 1}}
    TG._handle_callback(cb)
    assert answered == [""]


# ── 🔒 real-money commands are the owner's alone ─────────────────────────────
# Until 2026-08-02 /positions and /winrate were public read-only commands and
# were advertised in the ☰ Menu. They print the owner's open positions, entry
# prices and P&L in USDT. With the group promoted publicly, anyone who joined
# could read the owner's book. These tests pin that shut.
ACCOUNT_CMDS = ("positions", "pos", "winrate", "stats", "wr", "report")


def test_account_commands_are_owner_only():
    for cmd in ACCOUNT_CMDS:
        assert cmd in TG.OWNER_ONLY_COMMANDS, f"/{cmd} must be owner-only"
        assert not TG.authorized(cmd, "999999"), f"/{cmd} answered a stranger"


def test_account_commands_refuse_even_group_admins(monkeypatch):
    """A group admin is not the account owner."""
    monkeypatch.setattr(TG, "_group_admin_ids", lambda: {"424242"})
    assert TG.is_admin("424242") is True
    for cmd in ACCOUNT_CMDS:
        assert not TG.authorized(cmd, "424242"), f"/{cmd} leaked to a group admin"


def test_account_commands_answer_the_owner(monkeypatch):
    monkeypatch.setattr(TG, "OWNER_IDS", {"777"})
    for cmd in ACCOUNT_CMDS:
        assert TG.authorized(cmd, "777"), f"/{cmd} refused the owner"


def test_account_replies_go_to_the_owners_dm():
    """Answering in the group would defeat the gate — the data would be on
    screen for everyone regardless of who typed it."""
    for cmd in ACCOUNT_CMDS:
        assert cmd in TG.PRIVATE_REPLY_COMMANDS


def test_refresh_button_recheeks_the_sender(monkeypatch):
    """The 🔄 button lives in the message forever and ANY member can tap it,
    so the callback path must re-authorise — gating only the typed command
    left a tappable back door to the same data."""
    monkeypatch.setattr(TG, "OWNER_IDS", {"777"})
    monkeypatch.setattr(TG, "_group_admin_ids", lambda: set())
    monkeypatch.setattr(TG, "allowed", lambda c: True)
    answers, edits = [], []
    monkeypatch.setattr(TG, "_answer_callback", lambda cb_id, text=None: answers.append(text))
    monkeypatch.setattr(TG, "_edit_message", lambda *a, **k: edits.append(a))
    monkeypatch.setattr(TG, "handle", lambda *a, **k: "SECRET BALANCE")

    def _tap(uid):
        answers.clear(); edits.clear()
        TG._handle_callback({"id": "1", "data": "r:positions:", "from": {"id": uid},
                             "message": {"message_id": 5, "chat": {"id": -100}}})

    _tap("999999")                      # a member taps someone else's refresh
    assert not edits, "stranger refreshed an account message"
    assert answers and "⛔" in (answers[0] or "")

    _tap("777")                         # the owner taps their own
    assert edits, "owner was blocked from refreshing"


def test_public_menu_does_not_advertise_account_commands():
    import re
    src = open("set_bot_commands.py", encoding="utf-8").read()
    listed = set(re.findall(r'\(\s*"([a-z]+)"\s*,\s*"', src))
    for cmd in ("winrate", "positions", "report"):
        assert cmd not in listed, f"/{cmd} is still in the public ☰ Menu"


# ── 🚪 the bot is not usable in anyone else's group ──────────────────────────
def test_leaves_a_foreign_group(monkeypatch):
    calls = []
    monkeypatch.setattr(TG, "_api", lambda m, **kw: calls.append((m, kw)) or {})
    TG._left_chats.clear()
    TG._leave_foreign_chat(-100999)
    assert calls and calls[0][0] == "leaveChat"


def test_leave_is_tried_once_per_chat(monkeypatch):
    """A failure (already gone, no rights) must not retry on every poll."""
    calls = []
    monkeypatch.setattr(TG, "_api", lambda m, **kw: calls.append(m) or {})
    TG._left_chats.clear()
    for _ in range(4):
        TG._leave_foreign_chat(-100999)
    assert len(calls) == 1
