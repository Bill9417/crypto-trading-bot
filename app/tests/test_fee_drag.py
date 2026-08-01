"""Fee drag — is the account too small to clear its own costs?

Bybit reports closedPnl already NET of fees, so a strategy can be gross-
positive and net-negative and look like a broken edge when it is actually a
cost problem. Different diagnosis, different fix.
"""
import strategy_ledger as L


def _t(pnl, fees, symbol="ETHUSDT", ts=0, notional=100.0, lev=10.0):
    return {"symbol": symbol, "pnl": pnl, "fees": fees, "time": ts,
            "notional": notional, "lev": lev}


def test_gross_is_net_plus_fees():
    d = L.fee_drag([_t(1.0, 0.10), _t(-0.5, 0.10)])
    assert d["n"] == 2
    assert d["net"] == 0.5
    assert d["fees"] == 0.2
    assert d["gross"] == 0.7


def test_fee_share_of_a_positive_gross():
    d = L.fee_drag([_t(0.5, 0.5)])          # gross 1.0, fees 0.5
    assert d["fee_pct_of_gross"] == 50.0
    assert "吃掉毛利的" in L.fee_report([_t(0.5, 0.5)])


def test_a_cost_problem_is_named_as_one():
    """Gross-positive but more than half eaten — say it is cost, not strategy."""
    rep = L.fee_report([_t(0.1, 0.9)])       # gross 1.0, fees 0.9
    assert "成本問題，不是策略問題" in rep


def test_no_percentage_is_invented_on_a_gross_loss():
    """A negative gross has no edge for fees to be a fraction OF."""
    d = L.fee_drag([_t(-5.0, 0.2)])
    assert d["fee_pct_of_gross"] is None
    rep = L.fee_report([_t(-5.0, 0.2)])
    assert "%" not in rep.split("\n")[-1]
    assert "問題不在成本" in rep


def test_fees_are_counted_as_a_cost_whatever_their_sign():
    """Some venues report fees negative; magnitude is what matters."""
    assert L.fee_drag([_t(1.0, -0.25)])["fees"] == 0.25


def test_silent_when_there_is_nothing_to_report():
    assert L.fee_report([]) == ""
    assert L.fee_report([_t(1.0, 0.0)]) == ""


def test_missing_fee_field_degrades_to_zero():
    d = L.fee_drag([{"symbol": "ETHUSDT", "pnl": 1.0}])
    assert d["fees"] == 0.0 and d["gross"] == 1.0


def test_per_strategy_fee_drag(monkeypatch, tmp_path):
    import time
    monkeypatch.setattr(L, "LEDGER_FILE", str(tmp_path / "l.json"))
    now = time.time()
    L.record_open("S1", "ETH/USDT:USDT", "long", ts=now - 100)
    L.record_close("S1", "ETH/USDT:USDT", ts=now - 50)
    trades = [_t(1.0, 0.3, "ETHUSDT", int((now - 60) * 1000)),
              # notional/leverage deliberately OUTSIDE S1's fingerprint, or
              # infer() would file this unrecorded trade as S1 too
              _t(2.0, 0.4, "SNDKUSDT", int((now - 60) * 1000),
                 notional=5000.0, lev=3.0)]
    d = L.fee_drag(trades, strategy="S1")
    assert d["n"] == 1 and d["fees"] == 0.3      # SNDK is not S1's
