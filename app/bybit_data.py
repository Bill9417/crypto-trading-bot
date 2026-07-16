"""
Public Bybit market data for the Chinese alert text (no API keys needed).

The Chinese version of every outward-facing Telegram message quotes BYBIT
prices and links (the user promotes Bybit). This module is the single source
for that: a public ccxt.bybit client with two small caches —

    • linear-perp universe (BASE → market symbol), refreshed every 6h
    • bulk tickers, refreshed every 90s (one API call covers a whole digest)

Everything here is failure-safe: any Bybit API hiccup degrades to None /
empty string and the alert falls back to the Binance price it already has.
An alert must never die (or block a sweep) because Bybit was slow — network
work happens at most once per TTL and is guarded by a lock.
"""
import threading
import time

_lock = threading.Lock()
_ex = None
_bases: dict = {}        # "BTC" -> "BTC/USDT:USDT"
_bases_ts = 0.0
_tickers: dict = {}      # market symbol -> ticker
_tickers_ts = 0.0

BASES_TTL = 6 * 3600
TICKERS_TTL = 90


def _exchange():
    global _ex
    if _ex is None:
        import ccxt
        _ex = ccxt.bybit({"enableRateLimit": True,
                          "options": {"defaultType": "swap"}})
    return _ex


def _refresh_bases() -> None:
    global _bases, _bases_ts
    if _bases and time.time() - _bases_ts < BASES_TTL:
        return
    markets = _exchange().load_markets()
    _bases = {str(m.get("base")).upper(): s for s, m in markets.items()
              if m.get("swap") and m.get("quote") == "USDT"
              and m.get("active", True) and s.endswith(":USDT")}
    _bases_ts = time.time()


def _refresh_tickers() -> None:
    global _tickers, _tickers_ts
    if _tickers and time.time() - _tickers_ts < TICKERS_TTL:
        return
    _tickers = _exchange().fetch_tickers() or {}
    _tickers_ts = time.time()


def available(base: str) -> bool:
    """Is this base tradable as a Bybit USDT linear perp?"""
    try:
        with _lock:
            _refresh_bases()
        return str(base).upper() in _bases
    except Exception:  # noqa: BLE001 — Bybit down ≠ alert down
        return False


def trade_url(base: str):
    """Bybit trade page for the perp, or None when not listed there."""
    if not available(base):
        return None
    return f"https://www.bybit.com/trade/usdt/{str(base).upper()}USDT"


def last_price(base: str):
    """Latest Bybit perp price for the base, or None (not listed / API blip)."""
    try:
        with _lock:
            _refresh_bases()
            sym = _bases.get(str(base).upper())
            if not sym:
                return None
            _refresh_tickers()
        px = (_tickers.get(sym) or {}).get("last")
        return float(px) if px else None
    except Exception:  # noqa: BLE001
        return None


def _fmt(p) -> str:
    try:
        return f"{float(p):,.6g}"
    except (TypeError, ValueError):
        return str(p)


def price_line(base: str, fallback_price=None) -> str:
    """One Chinese line quoting the Bybit price, with an honest fallback:
    'Bybit 現價 118,500' / 'Bybit 未上架 · 參考價 0.0097 (Binance)'."""
    px = last_price(base)
    if px is not None:
        return f"Bybit 現價 {_fmt(px)}"
    if fallback_price:
        return f"Bybit 未上架 · 參考價 {_fmt(fallback_price)} (Binance)"
    return ""
