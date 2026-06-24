"""Fixed-notional dollar accounting in the S4 dry-run — paper_s4._close / _summary.

This is what makes the dry-run mirror a real Binance account (fixed margin ×
leverage = fixed notional), so the dollar math must be exact.
"""
import backtest as BT
import paper_s4


def test_close_long_computes_pnl_and_dollars():
    t = {"dir": "LONG", "entry": 100.0, "sl": 98.0}
    paper_s4._close(t, 110.0, ts=123, reason="trailing stop")
    # +10% gross, minus the round-trip fee
    assert t["pnl_pct"] == round(10.0 - BT.FEE_PCT, 3)
    assert t["pnl_usd"] == round(paper_s4.NOTIONAL_USDT * t["pnl_pct"] / 100, 4)
    assert t["R"] == 2.0                      # (100-98)/100*100
    assert t["win"] is True


def test_close_short_pnl_sign():
    t = {"dir": "SHORT", "entry": 100.0, "sl": 102.0}
    paper_s4._close(t, 90.0, ts=1, reason="trailing stop")
    # short into a drop is a win: +10% gross minus fee
    assert t["pnl_pct"] == round(10.0 - BT.FEE_PCT, 3)
    assert t["win"] is True


def test_close_loss_is_negative_dollars():
    t = {"dir": "LONG", "entry": 100.0, "sl": 98.0}
    paper_s4._close(t, 98.0, ts=1, reason="trailing stop")
    assert t["pnl_usd"] < 0
    assert t["win"] is False


def test_summary_balance_is_seed_plus_net_pnl():
    def closed(pnl_usd, win):
        return {"status": "CLOSED", "win": win, "rr": 1.0 if win else -1.0,
                "pnl_pct": 1.0, "pnl_usd": pnl_usd}
    trades = [closed(4.5, True), closed(-0.09, False), closed(2.0, True)]
    s = paper_s4._summary(trades)
    net = round(4.5 - 0.09 + 2.0, 4)
    assert s["net_pnl_usd"] == net
    assert s["balance"] == round(paper_s4.START_BALANCE + net, 4)
    assert s["best_usd"] == 4.5
    assert s["worst_usd"] == -0.09
    assert s["closed"] == 3
