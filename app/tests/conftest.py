"""Shared fixtures: synthetic OHLCV generators tuned to drive Strategy 4 through
its gates. Each candle is [ts, open, high, low, close, volume].

The shape is deliberate: a choppy base (low/flat ADX) followed by a clean
directional leg (so ADX is high AND *rising* into the last bar), with a decisive
breakout + volume surge on the final candle. This satisfies every S4 filter:
Donchian breakout + ATR buffer, EMA200 side, ADX≥25 & rising, volume gate.
"""
import math

import pytest

HOUR_MS = 3_600_000


def _uptrend(n=230, base=100.0):
    oh = []
    for i in range(n):
        if i < 190:                                  # choppy base → low ADX
            mid, vol = base + 3.0 * math.sin(i / 3.0), 1000.0
        else:                                        # clean strong climb → ADX rises
            mid, vol = base + 3.0 + 1.6 * (i - 189), 1100.0
        oh.append([i * HOUR_MS, mid - 0.3, mid + 0.5, mid - 0.8, mid, vol])
    prev_close = oh[-2][4]                            # decisive breakout + volume surge
    bo = prev_close + 5.0
    oh[-1] = [oh[-1][0], prev_close, bo + 0.3, prev_close - 0.2, bo, 4000.0]
    return oh


def _downtrend(n=230, base=300.0):
    oh = []
    for i in range(n):
        if i < 190:
            mid, vol = base + 3.0 * math.sin(i / 3.0), 1000.0
        else:
            mid, vol = base + 3.0 - 1.6 * (i - 189), 1100.0
        oh.append([i * HOUR_MS, mid + 0.3, mid + 0.8, mid - 0.5, mid, vol])
    prev_close = oh[-2][4]
    bd = prev_close - 5.0
    oh[-1] = [oh[-1][0], prev_close, prev_close + 0.2, bd - 0.3, bd, 4000.0]
    return oh


def _flat(n=230, base=100.0):
    return [[i * HOUR_MS, base, base + 0.5, base - 0.5, base, 1000.0] for i in range(n)]


@pytest.fixture
def uptrend():
    """OHLCV that produces a qualifying S4 LONG (in a BTC bull regime)."""
    return _uptrend()


@pytest.fixture
def downtrend():
    """OHLCV that would produce a SHORT breakdown (blocked by longs-only)."""
    return _downtrend()


@pytest.fixture
def flat():
    """Sideways OHLCV — no breakout, no signal."""
    return _flat()


# ── live-side-effect guard ────────────────────────────────────────────────────
# 2026-07-14 incident: a test reached strategy3_scanner.open_flip with the real
# strategy3_risk in place — it called the REAL Bybit closed-P&L endpoint, wrote
# the REAL app/s3_halt.json (halting the live engine until /resume) and sent a
# real 🛑 alert to the Telegram group. This autouse fixture makes that
# impossible for every test, whatever an individual test forgets to patch:
# the halt file is redirected into tmp_path, and telegram_utils' single
# network choke-point is stubbed (send_message still returns True and its
# chunking logic still runs — nothing leaves the process). test_telegram_utils
# is exempt from the stub: it tests _post_one's own retry/ledger behaviour
# against a patched requests layer.
@pytest.fixture(autouse=True)
def _no_live_side_effects(request, monkeypatch, tmp_path):
    import strategy3_risk
    monkeypatch.setattr(strategy3_risk, "HALT_FILE", str(tmp_path / "s3_halt.json"))
    if request.module.__name__ != "test_telegram_utils":
        import telegram_utils
        monkeypatch.setattr(telegram_utils, "_post_one",
                            lambda url, payload, retries: (True, None))
    # bybit_data is the Chinese alerts' price source — its ccxt client would
    # happily reach the real Bybit API from a formatting test. Kill the
    # exchange factory; every helper is failure-safe and degrades to its
    # Binance fallback, which is exactly the offline behaviour tests want.
    import bybit_data
    def _no_exchange():
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(bybit_data, "_exchange", _no_exchange)
    monkeypatch.setattr(bybit_data, "_bases", {})
    monkeypatch.setattr(bybit_data, "_tickers", {})
    # market_intel powers /market from public endpoints — no keys, but its ccxt
    # client and urllib helpers would still reach Binance/DefiLlama/RSS from a
    # plain page test, which is slow, flaky and (for Binance) shares the live
    # bot's IP rate-limit budget. Every producer is fail-soft, so killing the
    # three network seams degrades to empty panels + an errors list: exactly
    # the offline behaviour tests want. The cache is cleared so results never
    # leak between tests. No module exemption here: a test that wants live-ish
    # behaviour monkeypatches its own fake over these, which already wins
    # (this fixture runs first). An exemption would instead silently hand that
    # module the real network — which is how this guard got written.
    import market_intel
    def _no_net(*a, **k):
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(market_intel, "_exchange", _no_exchange)
    monkeypatch.setattr(market_intel, "_get_json", _no_net)
    monkeypatch.setattr(market_intel, "_get_text", _no_net)
    market_intel._CACHE.clear()
    # strategy3_exec.client() is the EXECUTION venue (real Bybit keys live in
    # .env, which the test process loads) — any test path that reaches it
    # unstubbed must die loudly, not silently place/read real orders. Tests
    # that need a client monkeypatch their own fake over this.
    import strategy3_exec
    def _no_bybit_client():
        raise RuntimeError("strategy3_exec.client() called in tests — stub it")
    monkeypatch.setattr(strategy3_exec, "client", _no_bybit_client)
    # Copy-trading: redirect the encrypted follower store + runtime status into
    # tmp_path (never touch the real copy_followers.json), keep the master
    # switch OFF, and make the two live-Bybit entry points die loudly so no
    # test can validate against or place orders on a real follower account.
    import config as _cfg0
    import copy_engine
    import copy_store
    monkeypatch.setattr(copy_store, "STORE_FILE", str(tmp_path / "copy_followers.json"))
    monkeypatch.setattr(copy_engine, "STATUS_FILE", str(tmp_path / "copy_status.json"))
    monkeypatch.setattr(_cfg0, "COPY_TRADING_LIVE", False)
    copy_engine._clients.clear()
    def _no_copy_net(*a, **k):
        raise RuntimeError("copy_engine live client called in tests — stub it")
    # Both network seams die loudly by default; validate()'s own logic still
    # runs (it catches the raise and returns a safe {ok:False}), so an unstubbed
    # enroll test degrades to 'invalid key', never a real Bybit call.
    monkeypatch.setattr(copy_engine, "_client", _no_copy_net)
    monkeypatch.setattr(copy_engine, "_probe_client", _no_copy_net)
    # the S1→Bybit mirror must also stay OFF unless a test opts in
    import config as _cfg
    monkeypatch.setattr(_cfg, "S1_BYBIT_MIRROR", False)
    # LINE push (dad's 台股 messages) — same single-choke-point treatment as
    # telegram: send() logic still runs, nothing leaves the process. The
    # webhook's subscription file is redirected so tests never touch the
    # real app/line_ids.json.
    import line_push
    monkeypatch.setattr(line_push, "_post", lambda path, payload: (200, "stubbed"))
    monkeypatch.setattr(line_push, "_put", lambda url, payload: (200, "stubbed"))
    monkeypatch.setattr(line_push, "_get", lambda url: (200, "{}"))
    monkeypatch.setattr(line_push, "IDS_FILE", str(tmp_path / "line_ids.json"))
    monkeypatch.setattr(line_push, "TUNNEL_LOG", str(tmp_path / "cloudflared.log"))
    monkeypatch.setattr(line_push, "QUOTA_STATE_FILE", str(tmp_path / "line_quota.json"))
    # real LINE credentials live in .env (which this process loads) — blank
    # them so enabled() is False unless a test opts in with its own fakes
    monkeypatch.setattr(_cfg, "LINE_CHANNEL_ID", "")
    monkeypatch.setattr(_cfg, "LINE_CHANNEL_SECRET", "")
    monkeypatch.setattr(_cfg, "LINE_CHANNEL_ACCESS_TOKEN", "")
    monkeypatch.setattr(_cfg, "LINE_TO", [])
    # 美股收盤 digest: redirect its state file (a test that ran tick() would
    # otherwise mark today as sent and silently suppress the real 08:00 push)
    # and kill its Yahoo seam — fetch_quotes is the module's only network.
    import us_market
    monkeypatch.setattr(us_market, "STATE_FILE", str(tmp_path / "us_market_state.json"))
    monkeypatch.setattr(us_market, "fetch_quotes", _no_net)
    # site-link announcer + watchdog write real state/log files — redirect
    import site_link
    import watchdog
    monkeypatch.setattr(site_link, "STATE_FILE", str(tmp_path / "site_link.json"))
    monkeypatch.setattr(watchdog, "STATE_FILE", str(tmp_path / "watchdog.json"))
    monkeypatch.setattr(watchdog, "LOG_DIR", str(tmp_path / "logs"))
    # the signal scorecard is months of accumulated measurement — a test that
    # calls tick()/_save_state() must never be able to overwrite the real one
    import signal_outcomes
    monkeypatch.setattr(signal_outcomes, "STATE_FILE",
                        str(tmp_path / "signal_outcomes.json"))
    monkeypatch.setattr(signal_outcomes, "SIGNALS_FILE",
                        str(tmp_path / "strategy2_signals.json"))
    # strategy attribution: the only record of which engine owned a closed
    # trade. A test that overwrote it would erase real P&L history.
    import strategy_ledger
    monkeypatch.setattr(strategy_ledger, "LEDGER_FILE",
                        str(tmp_path / "strategy_ledger.json"))
    # paper_tracker's forward record — a test must never overwrite the real
    # accumulating track record, and its own tick() hits the real Binance
    # API (fetch_ohlcv/top_symbols via backtest.py) with no other guard.
    import paper_tracker
    monkeypatch.setattr(paper_tracker, "STATE_FILE", str(tmp_path / "paper_tracker_state.json"))
    monkeypatch.setattr(paper_tracker, "_last_tick", 0.0)
    if request.module.__name__ != "test_paper_tracker":
        monkeypatch.setattr(paper_tracker, "_fetch_universe", lambda progress=lambda *a: None: ({}, []))
    # tw_financials hits the real TWSE OpenAPI (no key needed = no test guard
    # against it otherwise) every time tw_stocks.web_view() runs its real
    # body. Stub to the safe empty shape; a test that wants real-looking
    # numbers overrides this with its own monkeypatch. test_tw_financials is
    # exempt — same reason as test_telegram_utils above: it tests summary()'s
    # own parsing against a patched requests layer, not through this stub.
    if request.module.__name__ != "test_tw_financials":
        import tw_financials
        monkeypatch.setattr(tw_financials, "summary", lambda code: {
            "rev_yoy": None, "rev_month": None, "eps_cur": None,
            "eps_yoy": None, "eps_season": None, "next_deadline": ""})
