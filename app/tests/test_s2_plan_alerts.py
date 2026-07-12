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


def test_plan_suffix():
    assert S2S._plan_suffix(_sig()) == "  ·  SL 1782 · TP 1836"
    assert S2S._plan_suffix({"direction": "long"}) == ""
