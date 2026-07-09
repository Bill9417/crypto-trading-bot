"""Funnel page 'best trade' hero — mixes Strategy 1 (queue/entry system) and
Strategy 3 (Vegas Flag Flip) candidates, per the operator's request
(2026-07-04) for "a mix version of strategy 1 and 3" on the funnel page."""
import app as A
import config


def _sig(**kw):
    base = {
        "symbol": "SOL/USDT:USDT", "direction": "long", "tradeable": True,
        "trade_queued": False, "trade_status": "rejected",
        "effective_lights": 5, "conviction": "HIGH CONVICTION", "rsi": 55.0,
        "entry": "100.0", "sl": "95.0", "tp": "105.0", "tp2": "110.0",
        "record_status_reason": "rejected on latest scan: volume gate failed",
        "details": ["RSI > 50 (+1 bull)"], "smc": {}, "tv_url": "https://tv/x",
    }
    base.update(kw)
    return base


def test_best_s1_trade_picks_queued_over_watch():
    data = {"signals": [
        _sig(symbol="A/USDT:USDT", trade_queued=False, trade_status="rejected", effective_lights=7),
        _sig(symbol="B/USDT:USDT", trade_queued=True, trade_status="queued", effective_lights=4,
             record_status_reason=""),
    ]}
    out = A.build_best_s1_trade(data)
    assert out["best"]["symbol"] == "B/USDT:USDT"      # queued beats a higher-light watch item
    assert out["best"]["queued"] is True


def test_best_s1_trade_computes_rr():
    data = {"signals": [_sig(entry="100.0", sl="95.0", tp2="110.0")]}
    out = A.build_best_s1_trade(data)
    assert out["best"]["rr"] == 2.0                    # risk=5, reward=10


def test_best_s1_trade_none_when_nothing_qualifies():
    data = {"signals": [_sig(tradeable=False)]}
    out = A.build_best_s1_trade(data)
    assert out["best"] is None
    assert out["considered"] == 0


def test_best_s1_trade_watch_needs_four_lights():
    data = {"signals": [_sig(trade_queued=False, effective_lights=3)]}
    out = A.build_best_s1_trade(data)
    assert out["best"] is None                          # below the 4-light watch bar


def _state(**per_symbol):
    return per_symbol


def test_best_s3_trade_armed_ranks_first(monkeypatch):
    import strategy3_scanner as S3
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["BTC", "SOL"])
    # flag-flip ranking mechanics — pin the engine; the real .env may route
    # SOL to the OCC engine, which takes a different note/ranking branch
    monkeypatch.setattr(config, "STRATEGY3_OCC_SYMBOLS", [])
    monkeypatch.setattr(S3, "load_state", lambda: {
        "BTC/USDT:USDT": {"last_score": 50, "last_vegas": 0, "pos_dir": None,
                           "last_flag": None, "consumed": True},
        "SOL/USDT:USDT": {"last_score": 90, "last_vegas": 1, "pos_dir": None,
                           "last_flag": "long", "consumed": False},
    })
    out = A.build_best_s3_trade()
    assert out["best"]["symbol"] == "SOL"
    assert "armed" in out["best"]["note"].lower()


def test_best_s3_trade_holding_ranks_last(monkeypatch):
    import strategy3_scanner as S3
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["BTC", "SOL"])
    monkeypatch.setattr(S3, "load_state", lambda: {
        "BTC/USDT:USDT": {"last_score": 95, "last_vegas": 1, "pos_dir": "long",
                           "last_flag": "long", "consumed": True},
        "SOL/USDT:USDT": {"last_score": 60, "last_vegas": 0, "pos_dir": None,
                           "last_flag": None, "consumed": True},
    })
    out = A.build_best_s3_trade()
    assert out["best"]["symbol"] == "SOL"               # BTC is already held — not a new entry
    holding_row = next(r for r in out["all"] if r["symbol"] == "BTC")
    assert "already holding" in holding_row["note"].lower()


def test_best_s3_trade_never_claims_imminent_fire_from_score_alone(monkeypatch):
    """Regression: score crossing the threshold does NOT mean a flag will
    fire (a flag needs the MSB structure to flip) — an earlier draft of this
    feature wrongly said 'should fire on the next closed candle' whenever
    score+Vegas agreed, which overstates what the data actually supports."""
    import strategy3_scanner as S3
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["BTC"])
    monkeypatch.setattr(S3, "load_state", lambda: {
        "BTC/USDT:USDT": {"last_score": 97, "last_vegas": 1, "pos_dir": None,
                           "last_flag": None, "consumed": True},
    })
    out = A.build_best_s3_trade()
    note = out["best"]["note"].lower()
    assert "should fire" not in note
    assert "no flag is armed" in note
