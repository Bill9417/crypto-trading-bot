"""Shared Telegram house style (tg_format) — the one place S1/S2/whale get their
tidy layout. Bybit is offline here (conftest stubs bybit_data), so bybit_line
exercises the honest reference-price fallback."""
import tg_format


def test_dir_zh():
    assert tg_format.dir_zh("long") == "🟢 做多"
    assert tg_format.dir_zh("short") == "🔴 做空"
    assert tg_format.dir_zh("LONG", arrow=False) == "做多"


def test_fmt_price_trims_trailing_zeros():
    assert tg_format.fmt_price(3500.0) == "3,500"
    assert tg_format.fmt_price(3552.5) == "3,552.5"
    assert tg_format.fmt_price(1700.0) == "1,700"
    assert tg_format.fmt_price(0.0012345) == "0.0012345"  # sub-cent precision kept


def test_signed_pct_uses_minus_glyph_and_flips_for_short():
    assert tg_format.signed_pct(100, 102, True) == "+2.0%"
    assert tg_format.signed_pct(100, 98, True) == "−2.0%"      # U+2212, not '-'
    assert tg_format.signed_pct(100, 102, False) == "−2.0%"    # short: up = against
    assert "-" not in tg_format.signed_pct(100, 98, True)      # no ascii hyphen


def test_mono_plan_is_pre_wrapped_with_all_labels():
    block = tg_format.mono_plan(100.0, 98.0, 103.0, 106.0, is_long=True)
    assert block.startswith("<pre>") and block.endswith("</pre>")
    for lbl in ("進場", "停損", "目標", "終標"):
        assert lbl in block
    assert "1.5R" in block and "3R" in block    # (103-100)/2=1.5R · (106-100)/2=3R
    # entry row carries no % / R
    assert block.split("\n")[0].endswith("100")


def test_bybit_line_offline_is_reference_price_plus_tappable_link():
    line = tg_format.bybit_line("ETH", ref_price=1800.0)
    assert "參考價 1,800" in line
    assert "看圖" in line and "<a href=" in line


def test_disp_w_counts_cjk_double():
    assert tg_format.disp_w("ETH") == 3
    assert tg_format.disp_w("做多") == 4          # 2 CJK chars × 2 columns
    assert tg_format.disp_w("進場 100") == 8


def test_pre_table_aligns_mixed_cjk_and_latin():
    t = tg_format.pre_table([("做多", "1,800"), ("空", "98.5"), ("ETH", "3")])
    assert t.startswith("<pre>") and t.endswith("</pre>")
    lines = t[5:-6].split("\n")
    # numbers right-aligned: every row's last char column matches
    assert all(tg_format.disp_w(ln) == tg_format.disp_w(lines[0]) for ln in lines)


def test_pre_table_escapes_html():
    t = tg_format.pre_table([("A&B", "<x>")])
    assert "&amp;" in t and "&lt;x&gt;" in t
    assert "<x>" not in t


def test_pre_table_empty_and_uneven_rows():
    assert tg_format.pre_table([]) == ""
    t = tg_format.pre_table([("a", "b", "c"), ("only",)])   # short row is fine
    assert "only" in t


def test_balance_pre_reopens_split_blocks():
    import telegram_utils
    parts = telegram_utils.balance_pre(["x<pre>row1", "row2</pre>y"])
    assert parts[0].endswith("</pre>")            # part 1 closed
    assert parts[1].startswith("<pre>")           # part 2 reopened
    balanced = telegram_utils.balance_pre(["<pre>a</pre>", "plain"])
    assert balanced == ["<pre>a</pre>", "plain"]  # already-valid parts untouched
