"""Daily Report — pure-logic tests (no network, no state file)."""
from datetime import datetime, timedelta

import daily_report as DR


def _now(hour=9):
    return datetime(2026, 7, 10, hour, 30, tzinfo=DR.TZ)


def _daily(*nets):
    """Zero-filled 14-day series ending today, with the given tail values."""
    base = [0.0] * (14 - len(nets)) + list(nets)
    day0 = _now().date() - timedelta(days=13)
    return [{"date": (day0 + timedelta(days=i)).strftime("%Y-%m-%d"), "net": v}
            for i, v in enumerate(base)]


def _data():
    return {
        "binance_snap": {
            "balance": {"wallet": 25.31, "available": 21.02, "unrealized_pnl": 0.42},
            "positions": [{"symbol": "ETH/USDT:USDT", "side": "LONG",
                           "unrealized_pnl": 0.42, "pnl_pct": 2.1}],
        },
        "binance_pnl": {"daily": _daily(1.13, -0.42)},
        "bybit_snap": {"balance": {"equity": 235.6, "available": 195.0,
                                   "unrealized_pnl": 0.0}, "positions": []},
        "bybit_pnl": {"daily": _daily(-3.2, 0.0)},
        "btc": {"price": 108432.0, "change_pct": 1.2},
        "fng": {"value": 61, "label": "Greed"},
        "calendar": [
            {"title": "CPI y/y", "date": _now(20).isoformat(), "forecast": "2.4%"},
            {"title": "NFP", "date": (_now(20) + timedelta(days=1)).isoformat()},
        ],
    }


# ── cadence gate ─────────────────────────────────────────────────────────────
def test_due_only_after_report_hour():
    assert not DR._due({}, _now(hour=DR.REPORT_HOUR - 1))
    assert DR._due({}, _now(hour=DR.REPORT_HOUR))


def test_due_once_per_day():
    today = _now().strftime("%Y-%m-%d")
    assert not DR._due({"last_report": today}, _now())
    assert DR._due({"last_report": "2026-07-09"}, _now())


# ── report body ──────────────────────────────────────────────────────────────
def test_report_has_all_sections():
    msg = DR.build_report(_data(), _now())
    assert "每日帳戶報告" in msg and "2026-07-10" in msg
    assert "Binance · S1/S2" in msg and "Bybit · S3" in msg


def test_the_account_report_carries_only_the_account():
    """2026-08-08: the owner asked for the two daily DMs to do two different
    jobs — this one is the account, the 🧵 threads draft is the postable market
    one. Prices and the macro calendar leaking back in here is the regression."""
    msg = DR.build_report(_data(), _now())
    for market_only in ("🌡 市場", "🗓 今日", "本週大事", "恐懼貪婪",
                        "貪婪指數", "重大事件"):
        assert market_only not in msg, f"market content back in the account report: {market_only}"


def test_report_pnl_lines():
    msg = DR.build_report(_data(), _now())
    # Binance: today −0.42, yesterday +1.13, 7d = their sum
    assert "今日 -0.42" in msg and "昨日 +1.13" in msg and "7日 +0.71" in msg
    # Bybit: yesterday −3.20
    assert "昨日 -3.20" in msg
    assert "<pre>" in msg                        # aligned account tables


def test_report_positions_and_flat():
    msg = DR.build_report(_data(), _now())
    row = next(ln for ln in msg.splitlines() if "▸ ETH" in ln)
    assert "做多" in row and "+0.42" in row and "+2.10%" in row
    assert "持倉" in msg and "無" in msg          # Bybit is flat




def test_report_lists_hand_opened_positions_it_does_not_manage():
    """The balance line counts their P&L, so calling the account 持倉 無 was a
    self-contradiction — and a manual position with no stop stayed invisible."""
    data = _data()
    data["bybit_snap"]["foreign"] = [
        {"symbol": "ETH/USDT:USDT", "side": "LONG", "contracts": 0.41,
         "unrealized_pnl": -1.11, "pnl_pct": -0.5, "sl": None, "engine": "手動"}]
    msg = DR.build_report(data, _now())
    row = next(ln for ln in msg.splitlines() if "手動" in ln)
    assert "ETH" in row and "-1.11" in row and "無停損" in row
    assert "機器人持倉" in msg                    # bot itself is flat, said plainly
