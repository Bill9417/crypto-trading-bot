"""S2 Telegram trade-plan blocks — the plan block now lives in the shared house
style (tg_format.mono_plan); the digest still uses S2S helpers. Bybit fallback
is exercised offline via the conftest bybit_data guard."""
import strategy2_scanner as S2S
import tg_format


def _sig(direction="long"):
    if direction == "long":
        return {"direction": "long", "entry": 1800.0, "sl": 1782.0,
                "tp1": 1818.0, "tp2": 1836.0}
    return {"direction": "short", "entry": 1800.0, "sl": 1818.0,
            "tp1": 1782.0, "tp2": 1764.0}


def test_mono_plan_long_aligned():
    block = tg_format.mono_plan(1800.0, 1782.0, 1818.0, 1836.0, is_long=True)
    assert block.startswith("<pre>") and block.endswith("</pre>")
    assert "進場 1,800" in block
    assert "停損 1,782" in block and "−1.0%" in block   # real minus glyph
    assert "目標 1,818" in block and "+1.0% · 1R" in block
    assert "終標 1,836" in block and "+2.0% · 2R" in block


def test_mono_plan_fractional_r_not_rounded():
    block = tg_format.mono_plan(100.0, 98.0, 101.5, 104.0, is_long=True)
    assert "0.75R" in block and "2R" in block           # 0.75R must not round to 1R


def test_mono_plan_short_signs_flip():
    block = tg_format.mono_plan(1800.0, 1818.0, 1782.0, 1764.0, is_long=False)
    assert "停損 1,818" in block and "−1.0%" in block    # SL above entry = unfavourable
    assert "終標 1,764" in block and "+2.0% · 2R" in block


def test_mono_plan_missing_or_zero_risk_is_empty():
    assert tg_format.mono_plan(1800.0, None, 1818.0, 1836.0, is_long=True) == ""
    assert tg_format.mono_plan(1800.0, 1800.0, 1.0, 1.0, is_long=True) == ""  # zero-risk


def test_digest_caps_rows_and_stays_under_telegram_limit():
    # an 80-signal chop day once produced a >4096-char digest → 400, lost
    sigs = ([{"direction": "long", "base": f"L{i}", "score": 90 - i,
              "price": 1.2345, "sl": 1.2, "tp2": 1.3} for i in range(50)]
            + [{"direction": "short", "base": f"S{i}", "score": 10 + i,
                "price": 2.5, "sl": 2.6, "tp2": 2.3} for i in range(30)])
    msg = S2S._digest_text(sigs)
    assert len(msg) < 4096
    assert msg.count("  • ") == 2 * S2S.DIGEST_MAX_ROWS
    assert f"…還有 {50 - S2S.DIGEST_MAX_ROWS} 個" in msg
    assert f"…還有 {30 - S2S.DIGEST_MAX_ROWS} 個" in msg
    assert "80 個新訊號" in msg                   # true total still reported
    # highest-conviction rows survive the cap
    assert "L0" in msg and "S0" in msg


def test_digest_marks_premium_rows():
    sigs = [{"direction": "long", "base": "AAA", "score": 88, "price": 1.0,
             "sl": 0.98, "tp2": 1.04, "premium": True},
            {"direction": "long", "base": "BBB", "score": 75, "price": 1.0,
             "sl": 0.98, "tp2": 1.04}]
    msg = S2S._digest_text(sigs)
    assert "⭐ AAA" in msg
    assert "• BBB" in msg
    assert "非投資建議" in msg


def test_digest_offline_falls_back_to_binance_price():
    # conftest kills the bybit client → row must show the Binance price
    msg = S2S._digest_text([{"direction": "long", "base": "ZZZ", "score": 80,
                             "price": 1.2345, "sl": 1.2, "tp2": 1.3}])
    assert "1.2345" in msg


def test_plan_suffix():
    assert S2S._plan_suffix(_sig()) == " · 停損 1782 · 目標 1836"
    assert S2S._plan_suffix({"direction": "long"}) == ""
