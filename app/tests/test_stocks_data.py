"""stocks_data — MIS quote parsing (the '-' last-trade fallback chain)."""
import stocks_data


def test_mis_price_uses_last_trade_when_present():
    assert stocks_data._mis_price({"z": "2335.0000", "b": "2330.0000_2325.0000"}) == 2335.0


def test_mis_price_falls_back_to_pz():
    assert stocks_data._mis_price({"z": "-", "pz": "710.0000"}) == 710.0


def test_mis_price_falls_back_to_best_bid():
    # mid-session lull: no trade in the 5-second snapshot, book still live
    m = {"z": "-", "pz": "-", "b": "41.1500_41.1000_41.0500", "a": "41.2000_41.2500"}
    assert stocks_data._mis_price(m) == 41.15


def test_mis_price_limit_down_uses_ask_and_skips_zero():
    # locked limit-down: no bids, ask string leads with '0.0000'
    m = {"z": "-", "pz": "-", "b": "-", "a": "0.0000_130.0000_130.5000"}
    assert stocks_data._mis_price(m) == 130.0


def test_mis_price_no_data_returns_none():
    assert stocks_data._mis_price({"z": "-", "pz": "-", "b": "-", "a": "-"}) is None
    assert stocks_data._mis_price({}) is None
