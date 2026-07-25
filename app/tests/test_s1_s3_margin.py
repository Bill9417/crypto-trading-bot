"""S1 and S3 share one Bybit sub-account — S1 must not spend S3's entry margin.

S3 trades one symbol off a slow 30m flag and cannot re-enter if it misses the
flip; S1 fires often and skipping a mirror costs little. So when margin is
tight, S1 gives way.
"""
import config
import s1_bybit_mirror as M


def _params(margin):
    return lambda base: {"margin": margin, "leverage": 50, "timeframe": "30m",
                         "sl_pct": 0.015}


def test_reserve_covers_a_flat_s3_symbol(monkeypatch):
    monkeypatch.delenv("S1_BYBIT_RESERVE_USDT", raising=False)
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["XAUT"])
    monkeypatch.setattr(config, "strategy3_params", _params(30.0))
    monkeypatch.setattr(M.X, "get_position", lambda sym: None)   # flat
    assert M._s3_reserve() == 30.0


def test_open_s3_position_is_not_reserved_twice(monkeypatch):
    """Its margin already sits in 'used' — reserving again would double-count
    and needlessly block S1."""
    monkeypatch.delenv("S1_BYBIT_RESERVE_USDT", raising=False)
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["XAUT"])
    monkeypatch.setattr(config, "strategy3_params", _params(30.0))
    monkeypatch.setattr(M.X, "get_position", lambda sym: {"side": "short"})
    assert M._s3_reserve() == 0.0


def test_reserve_is_conservative_when_positions_cannot_be_read(monkeypatch):
    monkeypatch.delenv("S1_BYBIT_RESERVE_USDT", raising=False)
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["XAUT"])
    monkeypatch.setattr(config, "strategy3_params", _params(30.0))

    def boom(sym):
        raise RuntimeError("bybit down")
    monkeypatch.setattr(M.X, "get_position", boom)
    assert M._s3_reserve() == 30.0        # assume S3 still needs the room


def test_reserve_can_be_overridden_and_disabled(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["XAUT"])
    monkeypatch.setattr(config, "strategy3_params", _params(30.0))
    monkeypatch.setattr(M.X, "get_position", lambda sym: None)
    monkeypatch.setenv("S1_BYBIT_RESERVE_USDT", "75")
    assert M._s3_reserve() == 75.0
    monkeypatch.setenv("S1_BYBIT_RESERVE_USDT", "0")
    assert M._s3_reserve() == 0.0
    monkeypatch.setenv("S1_BYBIT_RESERVE_USDT", "not-a-number")
    assert M._s3_reserve() == 30.0        # garbage falls back to the real need


def test_reserve_ignores_symbols_with_no_params(monkeypatch):
    monkeypatch.delenv("S1_BYBIT_RESERVE_USDT", raising=False)
    monkeypatch.setattr(config, "STRATEGY3_SYMBOLS", ["NOPE"])

    def boom(base):
        raise KeyError(base)
    monkeypatch.setattr(config, "strategy3_params", boom)
    assert M._s3_reserve() == 0.0
