"""Strategy-2 candle cache — bucket reuse, forming-candle patching, kill switch.

The cache must NEVER change closed-bar data (signals are computed on it);
it may only refresh the forming candle from the live ticker."""
import strategy2_scanner as S2S

NOW = 1_783_900_000.0 - (1_783_900_000.0 % 900) + 100   # 100s into a bucket
SYM = "BTC/USDT:USDT"


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.fetches = 0

    def call(self, method, *args):
        assert method == "fetch_ohlcv"
        self.fetches += 1
        return [r[:] for r in self.rows]


def _rows(last_bucket, n=3):
    """n candles whose LAST one opens at `last_bucket`."""
    return [[(last_bucket - (n - 1 - i)) * 900 * 1000,
             100.0, 101.0, 99.0, 100.5, 10.0] for i in range(n)]


def _fresh(monkeypatch, cache_on=True):
    monkeypatch.setattr(S2S, "OHLCV_CACHE_ON", cache_on)
    monkeypatch.setattr(S2S, "_ohlcv_cache", {})


def test_cache_reuses_within_bucket(monkeypatch):
    _fresh(monkeypatch)
    client = FakeClient(_rows(S2S._bucket(NOW)))
    _r1, c1 = S2S._get_ohlcv(client, SYM, {}, now=NOW)
    _r2, c2 = S2S._get_ohlcv(client, SYM, {}, now=NOW + 60)
    assert (c1, c2) == (False, True)
    assert client.fetches == 1


def test_cache_refetches_on_new_bucket(monkeypatch):
    _fresh(monkeypatch)
    client = FakeClient(_rows(S2S._bucket(NOW)))
    S2S._get_ohlcv(client, SYM, {}, now=NOW)
    _rows2, cached = S2S._get_ohlcv(client, SYM, {}, now=NOW + 900)
    assert cached is False
    assert client.fetches == 2


def test_cached_rows_get_live_ticker_price(monkeypatch):
    _fresh(monkeypatch)
    client = FakeClient(_rows(S2S._bucket(NOW)))
    S2S._get_ohlcv(client, SYM, {}, now=NOW)
    rows, cached = S2S._get_ohlcv(client, SYM, {SYM: {"last": 107.0}}, now=NOW + 60)
    assert cached is True
    assert rows[-1][4] == 107.0          # close = live price
    assert rows[-1][2] == 107.0          # high stretched
    assert rows[-1][3] == 99.0           # low untouched
    # closed bars must be byte-identical to the fetch
    assert rows[:-1] == _rows(S2S._bucket(NOW))[:-1]


def test_patch_appends_synthetic_when_snapshot_ended_on_boundary():
    rows = _rows(S2S._bucket(NOW) - 1)          # ends in the PREVIOUS bucket
    out = S2S._patch_forming([r[:] for r in rows], 105.0, NOW)
    assert len(out) == len(rows) + 1
    assert out[-1][0] == S2S._bucket(NOW) * 900 * 1000
    assert out[-1][1:] == [105.0, 105.0, 105.0, 105.0, 0.0]


def test_patch_without_price_is_a_noop():
    rows = _rows(S2S._bucket(NOW))
    assert S2S._patch_forming([r[:] for r in rows], None, NOW) == rows


def test_intra_bucket_high_low_accumulates(monkeypatch):
    _fresh(monkeypatch)
    client = FakeClient(_rows(S2S._bucket(NOW)))
    S2S._get_ohlcv(client, SYM, {}, now=NOW)
    S2S._get_ohlcv(client, SYM, {SYM: {"last": 110.0}}, now=NOW + 60)
    rows, _c = S2S._get_ohlcv(client, SYM, {SYM: {"last": 95.0}}, now=NOW + 120)
    assert rows[-1][2] == 110.0          # spike high remembered
    assert rows[-1][3] == 95.0           # new low recorded
    assert rows[-1][4] == 95.0


def test_kill_switch_disables_cache(monkeypatch):
    _fresh(monkeypatch, cache_on=False)
    client = FakeClient(_rows(S2S._bucket(NOW)))
    _r1, c1 = S2S._get_ohlcv(client, SYM, {}, now=NOW)
    _r2, c2 = S2S._get_ohlcv(client, SYM, {}, now=NOW + 60)
    assert (c1, c2) == (False, False)
    assert client.fetches == 2
