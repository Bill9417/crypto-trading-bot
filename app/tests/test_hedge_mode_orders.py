"""What positionIdx each live order path actually puts on the wire.

test_bybit_mode.py proves the index helper is right in isolation. This file
proves the CALL SITES hand it the right thing, which is the part that silently
breaks: in hedge mode positionIdx names the POSITION, so closing a long is
`side=sell, positionIdx=1`. Pass the order side instead and you get 2 — Bybit
then reads a reduce-only sell against the short book and the position stays
open, with no error to notice. Every close path below is pinned for that.
"""
import bybit_mode as B
import config
import s1_bybit_mirror as M
import strategy3_exec as X

SYM = "ETH/USDT:USDT"


def _exchange(sent):
    """Minimal ccxt stand-in recording (side, params) per order."""
    class _Ex:
        markets = {SYM: {}}

        def market(self, s):
            return {"id": "ETHUSDT"}

        def price_to_precision(self, s, p):
            return str(p)

        def set_leverage(self, lev, s):
            return None

        def create_order(self, symbol, typ, side, qty, params=None):
            sent.append({"side": side, "qty": qty, **(params or {})})
            return {"id": "o1"}

        def privatePostV5PositionTradingStop(self, body):
            sent.append({"trading_stop": True, **body})
            return {"retCode": 0}

    return _Ex()


def _live(monkeypatch, sent, position=None):
    monkeypatch.setattr(X, "is_live", lambda: True)
    monkeypatch.setattr(X, "keys_present", lambda: True)
    monkeypatch.setattr(X, "client", lambda: _exchange(sent))
    monkeypatch.setattr(X, "_market_limits", lambda s: (0.01, 0.01, 5.0))
    monkeypatch.setattr(X, "get_position", lambda s: position)
    monkeypatch.setattr(X, "ensure_stop", lambda *a, **k: None)


# ── S3 execution ─────────────────────────────────────────────────────────────
def test_one_way_still_sends_index_zero(monkeypatch):
    """The default path must be byte-identical to before this feature."""
    sent = []
    _live(monkeypatch, sent)
    X.open_flip(SYM, "long", 3500.0, 3430.0, 50.0, 10)
    assert sent[0]["positionIdx"] == 0 and sent[0]["side"] == "buy"


def test_hedge_open_indexes_by_direction(monkeypatch):
    sent = []
    _live(monkeypatch, sent)
    B.learn(SYM, B.HEDGE)
    X.open_flip(SYM, "short", 3500.0, 3570.0, 50.0, 10)
    assert sent[0]["side"] == "sell" and sent[0]["positionIdx"] == 2


def test_hedge_close_of_a_long_indexes_the_LONG(monkeypatch):
    """THE trap. side=sell but positionIdx=1 — the long's index, not the
    sell's. Index 2 here would close nothing and leave the position naked."""
    sent = []
    _live(monkeypatch, sent, position={"side": "long", "qty": 1.0, "entry": 3500.0,
                                       "mark": 3500.0, "sl": None, "idx": 1})
    B.learn(SYM, B.HEDGE)
    X.close_flip(SYM)
    assert sent[0]["side"] == "sell"
    assert sent[0]["positionIdx"] == 1
    assert sent[0]["reduceOnly"] is True


def test_hedge_close_of_a_short_indexes_the_SHORT(monkeypatch):
    sent = []
    _live(monkeypatch, sent, position={"side": "short", "qty": 1.0, "entry": 3500.0,
                                       "mark": 3500.0, "sl": None, "idx": 2})
    B.learn(SYM, B.HEDGE)
    X.close_flip(SYM)
    assert sent[0]["side"] == "buy" and sent[0]["positionIdx"] == 2


def test_hedge_stop_move_targets_the_position_side(monkeypatch):
    sent = []
    pos = {"side": "short", "qty": 1.0, "entry": 3500.0, "mark": 3500.0,
           "sl": 3570.0, "idx": 2}
    _live(monkeypatch, sent, position=pos)
    B.learn(SYM, B.HEDGE)
    X.set_stop(SYM, 3550.0)
    assert sent[0]["trading_stop"] and sent[0]["positionIdx"] == 2


def test_set_stop_refuses_when_flat(monkeypatch):
    """It needs a side to index; guessing one would arm a stop on the wrong
    book. Callers already treat a raise as 'retry next poll'."""
    sent = []
    _live(monkeypatch, sent, position=None)
    try:
        X.set_stop(SYM, 3550.0)
        raise AssertionError("expected a raise")
    except RuntimeError as exc:
        assert "no open position" in str(exc)
    assert sent == []


def test_a_live_position_teaches_the_mode_for_free(monkeypatch):
    """get_position learns from the position's own index — the one reading
    Bybit exposes that is actually trustworthy."""
    class _Ex:
        def fetch_positions(self, syms):
            return [{"contracts": 1.0, "side": "short", "entryPrice": 3500.0,
                     "markPrice": 3500.0,
                     "info": {"positionIdx": "2", "stopLoss": "3570"}}]
    monkeypatch.setattr(X, "client", lambda: _Ex())
    pos = X.get_position(SYM)
    assert pos["idx"] == 2
    assert B.mode_of(SYM) == B.HEDGE


# ── S1 mirror ────────────────────────────────────────────────────────────────
def test_mirror_reduce_of_a_short_indexes_the_SHORT(monkeypatch, tmp_path):
    sent = []
    _live(monkeypatch, sent)
    monkeypatch.setattr(config, "S1_BYBIT_MIRROR", True)
    B.learn(SYM, B.HEDGE)
    assert M._reduce(SYM, "short", 5.0) == ""
    assert sent[0]["side"] == "buy"             # closing a short = buy …
    assert sent[0]["positionIdx"] == 2          # … at the SHORT index


def test_mirror_tp_attaches_to_the_position_side(monkeypatch, tmp_path):
    sent = []
    _live(monkeypatch, sent)
    monkeypatch.setattr(config, "S1_BYBIT_MIRROR", True)
    B.learn(SYM, B.HEDGE)
    assert M._set_tp(SYM, 3200.0, "short") == ""
    assert sent[0]["trading_stop"] and sent[0]["positionIdx"] == 2


def test_mirror_retries_a_rejected_index_instead_of_losing_the_close(monkeypatch):
    """The 2026-07-09 failure mode, on the mirror's close path."""
    calls = []

    class _Ex:
        def create_order(self, symbol, typ, side, qty, params=None):
            calls.append(params["positionIdx"])
            if params["positionIdx"] == 0:
                raise RuntimeError('bybit {"retCode":10001,"retMsg":'
                                   '"position idx not match position mode"}')
            return {"id": "o1"}

    monkeypatch.setattr(X, "is_live", lambda: True)
    monkeypatch.setattr(X, "keys_present", lambda: True)
    monkeypatch.setattr(X, "client", lambda: _Ex())
    monkeypatch.setattr(config, "S1_BYBIT_MIRROR", True)
    assert M._reduce(SYM, "long", 5.0) == ""    # succeeded on the retry
    assert calls == [0, 1]
