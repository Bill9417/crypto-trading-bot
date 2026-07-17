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
    assert "每日報告" in msg and "2026-07-10" in msg
    assert "Binance · S1/S2" in msg and "Bybit · S3" in msg
    assert "🌡 市場" in msg and "🗓 今日" in msg


def test_report_pnl_lines():
    msg = DR.build_report(_data(), _now())
    # Binance: today −0.42, yesterday +1.13, 7d = their sum
    assert "今日 -0.42" in msg and "昨日 +1.13" in msg and "7 日 +0.71" in msg
    # Bybit: yesterday −3.20
    assert "昨日 -3.20" in msg


def test_report_positions_and_flat():
    msg = DR.build_report(_data(), _now())
    assert "ETH 做多 +0.42（+2.10%）" in msg      # Binance open position
    assert "無持倉" in msg                        # Bybit is flat


def test_report_calendar_today_only():
    msg = DR.build_report(_data(), _now())
    assert "CPI y/y（預測 2.4%）" in msg and "20:30" in msg
    assert "NFP" not in msg                      # tomorrow's print stays out


def test_report_degrades_without_data():
    msg = DR.build_report({}, _now())
    assert "餘額暫時無法取得" in msg
    assert "無 — 平靜的總經日" in msg             # calendar section still renders
