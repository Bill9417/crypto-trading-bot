"""⭐ Premium signal tier + outcome-tracker snapshot + owner-only /report.

Covers the 2026-07-16 batch:
  • classify_signal / digest_worthy — the measured win-rate gate
  • premium_trade_levels — SL 2×ATR, TP1 0.75R (the published ⭐ plan)
  • premium_alert_text — 中文 Bybit-first alert, offline fallback
  • signal_outcomes snapshot — signals survive the page feed's 24h purge
    long enough to be evaluated at 48h (the bug that hid the win rate)
  • tg_commands owner-only /report
"""
import json
import time

import config
import signal_outcomes as SO
import strategy2_live as S2L
import strategy2_scanner as S2S
import tg_commands as TGC


# ── classify_signal ──────────────────────────────────────────────────────────
def test_premium_needs_conviction_alignment_and_adx():
    t = S2S.classify_signal("long", 88, 25.0, "bull")
    assert t["premium"] and t["aligned"] and not t["against"]


def test_premium_short_mirror():
    t = S2S.classify_signal("short", 12, 25.0, "bear")   # conviction 88
    assert t["premium"] and t["aligned"]


def test_no_premium_when_fighting_btc():
    t = S2S.classify_signal("long", 95, 30.0, "bear")
    assert t["against"] and not t["premium"]


def test_no_premium_in_neutral_regime_when_alignment_required():
    t = S2S.classify_signal("long", 95, 30.0, "neutral")
    assert not t["aligned"] and not t["premium"]


def test_no_premium_below_adx_floor():
    t = S2S.classify_signal("long", 95, config.STRATEGY2_PREMIUM_MIN_ADX - 1, "bull")
    assert not t["premium"]


def test_no_premium_below_conviction_floor():
    t = S2S.classify_signal("long", config.STRATEGY2_PREMIUM_MIN_SCORE - 1,
                            30.0, "bull")
    assert not t["premium"]


def test_missing_adx_never_premium():
    assert not S2S.classify_signal("long", 95, None, "bull")["premium"]


def test_conviction_handles_literal_zero_score():
    # a score of exactly 0 is a maximally-bearish short → conviction 100,
    # NOT 0 (the `score or 100` trap). Long 0 → conviction 0.
    assert S2S._conviction("short", 0) == 100
    assert S2S._conviction("long", 0) == 0
    assert S2S._conviction("short", None) == 100
    assert S2S._conviction("long", 88) == 88
    assert S2S._conviction("short", 12) == 88


# ── digest bar ───────────────────────────────────────────────────────────────
def test_digest_drops_counter_btc_and_low_conviction():
    assert not S2S.digest_worthy({"direction": "long", "score": 90, "against": True})
    assert not S2S.digest_worthy({"direction": "long",
                                  "score": config.STRATEGY2_DIGEST_MIN_CONV - 1})
    assert S2S.digest_worthy({"direction": "long",
                              "score": config.STRATEGY2_DIGEST_MIN_CONV})
    # short mirror: score 25 ⇒ conviction 75
    assert S2S.digest_worthy({"direction": "short",
                              "score": 100 - config.STRATEGY2_DIGEST_MIN_CONV})


# ── premium plan geometry ────────────────────────────────────────────────────
def test_premium_trade_levels_geometry():
    entry, sl, tp1, tp2 = S2L.premium_trade_levels(100.0, True, atr=1.0)
    assert entry == 100.0
    assert sl == 100.0 - config.STRATEGY2_PREMIUM_SL_MULT * 1.0
    risk = entry - sl
    assert abs(tp1 - (entry + risk * config.STRATEGY2_PREMIUM_TP1_R)) < 1e-9
    assert abs(tp2 - (entry + risk * config.STRATEGY2_PREMIUM_TP2_R)) < 1e-9


def test_premium_trade_levels_short_and_cap():
    entry, sl, tp1, tp2 = S2L.premium_trade_levels(100.0, False, atr=50.0)
    assert sl == 100.0 * (1 + config.MAX_SL_PCT)      # capped, not 100+100
    assert tp1 < entry < sl


def test_plain_trade_levels_unchanged_for_live_execution():
    entry, sl, tp1, tp2 = S2L.trade_levels(100.0, True, atr=1.0)
    assert sl == 100.0 - config.ATR_SL_MULTIPLIER * 1.0
    risk = entry - sl
    assert abs(tp1 - (entry + risk * config.ATR_TP1_MULTIPLIER)) < 1e-9


# ── premium alert text (offline: bybit guard in conftest) ────────────────────
def test_premium_alert_text_chinese_with_fallback_link():
    sig = {"base": "ETH", "direction": "long", "score": 88, "price": 1800.0,
           "entry": 1800.0, "sl": 1764.0, "tp1": 1827.0, "tp2": 1872.0,
           "aligned": True, "premium": True, "adx": 27.0,
           "tv_url": "https://tv.example/ETH"}
    msg = S2S.premium_alert_text(sig)
    assert "精選訊號" in msg and "做多" in msg
    assert "信心 88" in msg and "BTC同向" in msg and "ADX 27" in msg
    assert "進場" in msg and "0.75R" in msg
    assert "參考價" in msg                             # Bybit offline → honest reference price
    assert "看圖" in msg or "下單" in msg                # always a tappable link
    assert "非投資建議" in msg


# ── signal_outcomes snapshot ─────────────────────────────────────────────────
class _Client:
    def __init__(self, candles):
        self.candles = candles

    def call(self, method, *a, **k):
        assert method == "fetch_ohlcv"
        return self.candles


def _wire_files(monkeypatch, tmp_path, signals):
    sig_file = tmp_path / "signals.json"
    state_file = tmp_path / "outcomes.json"
    sig_file.write_text(json.dumps({"signals": signals}), encoding="utf-8")
    monkeypatch.setattr(SO, "SIGNALS_FILE", str(sig_file))
    monkeypatch.setattr(SO, "STATE_FILE", str(state_file))
    return sig_file, state_file


def test_snapshot_survives_page_purge_and_evaluates(monkeypatch, tmp_path):
    now = time.time()
    fresh = {"symbol": "ETH/USDT:USDT", "base": "ETH", "direction": "long",
             "score": 88, "ts": now - 3600, "entry": 100.0, "sl": 98.0,
             "tp1": 101.5, "tp2": 104.0, "premium": True}
    sig_file, state_file = _wire_files(monkeypatch, tmp_path, [fresh])

    SO.tick(_Client([]))                    # snapshot; nothing due yet
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(state["signals"]) == 1       # persisted BEFORE any evaluation

    # page purge (the 24h retention) — snapshot must not care
    sig_file.write_text(json.dumps({"signals": []}), encoding="utf-8")
    # age the snapshot past the 48h window
    key = next(iter(state["signals"]))
    state["signals"][key]["ts"] = now - 49 * 3600
    state_file.write_text(json.dumps(state), encoding="utf-8")

    ts_ms = int((now - 49 * 3600) * 1000)
    candles = [[ts_ms + 900_000, 100, 104.5, 99.5, 104, 1.0]]   # hits tp2, not sl
    assert SO.tick(_Client(candles)) == 1
    state = json.loads(state_file.read_text(encoding="utf-8"))
    ev = list(state["evaluated"].values())[0]
    assert ev["outcome"] == "tp2"
    assert ev["premium"] is True


def test_snapshot_prunes_evaluated_and_ancient(monkeypatch, tmp_path):
    now = time.time()
    state = {"evaluated": {"X:1": {"outcome": "sl", "sig_ts": now}},
             "signals": {"X:1": {"symbol": "X", "sl": 1, "ts": now},
                         "Y:2": {"symbol": "Y", "sl": 1,
                                 "ts": now - (SO.MAX_AGE_D + 1) * 86400}}}
    SO._snapshot(state, [], now)
    assert state["signals"] == {}           # evaluated + too-old both pruned


def test_scorecard_shows_premium_line():
    outs = [{"outcome": "tp1", "premium": True},
            {"outcome": "sl", "premium": True},
            {"outcome": "tp2"}]
    text = SO.summarize(outs)
    assert "精選訊號 2 個" in text
    assert "先到TP1 1 個 (50%)" in text


# ── owner-only /report ───────────────────────────────────────────────────────
def test_report_is_owner_only(monkeypatch):
    monkeypatch.setattr(TGC, "OWNER_IDS", {"777"})
    assert TGC.authorized("report", 777)
    assert not TGC.authorized("report", 12345)
    # …even for a group admin
    monkeypatch.setattr(TGC, "_group_admin_ids", lambda: {"12345"})
    assert not TGC.authorized("report", 12345)
    # ordinary read-only commands stay open
    assert TGC.authorized("signals", 12345)
