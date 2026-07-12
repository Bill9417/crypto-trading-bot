"""S2 Telegram trade-plan blocks — pure formatting tests."""
import strategy2_scanner as S2S


def _sig(direction="long"):
    if direction == "long":
        return {"direction": "long", "entry": 1800.0, "sl": 1782.0,
                "tp1": 1818.0, "tp2": 1836.0}
    return {"direction": "short", "entry": 1800.0, "sl": 1818.0,
            "tp1": 1782.0, "tp2": 1764.0}


def test_plan_block_long():
    block = S2S._plan_block(_sig("long"))
    assert "Entry  1800" in block
    assert "SL     1782  (-1.00%)" in block
    assert "TP1    1818  (+1.00%, 1R)" in block
    assert "TP2    1836  (+2.00%, 2R)" in block


def test_plan_block_short_signs_flip():
    block = S2S._plan_block(_sig("short"))
    assert "SL     1818  (+1.00%)" in block
    assert "TP2    1764  (-2.00%, 2R)" in block


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
    assert f"…+{50 - S2S.DIGEST_MAX_ROWS} more" in msg
    assert f"…+{30 - S2S.DIGEST_MAX_ROWS} more" in msg
    assert "80 new" in msg                       # true total still reported
    # highest-conviction rows survive the cap
    assert "L0" in msg and "S0" in msg


def test_plan_suffix():
    assert S2S._plan_suffix(_sig()) == "  ·  SL 1782 · TP 1836"
    assert S2S._plan_suffix({"direction": "long"}) == ""
