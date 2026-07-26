"""Strategy attribution: which engine owned which Bybit position.

S1, S3 and the operator's manual trades share one Bybit sub-account, so an
account-wide P&L total measures none of them. These tests pin the recording
and the attribution, including the cases where attribution must admit it is
guessing.
"""
import config
import strategy_ledger as L

T = 1_784_900_000.0


def _tr(symbol, pnl, ts, notional=0.0, lev=0.0):
    return {"symbol": symbol, "pnl": pnl, "time": int(ts * 1000),
            "notional": notional, "lev": lev}


# ── symbol normalisation ─────────────────────────────────────────────────────
def test_norm_accepts_every_symbol_spelling():
    for s in ("PENGU/USDT:USDT", "PENGU/USDT", "PENGUUSDT", "pengu/usdt:usdt"):
        assert L.norm(s) == "PENGUUSDT"
    assert L.norm(None) == "" and L.norm("") == ""


# ── recording ────────────────────────────────────────────────────────────────
def test_open_then_close_is_recorded_as_one_interval():
    L.record_open("S1", "PENGU/USDT:USDT", "short", ts=T)
    L.record_close("S1", "PENGUUSDT", ts=T + 3600)
    rows = L._load()["rows"]
    assert len(rows) == 1
    assert rows[0]["strategy"] == "S1" and rows[0]["symbol"] == "PENGUUSDT"
    assert rows[0]["opened"] == T and rows[0]["closed"] == T + 3600


def test_double_open_does_not_duplicate_the_interval():
    L.record_open("S3", "XAUT/USDT:USDT", "long", ts=T)
    L.record_open("S3", "XAUT/USDT:USDT", "long", ts=T + 60)
    assert len(L._load()["rows"]) == 1


def test_reopening_after_a_close_starts_a_new_interval():
    L.record_open("S1", "ETHUSDT", "long", ts=T)
    L.record_close("S1", "ETHUSDT", ts=T + 100)
    L.record_open("S1", "ETHUSDT", "short", ts=T + 200)
    rows = L._load()["rows"]
    assert len(rows) == 2 and rows[1]["closed"] is None


def test_recording_never_raises_on_a_broken_ledger(monkeypatch, capsys):
    """Bookkeeping must not be able to break a live order."""
    def boom(_state):
        raise OSError("disk full")
    monkeypatch.setattr(L, "_save", boom)
    L.record_open("S1", "ETHUSDT", "long", ts=T)      # must not raise
    L.record_close("S1", "ETHUSDT", ts=T)
    assert "record_open failed" in capsys.readouterr().out


# ── attribution ──────────────────────────────────────────────────────────────
def test_owner_of_matches_inside_the_interval():
    L.record_open("S3", "XAUTUSDT", "long", ts=T)
    L.record_close("S3", "XAUTUSDT", ts=T + 7200)
    assert L.owner_of("XAUTUSDT", T + 3600) == "S3"
    assert L.owner_of("XAUTUSDT", T - 4 * L.GRACE_S) is None   # before it opened
    assert L.owner_of("ETHUSDT", T + 3600) is None             # different symbol


def test_owner_of_tolerates_exchange_clock_skew():
    """Bybit's close time and ours differ by seconds — a trade must not fall
    out of its own interval over that."""
    L.record_open("S1", "ETHUSDT", "long", ts=T)
    L.record_close("S1", "ETHUSDT", ts=T + 100)
    assert L.owner_of("ETHUSDT", T + 100 + L.GRACE_S / 2) == "S1"
    assert L.owner_of("ETHUSDT", T + 100 + L.GRACE_S * 3) is None


def test_still_open_position_is_attributable():
    L.record_open("S1", "PENGUUSDT", "short", ts=T)
    assert L.owner_of("PENGUUSDT", T + 86400) == "S1"


def test_attribute_prefers_the_record_over_the_guess():
    """PENGU mid-scale-out looks nothing like a 100-USDT mirror order — the
    size heuristic files it as manual, the ledger knows better."""
    L.record_open("S1", "PENGUUSDT", "short", ts=T)
    out = L.attribute([_tr("PENGUUSDT", 0.34, T + 60, notional=32.4, lev=10)])
    assert out[0]["strategy"] == "S1" and out[0]["attrib"] == "recorded"


def test_attribute_falls_back_to_inference_and_says_so():
    out = L.attribute([_tr("XAUTUSDT", 1.0, T, notional=1200, lev=50),
                       _tr("AERGOUSDT", 0.7, T, notional=100.3, lev=10),
                       _tr("SKHYNIXUSDT", 8.2, T, notional=260.7, lev=25)])
    assert [o["strategy"] for o in out] == ["S3", "S1", L.MANUAL]
    assert all(o["attrib"] == "inferred" for o in out)


def test_inference_uses_the_configured_s3_symbols(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["XAUT"])
    assert L.infer("XAUTUSDT", 1200, 50) == "S3"
    assert L.infer("BTCUSDT", 1200, 50) == L.MANUAL     # not an S3 symbol today


def test_manual_only_symbols_win_even_inside_s1s_own_window():
    """THE REGRESSION: SPCX is a Bybit stock perp the owner scalps by hand.
    On 2026-07-26 one scalp happened to close at 97.6 USDT notional / 10x —
    squarely inside S1's ~100 USDT/≤10x fingerprint — and got filed as an S1
    loss it never was. config.MANUAL_ONLY_SYMBOLS must win outright, size
    match or not."""
    assert "SPCX" in config.MANUAL_ONLY_SYMBOLS       # ships in the default set
    assert L.infer("SPCXUSDT", 97.6, 10) == L.MANUAL
    assert L.infer("SPCX/USDT:USDT", 100.0, 10) == L.MANUAL   # any symbol spelling


def test_manual_only_symbols_beats_the_s3_check_too(monkeypatch):
    """Defence in depth: even if a manual ticker ever collided with
    STRATEGY3_SYMBOLS, the manual override must still win."""
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["SPCX"])
    assert L.infer("SPCXUSDT", 1200, 50) == L.MANUAL


def test_manual_only_symbols_is_configurable(monkeypatch):
    """The user can add a future manually-traded ticker without a code edit."""
    monkeypatch.setattr(config, "MANUAL_ONLY_SYMBOLS", {"NVDA"})
    assert L.infer("NVDAUSDT", 100.0, 10) == L.MANUAL
    # and SPCX is no longer special-cased once the set is overridden
    assert L.infer("SPCXUSDT", 97.6, 10) == "S1"


# ── summary + report ─────────────────────────────────────────────────────────
def test_split_summary_keeps_the_strategies_apart():
    L.record_open("S1", "AUSDT", "long", ts=T)
    L.record_close("S1", "AUSDT", ts=T + 10)
    L.record_open("S3", "XAUTUSDT", "long", ts=T)
    L.record_close("S3", "XAUTUSDT", ts=T + 10)
    st = L.split_summary([_tr("AUSDT", -2.0, T + 5),
                          _tr("XAUTUSDT", 3.0, T + 5),
                          _tr("SKHYNIXUSDT", 9.0, T + 5, notional=260, lev=25)])
    assert st["S1"]["net"] == -2.0 and st["S1"]["win_rate"] == 0.0
    assert st["S3"]["net"] == 3.0 and st["S3"]["profit_factor"] is None
    assert st[L.MANUAL]["net"] == 9.0
    assert st["S1"]["recorded"] == 1 and st[L.MANUAL]["inferred"] == 1


def test_report_warns_that_old_trades_are_only_guessed():
    txt = L.report([_tr("SKHYNIXUSDT", 9.0, T, notional=260, lev=25)])
    assert "推測" in txt
    assert "手動交易" in txt


def test_report_flags_thin_strategy_samples():
    L.record_open("S3", "XAUTUSDT", "long", ts=T)
    L.record_close("S3", "XAUTUSDT", ts=T + 10)
    txt = L.report([_tr("XAUTUSDT", 3.0, T + 5)])
    assert "樣本太少" in txt          # 1 trade must never read as a track record


def test_report_survives_an_empty_account():
    assert "尚無已平倉" in L.report([])
