"""Strategy 3 OCC engine — resampling, SMMA and cross-signal unit tests.

Pure-math tests against strategy3_occ.py plus the scanner's engine-switch
state hygiene. Like test_strategy3.py, NOTHING here may assert the user's
live .env choices (symbols on/off, sizes) — preflight runs against the real
.env and such assertions abort live launches.
"""
import strategy3_occ as OCC
import strategy3_scanner as S3

TF = 1800            # 30m in seconds
TF_MS = TF * 1000
BUCKET_MS = TF_MS * 3


def _candles(closes, start_ms=0, opens=None):
    """30m OHLCV, contiguous from start_ms; open defaults to previous close."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = opens[i] if opens else prev
        out.append([start_ms + i * TF_MS, float(o), float(max(o, c)),
                    float(min(o, c)), float(c), 100.0])
        prev = c
    return out


# ── resample ─────────────────────────────────────────────────────────────────

def test_resample_groups_three_30m_into_90m_utc_aligned():
    rows = _candles(list(range(1, 10)))                 # 9 candles → 3 buckets
    b = OCC.resample(rows, TF, 3)
    assert len(b) == 3
    assert [x[0] for x in b] == [0, BUCKET_MS, 2 * BUCKET_MS]
    assert b[0][4] == 3.0 and b[1][4] == 6.0 and b[2][4] == 9.0   # closes
    assert b[1][1] == 3.0                               # bucket open = 1st candle's open
    assert b[0][5] == 300.0                             # volume summed


def test_resample_drops_trailing_partial_bucket():
    rows = _candles(list(range(1, 9)))                  # 8 candles → last bucket has 2
    b = OCC.resample(rows, TF, 3)
    assert len(b) == 2
    assert b[-1][0] == BUCKET_MS


def test_resample_drops_leading_partial_and_gapped_buckets():
    rows = _candles(list(range(1, 10)))
    # start mid-bucket: first bucket incomplete
    assert len(OCC.resample(rows[1:], TF, 3)) == 2
    # a missing candle INSIDE a bucket poisons only that bucket
    gapped = rows[:4] + rows[5:]                        # drop candle #5 (2nd bucket)
    b = OCC.resample(gapped, TF, 3)
    assert [x[0] for x in b] == [0, 2 * BUCKET_MS]


def test_resample_high_low_span_the_bucket():
    rows = _candles([5, 50, 2, 7, 7, 7])
    b = OCC.resample(rows, TF, 3)
    assert b[0][2] == 50.0 and b[0][3] == 2.0


# ── smma ─────────────────────────────────────────────────────────────────────

def test_smma_seeds_with_sma_then_wilder_recursion():
    vals = [1, 2, 3, 4, 10]
    out = OCC.smma(vals, 4)
    assert out[:3] == [None, None, None]
    assert out[3] == 2.5                                # SMA seed of first 4
    assert out[4] == (2.5 * 3 + 10) / 4                 # Wilder step


def test_smma_shorter_than_length_is_all_none():
    assert OCC.smma([1, 2], 5) == [None, None]


# ── snapshot / cross detection ───────────────────────────────────────────────

def _trending(n_buckets, flip_at=None, up_step=1.0):
    """Per-bucket closes that trend down then (optionally) sharply up so the
    close-SMMA crosses the open-SMMA on a known bucket. Bucket i occupies
    candles 3i..3i+2; open of bucket = close of previous (gapless walk)."""
    closes = []
    lvl = 1000.0
    for i in range(n_buckets):
        step = -up_step if flip_at is None or i < flip_at else 3 * up_step
        for _ in range(3):
            lvl += step
            closes.append(lvl)
    return _candles(closes)


def test_snapshot_insufficient_without_warmup():
    snap = OCC.snapshot(_trending(10), TF, 3, 8)        # < 8+10 buckets
    assert snap["insufficient"] is True


def test_snapshot_long_cross_fires_once_on_the_cross_bucket():
    # 30 buckets: down-trend, then a hard up-turn near the end
    rows = _trending(30, flip_at=26)
    # walk forward bucket by bucket: find where the signal fires
    fired = []
    for k in range(20 * 3, 30 * 3 + 1, 3):
        snap = OCC.snapshot(rows[:k], TF, 3, 8)
        if not snap["insufficient"] and snap["signal"]:
            fired.append((snap["bucket_ts"], snap["signal"]))
    assert fired, "up-turn never produced a long cross"
    assert all(s == "long" for _, s in fired)
    assert len(fired) == 1                              # a cross is a one-bucket event
    snap = OCC.snapshot(rows, TF, 3, 8)
    assert snap["trend"] == "up"                        # after the cross, trend reads up


def test_snapshot_short_cross_symmetric():
    rows = _trending(30, flip_at=26, up_step=-1.0)      # up-trend then hard down-turn
    snap_all = OCC.snapshot(rows, TF, 3, 8)
    assert snap_all["trend"] == "down"
    fired = []
    for k in range(20 * 3, 30 * 3 + 1, 3):
        snap = OCC.snapshot(rows[:k], TF, 3, 8)
        if not snap["insufficient"] and snap["signal"]:
            fired.append(snap["signal"])
    assert fired == ["short"]


def test_snapshot_steady_trend_has_no_signal():
    snap = OCC.snapshot(_trending(30), TF, 3, 8)
    assert snap["signal"] is None
    assert snap["trend"] == "down"
    assert snap["bucket_ts"] == 29 * BUCKET_MS


# ── scanner integration: decide() reuse + engine switch hygiene ──────────────

def _occ_decide(st, sig, holding):
    """The scanner's exact adapter: the cross IS the signal, no Vegas gate."""
    want = sig or st.get("last_flag")
    gate = 1 if want == "long" else -1 if want == "short" else 0
    return S3.decide(st, sig, gate, holding)


def test_occ_cross_flips_and_consumes_once():
    st = {"last_flag": None, "consumed": False}
    close, open_dir = _occ_decide(st, "long", None)
    assert (close, open_dir) == (False, "long")
    st["pos_dir"] = "long"; st["consumed"] = True
    # same bucket direction again (no new cross) → no-op
    assert _occ_decide(st, None, "long") == (False, None)
    # opposite cross → close AND reverse in one bucket
    close, open_dir = _occ_decide(st, "short", "long")
    assert (close, open_dir) == (True, "short")


def test_occ_no_reentry_after_stop_out_until_next_cross():
    st = {"last_flag": "long", "consumed": True}        # entered, then SL hit
    assert _occ_decide(st, None, None) == (False, None) # stays flat
    close, open_dir = _occ_decide(st, "short", None)    # next cross re-arms
    assert (close, open_dir) == (False, "short")


def test_ensure_engine_wipes_signal_state_on_switch_but_keeps_position():
    st = {"engine": "flagflip", "last_flag": "short", "consumed": False,
          "last_candle": 123, "pos_dir": "short", "be_armed": True}
    S3.ensure_engine(st, "occ")
    assert st["engine"] == "occ"
    assert st["last_flag"] is None and st["consumed"] is False
    assert st["last_candle"] == 0 and st["last_bucket"] == 0
    assert st["pos_dir"] == "short"                     # never orphan a position
    # legacy state with no stamp switching onto OCC is also wiped
    st2 = {"last_flag": "long", "consumed": False, "last_candle": 9}
    S3.ensure_engine(st2, "occ")
    assert st2["last_flag"] is None and st2["last_candle"] == 0
    # …but a legacy flag-flip state just gets stamped, nothing wiped
    st3 = {"last_flag": "long", "consumed": True, "last_candle": 9}
    S3.ensure_engine(st3, "flagflip")
    assert st3["last_flag"] == "long" and st3["last_candle"] == 9
    # stamping the same engine twice is a no-op
    S3.ensure_engine(st, "occ")
    assert st["last_bucket"] == 0 and st["pos_dir"] == "short"


def test_open_flip_uses_the_per_symbol_sl_pct(monkeypatch):
    """The OCC pair trades a wider disaster stop than the flag-flip global —
    the SL price handed to the executor must come from sl_pct, and the
    Telegram line must carry the engine-specific 'why'."""
    seen = {}

    def fake_exec_open(symbol, direction, price, sl, margin, leverage):
        seen.update(symbol=symbol, sl=sl, margin=margin, leverage=leverage)
        return {"ok": True, "dry": True, "qty": 0.2}

    sent = []
    monkeypatch.setattr(S3.config, "STRATEGY3_LIVE", True)
    monkeypatch.setattr(S3.X, "is_live", lambda: False)
    monkeypatch.setattr(S3.X, "open_flip", fake_exec_open)
    monkeypatch.setattr(S3, "_tg", sent.append)
    out = S3.open_flip("ETH/USDT:USDT", "long", 2500.0, None, 50.0, 10,
                       sl_pct=0.04, why="8-SMMA close crossed over open on 90m")
    assert out == "opened"
    assert seen["sl"] == 2500.0 * 0.96                  # 4%, not the 1.5% global
    assert seen["margin"] == 50.0 and seen["leverage"] == 10
    assert any("SMMA" in m and "4%" in m for m in sent)
