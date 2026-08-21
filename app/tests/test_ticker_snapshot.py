"""📈 TickerSnapshot — the shared price cache behind /api/live_prices.

Measured before: /api/live_prices called the exchange on EVERY request, 2.08s
each, polled every 10 seconds by the dashboard. One open tab held a waitress
worker for 2 seconds out of every 10 — 12.5 of the 14.3 thread-seconds per
minute the whole dashboard cost — and fired a real Binance call each time.

The subtlety is not "add a cache". It is what happens at each age, and every
one of those branches is a way to be wrong:

  · serving stale data without SAYING it is stale turns an old reading into a
    current one — the same fabrication as writing 0 for something unmeasured
  · blocking on every miss turns a cache into a stampede under load
  · a failed refresh that empties the cache is worse than no cache at all
  · refreshing in the background but not single-flighting it means N tabs
    still make N calls, which was the whole problem

None of those are visible in the returned prices, so they are only checkable by
counting calls and controlling the clock.
"""
import threading
import time

import pytest

from market_data import TickerSnapshot


class FakeClient:
    """Counts calls, can be made slow or made to fail."""

    def __init__(self, delay=0.0, fail=False):
        self.calls = 0
        self.delay = delay
        self.fail = fail
        self.value = {"BTC/USDT:USDT": {"last": 100.0},
                      "ETH/USDT:USDT": {"last": 10.0}}
        self._lock = threading.Lock()

    def call(self, _method, *a, **k):
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("exchange down")
        return dict(self.value)


def _settle():
    """Give a background refresh thread a moment to finish."""
    for _ in range(50):
        time.sleep(0.02)


def test_the_first_call_fetches_and_the_next_ones_do_not():
    c = FakeClient()
    s = TickerSnapshot(c, ttl=10, max_stale=60)
    for _ in range(5):
        data, age = s.get(["BTC/USDT:USDT"])
    assert c.calls == 1, f"{c.calls} exchange calls for 5 requests"
    assert data["BTC/USDT:USDT"]["last"] == 100.0
    assert age is not None and age < 10


def test_a_stale_snapshot_is_served_immediately_and_refreshed_behind_it():
    """The branch that removes the 2s wait. A request past the TTL must NOT
    block — it gets the old answer now and the new one lands for the next."""
    c = FakeClient(delay=0.30)
    s = TickerSnapshot(c, ttl=0.05, max_stale=60)
    s.get()                                   # prime (blocks once)
    assert c.calls == 1
    time.sleep(0.10)                          # now stale, not expired
    t = time.time()
    data, age = s.get(["BTC/USDT:USDT"])
    elapsed = time.time() - t
    assert elapsed < 0.15, f"stale read blocked for {elapsed:.2f}s"
    assert data, "served nothing while refreshing"
    assert age > 0.05, "reported itself as fresh while stale"
    _settle()
    assert c.calls == 2, "the background refresh never ran"


def test_an_expired_snapshot_blocks_rather_than_lying():
    """Past max_stale the data is not worth serving. A live price board showing
    a silently minutes-old number is worse than a slow one."""
    c = FakeClient()
    s = TickerSnapshot(c, ttl=0.01, max_stale=0.05)
    s.get()
    time.sleep(0.12)
    c.value = {"BTC/USDT:USDT": {"last": 999.0}}
    data, age = s.get(["BTC/USDT:USDT"])
    assert data["BTC/USDT:USDT"]["last"] == 999.0, "served past max_stale"
    assert age < 0.05


def test_concurrent_misses_cost_one_exchange_call():
    """Ten threads arriving on a cold cache must not make ten calls — that is
    the stampede the cache exists to prevent."""
    c = FakeClient(delay=0.25)
    s = TickerSnapshot(c, ttl=10, max_stale=60)
    out = []
    threads = [threading.Thread(target=lambda: out.append(s.get()))
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.calls == 1, f"{c.calls} concurrent exchange calls"
    assert len(out) == 10


def test_a_failed_refresh_keeps_the_old_prices():
    """An exchange hiccup must degrade to yesterday's number, not to a blank
    board. Returning {} here would render every price as '—'."""
    c = FakeClient()
    s = TickerSnapshot(c, ttl=0.01, max_stale=999)
    s.get()
    c.fail = True
    time.sleep(0.05)
    data, age = s.get(["BTC/USDT:USDT"])
    _settle()
    data2, _ = s.get(["BTC/USDT:USDT"])
    assert data["BTC/USDT:USDT"]["last"] == 100.0
    assert data2["BTC/USDT:USDT"]["last"] == 100.0, "a failed refresh emptied it"


def test_it_reports_how_old_it_is():
    """A cache that cannot say its age turns a stale reading into a current
    one. Callers get the number whether or not they use it today."""
    c = FakeClient()
    s = TickerSnapshot(c, ttl=10, max_stale=60)
    _d, age0 = s.get()
    time.sleep(0.20)
    _d, age1 = s.get()
    assert age1 > age0 >= 0
    assert age1 >= 0.19


def test_no_data_at_all_reports_none_rather_than_zero_age():
    """age 0 means 'fetched just now'. An empty cache is not that."""
    c = FakeClient(fail=True)
    s = TickerSnapshot(c, ttl=10, max_stale=60)
    data, age = s.get(["BTC/USDT:USDT"])
    assert data == {}
    assert age is None, "an empty cache claimed to be freshly fetched"


def test_it_filters_to_the_symbols_asked_for():
    c = FakeClient()
    s = TickerSnapshot(c, ttl=10, max_stale=60)
    data, _ = s.get(["BTC/USDT:USDT", "NOPE/USDT:USDT", None])
    assert set(data) == {"BTC/USDT:USDT"}
    allsyms, _ = s.get()
    assert set(allsyms) == {"BTC/USDT:USDT", "ETH/USDT:USDT"}


# ── the endpoint ─────────────────────────────────────────────────────────────
def test_live_prices_uses_the_shared_snapshot(monkeypatch):
    """A second uncached path would put the 2s back without changing anything
    visible."""
    import app as APP
    c = FakeClient()
    monkeypatch.setattr(APP, "_ticker_snapshot",
                        TickerSnapshot(c, ttl=10, max_stale=60))
    for _ in range(4):
        APP.fetch_live_tickers(["BTC/USDT:USDT"])
    assert c.calls == 1, f"{c.calls} exchange calls for 4 requests"


def test_live_prices_never_calls_the_exchange_directly():
    """Checked on the parsed calls: the old code called rest_client itself."""
    import ast
    import os
    app_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "app.py")
    with open(app_py, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "fetch_live_tickers")
    direct = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "call"
              and getattr(n.func.value, "id", "") == "rest_client"]
    assert not direct, "fetch_live_tickers calls the exchange directly again"


@pytest.mark.parametrize("ttl,poll", [(8.0, 10.0)])
def test_the_ttl_is_under_the_poll_interval(ttl, poll):
    """Deliberately UNDER, unlike the 板塊 cache which had to be over.

    Opposite reasons, and the difference is worth stating: 板塊 shows a 24h
    median that a poll should be able to reuse, so its TTL must outlive the
    interval. This is a live price board — the data must actually turn over
    every poll. The saving here is that the turnover happens in ONE background
    thread instead of in every request, and is shared by every tab.
    """
    import app as APP
    assert APP._ticker_snapshot.ttl <= poll
    assert APP._ticker_snapshot.max_stale > poll
