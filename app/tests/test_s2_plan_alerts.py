"""S2 Telegram trade-plan blocks — pure formatting tests (中文+EN format,
Bybit fallback exercised offline via the conftest bybit_data guard)."""
import strategy2_scanner as S2S


def _sig(direction="long"):
    if direction == "long":
        return {"direction": "long", "entry": 1800.0, "sl": 1782.0,
                "tp1": 1818.0, "tp2": 1836.0}
    return {"direction": "short", "entry": 1800.0, "sl": 1818.0,
            "tp1": 1782.0, "tp2": 1764.0}


def test_plan_block_long():
    block = S2S._plan_block(_sig("long"))
    assert "進場 Entry   1800" in block
    assert "停損 SL      1782  (-1.00%)" in block
    assert "目標1 TP1    1818  (+1.00% · 1R)" in block
    assert "目標2 TP2    1836  (+2.00% · 2R)" in block
    assert "先平一半" in block                       # managed-exit guidance


def test_plan_block_premium_geometry_fractional_r():
    # premium plan: TP1 at 0.75R must not round up to "1R"
    sig = {"direction": "long", "entry": 100.0, "sl": 98.0,
           "tp1": 101.5, "tp2": 104.0}
    block = S2S._plan_block(sig)
    assert "0.75R" in block
    assert "2R" in block


def test_plan_block_short_signs_flip():
    block = S2S._plan_block(_sig("short"))
    assert "停損 SL      1818  (+1.00%)" in block
    assert "目標2 TP2    1764  (-2.00% · 2R)" in block


def test_plan_block_missing_levels_is_empty():
    assert S2S._plan_block({"direction": "long", "entry": 1800.0}) == ""
    assert S2S._plan_block({"direction": "long", "entry": 1800.0, "sl": 1800.0,
                            "tp1": 1.0, "tp2": 1.0}) == ""   # zero-risk guard


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
