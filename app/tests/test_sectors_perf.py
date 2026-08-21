"""🧩 板塊 — the cost of drawing it.

This card is cheap to look at and was expensive to serve. The route called
market_intel.binance_futures(top_n=300), which fetches open interest with one
REST call PER SYMBOL — 300 sequential round-trips, 17.65 measured seconds,
holding a waitress worker thread for all of it. The board reads three fields
and none of them is open interest.

Two separate mistakes made that survivable, and both are pinned here:
  · the route's docstring said "costs no exchange call", so nobody looked
  · the cache TTL (45s) was SHORTER than the card's poll interval (120s), so
    the cache could never hit — every single poll paid the full cold cost

The second one is this repo's supply-vs-requirement drift again: a number in
one module outgrew a number in another and nothing connected them.
"""
import os
import re

import market_intel as MI

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _template(name="index.html"):
    with open(os.path.join(APP_DIR, "templates", name), encoding="utf-8") as f:
        return f.read()


def _poll_ms(card):
    """The poll interval the card actually ships, read from the template."""
    h = _template()
    seg = h[h.index(f'data-card="{card}"'):]
    seg = seg[:seg.index("</script>", seg.index("<script>"))]
    m = re.search(r"wolfPoll\(load,\s*(\d+)\)", seg) or \
        re.search(r"setInterval\(load,\s*(\d+)\)", seg)
    assert m, f"no poll interval found for the {card} card"
    return int(m.group(1))


def test_the_cache_outlives_the_poll_interval():
    """A TTL under the poll interval is not a cache, it is a decoration."""
    poll_s = _poll_ms("sectors") / 1000.0
    assert MI.TICKERS_TTL > poll_s, (
        f"tickers cached for {MI.TICKERS_TTL}s but the 板塊 card polls every "
        f"{poll_s}s — every poll would miss")


def test_the_sector_route_does_not_buy_open_interest():
    """One call per symbol for a field the board never reads.

    Checked on the parsed CALLS, not on the source text: the route's docstring
    explains what it stopped doing and names binance_futures while doing so,
    and a substring check would fail on the explanation of the fix.
    """
    import ast
    with open(os.path.join(APP_DIR, "app.py"), encoding="utf-8") as f:
        src = f.read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "api_sectors")
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "binance_tickers" in called
    assert "binance_futures" not in called, \
        "the route is back on the per-symbol open-interest path"


def test_binance_tickers_makes_exactly_one_exchange_call(monkeypatch):
    """The whole point. A second call per symbol would not change the ANSWER,
    so only a call count can catch this coming back."""
    calls = []

    class Ex:
        def fetch_tickers(self):
            calls.append("fetch_tickers")
            return {f"C{i}/USDT:USDT": {"quoteVolume": 100 - i, "last": 1.0,
                                        "percentage": 1.0} for i in range(50)}

        def fetch_open_interest(self, sym):
            calls.append(f"oi:{sym}")
            return {}

        def fetch_funding_rates(self, syms):
            calls.append("funding")
            return {}

    monkeypatch.setattr(MI, "_exchange", lambda: Ex())
    MI._CACHE.clear()
    rows = MI.binance_tickers(top_n=50, ttl=0)["rows"]
    assert len(rows) == 50
    assert calls == ["fetch_tickers"], f"extra exchange calls: {calls}"


def test_a_missing_24h_change_is_none_not_flat(monkeypatch):
    """`float(t.get("percentage") or 0)` made an absent reading a coin that
    did not move, and that coin then voted in its sector's median."""
    class Ex:
        def fetch_tickers(self):
            return {"A/USDT:USDT": {"quoteVolume": 9, "last": 1.0},          # no percentage
                    "B/USDT:USDT": {"quoteVolume": 8, "last": 1.0, "percentage": 3.0}}

    monkeypatch.setattr(MI, "_exchange", lambda: Ex())
    MI._CACHE.clear()
    rows = {r["base"]: r for r in MI.binance_tickers(top_n=10, ttl=0)["rows"]}
    assert rows["A"]["change_pct"] is None, "an unread change became 0.0%"
    assert rows["B"]["change_pct"] == 3.0
    # …and the board must drop it rather than count it as flat.
    import sectors
    kept = [r for r in rows.values() if r["change_pct"] is not None]
    assert len(kept) == 1
    assert sectors.board(list(rows.values()), exclude=set())["sectors"] is not None


def test_a_dead_ticker_feed_is_reported_not_silently_empty(monkeypatch):
    class Ex:
        def fetch_tickers(self):
            raise RuntimeError("exchange down")

    monkeypatch.setattr(MI, "_exchange", lambda: Ex())
    MI._CACHE.clear()
    out = MI.binance_tickers(top_n=10, ttl=0)
    assert out["rows"] == []
    assert out["errors"], "a dead feed returned an empty board with no error"
