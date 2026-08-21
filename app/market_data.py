import json
import os
import threading
import time

import ccxt


class RateLimitCooldownError(RuntimeError):
    pass


def is_tradfi_market(market: dict | None) -> bool:
    """True for Binance's TradFi stock/ETF perps (AAPL, NVDA, QQQ, SPY, …),
    identified by underlyingType EQUITY / contractType TRADIFI_PERPETUAL in the
    exchange info. Trading them needs a separately signed agreement — without it
    every order is rejected with -4411 — so the scanners and executor use this
    to keep them out of the tradeable universe (config.EXCLUDE_TRADFI_PERPS)."""
    info = (market or {}).get("info") or {}
    if str(info.get("underlyingType", "")).upper() == "EQUITY":
        return True
    return "TRADIFI" in str(info.get("contractType", "")).upper()


def timeframe_to_seconds(timeframe: str) -> int:
    units = {
        "m": 60,
        "h": 3600,
        "d": 86400,
    }
    unit = timeframe[-1]
    if unit not in units:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return int(timeframe[:-1]) * units[unit]


class SafeBinanceClient:
    def __init__(self, min_rest_interval: float = 0.25, max_retries: int = 4):
        self.exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        self.min_rest_interval = float(min_rest_interval)
        self.max_retries = int(max_retries)
        self._last_call_at = 0.0
        self._cooldown_until = 0.0
        self._lock = threading.Lock()

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def _respect_min_interval(self) -> None:
        elapsed = time.time() - self._last_call_at
        if elapsed < self.min_rest_interval:
            time.sleep(self.min_rest_interval - elapsed)

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(token in text for token in ["429", "418", "rate limit", "too many requests"])

    def call(self, method_name: str, *args, **kwargs):
        if self.cooldown_remaining() > 0:
            raise RateLimitCooldownError(f"REST cooldown active for {self.cooldown_remaining():.1f}s")

        last_exc = None
        for attempt in range(self.max_retries):
            try:
                with self._lock:
                    self._respect_min_interval()
                    method = getattr(self.exchange, method_name)
                    result = method(*args, **kwargs)
                    self._last_call_at = time.time()
                return result
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if self._is_rate_limit_error(exc):
                    cooldown_seconds = min(300, 30 * (attempt + 1))
                    self._cooldown_until = time.time() + cooldown_seconds
                    raise RateLimitCooldownError(f"{exc}; cooling down for {cooldown_seconds}s") from exc
                if attempt >= self.max_retries - 1:
                    raise
                time.sleep(min(5, 0.5 * (attempt + 1)))

        if last_exc:
            raise last_exc


class TickerSnapshot:
    """A shared, self-refreshing ticker cache so a page load never waits on the
    exchange.

    /api/live_prices called fetch_tickers on EVERY request with no cache. The
    dashboard polls it every 10 seconds and the call measures 2.08s, so one
    open tab held a waitress worker for 2 seconds out of every 10 — 12.5 of the
    14.3 thread-seconds per minute the whole dashboard costs — and fired a real
    Binance REST call every 10s per tab. Two tabs, two calls. A phone left open
    on the sofa, another.

    Binance has no multi-symbol ticker endpoint, so ccxt fetches ALL of them and
    filters; asking for 5 symbols costs the same as asking for 500. That makes
    ONE shared snapshot strictly better than N filtered ones.

    Three ages, three behaviours, and the middle one is the point:
      fresh   (<= ttl)        serve, touch nothing
      stale   (<= max_stale)  serve IMMEDIATELY, refresh in the background
      expired (> max_stale)   block and refresh — better a slow answer than a
                              silently minutes-old price on a live board
    A refresh is single-flight: concurrent callers wait for the one in progress
    instead of each starting their own, which is how a cache miss under load
    turns into the stampede it was meant to prevent.

    `age` is returned, never hidden. A cache that cannot say how old it is
    turns a stale reading into a current one, which is the same fabrication as
    writing 0 for a value nobody measured.
    """

    def __init__(self, client, ttl: float = 8.0, max_stale: float = 60.0):
        self.client = client
        self.ttl = float(ttl)
        self.max_stale = float(max_stale)
        self._data: dict = {}
        self._at = 0.0
        self._lock = threading.Lock()          # guards _data/_at
        self._refresh_lock = threading.Lock()  # single-flight
        self._refreshing = False

    def _snapshot(self):
        with self._lock:
            return self._data, self._at

    def _refresh(self) -> bool:
        """One fetch. Returns False on failure, leaving the old data in place —
        a failed refresh must not empty the board."""
        try:
            fresh = self.client.call("fetch_tickers")
        except Exception as exc:  # noqa: BLE001 — callers get the stale copy
            print(f"[tickers] refresh failed: {exc}")
            return False
        if not fresh:
            return False
        with self._lock:
            self._data = fresh
            self._at = time.time()
        return True

    def _refresh_once(self) -> None:
        """Single-flight refresh: the first caller does the work, the rest
        return immediately rather than queueing behind it."""
        if self._refreshing:
            return
        with self._refresh_lock:
            if self._refreshing:
                return
            self._refreshing = True
        try:
            self._refresh()
        finally:
            self._refreshing = False

    def _refresh_background(self) -> None:
        t = threading.Thread(target=self._refresh_once, name="ticker-refresh",
                             daemon=True)
        t.start()

    def get(self, symbols=None) -> tuple:
        """(tickers, age_seconds). age is None when there is no data at all."""
        data, at = self._snapshot()
        age = (time.time() - at) if at else None
        if age is None or age > self.max_stale:
            # Nothing usable. Block — but single-flight, so ten concurrent
            # requests cost one exchange call, not ten.
            self._refresh_once()
            data, at = self._snapshot()
            age = (time.time() - at) if at else None
        elif age > self.ttl:
            self._refresh_background()
        if symbols is None:
            return data, age
        want = [s for s in symbols if s]
        return {s: data[s] for s in want if s in data}, age


class BinanceFuturesPriceStream:
    def __init__(self, stale_after_seconds: int = 120):
        self.stale_after_seconds = int(stale_after_seconds)
        self.exchange = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        self._prices = {}
        self._updated_at = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="price-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def wait_until_ready(self, timeout: float = 8) -> bool:
        return self._ready.wait(timeout=timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                tickers = self.exchange.fetch_tickers()
                now = time.time()
                fresh_prices = {}
                fresh_updated_at = {}
                for symbol, payload in tickers.items():
                    last = payload.get("last")
                    quote_volume = payload.get("quoteVolume") or 0
                    if last is None:
                        continue
                    fresh_prices[symbol] = {
                        "symbol": symbol,
                        "last": last,
                        "quoteVolume": quote_volume,
                    }
                    fresh_updated_at[symbol] = now
                with self._lock:
                    self._prices = fresh_prices
                    self._updated_at = fresh_updated_at
                self._ready.set()
            except Exception as exc:  # noqa: BLE001
                print(f"Price snapshot refresh failed: {exc}")
            self._stop_event.wait(15)

    def _is_fresh(self, updated_at: float | None) -> bool:
        if not updated_at:
            return False
        return (time.time() - updated_at) <= self.stale_after_seconds

    def get_price(self, symbol: str):
        with self._lock:
            payload = self._prices.get(symbol)
            updated_at = self._updated_at.get(symbol)
        if not payload or not self._is_fresh(updated_at):
            return None
        return payload.get("last")

    def get_prices(self, symbols: list[str]) -> dict:
        with self._lock:
            return {
                symbol: self._prices[symbol]
                for symbol in symbols
                if symbol in self._prices and self._is_fresh(self._updated_at.get(symbol))
            }

    def get_top_symbols_by_quote_volume(self, limit: int, quote_asset: str) -> list[str]:
        with self._lock:
            candidates = [
                payload
                for symbol, payload in self._prices.items()
                if symbol.endswith(f"/{quote_asset}:{quote_asset}") and "_" not in symbol
                and self._is_fresh(self._updated_at.get(symbol))
            ]
        candidates.sort(key=lambda item: item.get("quoteVolume") or 0, reverse=True)
        return [item["symbol"] for item in candidates[:limit]]


class BinanceFuturesKlineCache:
    def __init__(self, cache_file: str, max_candles: int = 400):
        self.cache_file = cache_file
        self.max_candles = int(max_candles)
        self._candles = {}
        self._lock = threading.Lock()
        self._load_cache()

    def start(self) -> None:
        return

    def _load_cache(self) -> None:
        if not os.path.exists(self.cache_file):
            self._candles = {}
            return
        try:
            with open(self.cache_file, "r") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                self._candles = payload
            else:
                self._candles = {}
        except Exception as exc:  # noqa: BLE001
            print(f"Unable to load candle cache: {exc}")
            self._candles = {}

    def save_cache(self) -> None:
        try:
            temp_path = self.cache_file + ".tmp"
            with open(temp_path, "w") as f:
                json.dump(self._candles, f)
            os.replace(temp_path, self.cache_file)
        except Exception as exc:  # noqa: BLE001
            print(f"Unable to save candle cache: {exc}")

    def update_subscriptions(self, symbols: list[str], timeframes: list[str]) -> None:
        with self._lock:
            for timeframe in timeframes:
                bucket = self._candles.setdefault(timeframe, {})
                for symbol in symbols:
                    bucket.setdefault(symbol, [])

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int):
        with self._lock:
            candles = list(self._candles.get(timeframe, {}).get(symbol, []))
        if not candles:
            return []
        return candles[-limit:]

    def seed_history(self, symbol: str, timeframe: str, candles: list[list]) -> None:
        normalized = [list(item) for item in candles if isinstance(item, (list, tuple)) and len(item) >= 6]
        if not normalized:
            return
        with self._lock:
            bucket = self._candles.setdefault(timeframe, {})
            bucket[symbol] = normalized[-self.max_candles:]
