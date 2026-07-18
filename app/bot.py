import atexit
import traceback
from datetime import datetime, timezone, timedelta
import fcntl
import signal
import time
import json
import os

import pandas as pd
import numpy as np
from apscheduler.schedulers.blocking import BlockingScheduler

from config import (
    CHECK_INTERVAL_MINUTES,
    RSI_BLOCK_LONG_ABOVE,
    RSI_BLOCK_SHORT_BELOW,
    SMC_IDEAL_ZONE_BONUS,
    MIN_LIGHTS_SHORT,
    ALLOW_BEAR_TREND_SHORTS_IN_DISCOUNT,
    ENABLE_SHORTS,
    ENABLE_BTC_REGIME_FILTER,
    BTC_REGIME_SYMBOL,
    BTC_REGIME_TIMEFRAME,
    BTC_REGIME_EMA,
    BTC_REGIME_SLOPE_LOOKBACK,
    ENABLE_VWAP_FILTER,
    VWAP_PERIOD,
    ENTRY_OFFSET_PCT,
    FIXED_SL_PCT,
    FIXED_TP_PCT,
    MIN_LIGHTS_FOR_ENTRY,
    MIN_LIGHTS_FOR_RECORD,
    MIN_BASE_LIGHTS,
    MAX_SMC_BONUS_LIGHTS,
    STRATEGY_SCORE_LIGHT_THRESHOLD,
    BREAKOUT_VOLUME_MULTIPLIER,
    MIN_CANDLES_FOR_SIGNAL,
    CANCEL_QUEUE_IF_TARGET_HIT,
    QUOTE_ASSET,
    OHLCV_CACHE_LIMIT,
    REST_API_MAX_RETRIES,
    REST_API_MIN_INTERVAL_SEC,
    RSI_PERIOD,
    RSI_UPPER_THRESHOLD,
    RSI_LOWER_THRESHOLD,
    RSI_ALERT_ENABLED,
    RSI_ALERT_HIGH,
    RSI_ALERT_LOW,
    RSI_ALERT_TIMEFRAME,
    RSI_ALERT_ALWAYS,
    STOCH_RSI_RSI_PERIOD,
    STOCH_RSI_STOCH_PERIOD,
    STOCH_RSI_K_SMOOTH,
    STOCH_RSI_D_SMOOTH,
    STOCH_RSI_OVERSOLD,
    STOCH_RSI_OVERBOUGHT,
    SYMBOLS,
    TIMEFRAMES,
    TOP_SYMBOL_LIMIT,
    SCAN_SYMBOL_LIMIT,
    WS_READY_TIMEOUT_SECONDS,
    WS_STALE_AFTER_SECONDS,
    ENABLE_4H_TREND_FILTER,
    VOLUME_GATE_MULTIPLIER,
    REQUIRE_RSI_50_CROSS,
    ATR_SL_MULTIPLIER,
    ATR_TP1_MULTIPLIER,
    ATR_TP_MULTIPLIER,
    LEVERAGE,
    MAX_SL_PCT,
    ATR_OFFSET_MULTIPLIER_MIN,
    ATR_OFFSET_MULTIPLIER_MAX,
    POSITION_SIZE_4_LIGHTS,
    POSITION_SIZE_5_LIGHTS,
    POSITION_SIZE_6_LIGHTS,
    POSITION_SIZE_COUNTER_TREND,
    DAILY_MAX_LOSSES,
    DAILY_MAX_DRAWDOWN_PCT,
    MAX_CONCURRENT_POSITIONS,
    ENABLE_ADX_FILTER,
    ADX_PERIOD,
    ADX_MIN_THRESHOLD,
    ADX_REQUIRE_RISING,
    ENABLE_FUNDING_FILTER,
    FUNDING_MAX_LONG,
    FUNDING_MIN_SHORT,
    ENABLE_DIRECTION_CAP,
    MAX_SAME_DIRECTION,
    EXCLUDE_TRADFI_PERPS,
)
from indicators import (
    check_tsi_signal,
    check_macd_signal,
    check_volume_gate,
    check_rsi_cross_after_extreme,
    check_hidden_divergence,
    check_stoch_rsi_signal,
    calculate_ema,
    calculate_vwap,
    calculate_volume_profile,
    calculate_order_flow,
    adx_components,
)
from market_data import (
    BinanceFuturesKlineCache,
    BinanceFuturesPriceStream,
    RateLimitCooldownError,
    SafeBinanceClient,
    is_tradfi_market,
    timeframe_to_seconds,
)
from smc import analyze_smc
from telegram_utils import send_message
import executor
import s1_bybit_mirror
import tg_format

# Import database models from app
try:
    from app import app, db, SignalRecord
except ImportError:
    app, db, SignalRecord = None, None, None

# Shared data files for Web UI and candle cache
DATA_FILE = os.path.join(os.path.dirname(__file__), "scan_results.json")
CANDLE_CACHE_FILE = os.path.join(os.path.dirname(__file__), "ohlcv_cache.json")
BOT_LOCK_FILE = os.path.join(os.path.dirname(__file__), "bot.lock")
QUEUE_CLEAR_REQUEST_FILE = os.path.join(os.path.dirname(__file__), "clear_queue.request")
CIRCUIT_STATE_FILE = os.path.join(os.path.dirname(__file__), "circuit_state.json")

rest_client = SafeBinanceClient(
    min_rest_interval=REST_API_MIN_INTERVAL_SEC,
    max_retries=REST_API_MAX_RETRIES,
)
exchange = rest_client.exchange
price_stream = BinanceFuturesPriceStream(stale_after_seconds=WS_STALE_AFTER_SECONDS)
kline_cache = BinanceFuturesKlineCache(CANDLE_CACHE_FILE, max_candles=OHLCV_CACHE_LIMIT)

# To prevent spam: track the last alert state for each symbol+timeframe
# Format: {(symbol, timeframe): last_trigger_type} 
# trigger_type: 'overbought', 'oversold', or None
last_alerts = {}
queued_signals = {}
bot_lock_handle = None
_daily_loss_state: dict = {"date": None, "count": 0, "drawdown_pct": 0.0}
_circuit_state_loaded = False


def dir_tag(direction: str) -> str:
    """Coloured direction label for Telegram alerts."""
    return "🟢 LONG" if str(direction).upper() == "LONG" else "🔴 SHORT"


def notify(message: str, parse_mode: str = None, *, channel: str = "alerts") -> None:
    """Fire-and-forget Telegram alert; never let a notification break the bot."""
    try:
        send_message(message, parse_mode=parse_mode, channel=channel)
    except Exception as exc:  # noqa: BLE001
        print(f"Telegram notify failed: {exc}")


def get_now_taiwan():
    # Taiwan is UTC+8
    return datetime.now(timezone(timedelta(hours=8)))


def _load_circuit_state() -> None:
    """Load the daily loss state from disk once, so the circuit breaker
    survives a bot restart instead of silently resetting to zero mid-day."""
    global _circuit_state_loaded
    if _circuit_state_loaded:
        return
    _circuit_state_loaded = True
    try:
        if os.path.exists(CIRCUIT_STATE_FILE):
            with open(CIRCUIT_STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            _daily_loss_state["date"] = saved.get("date")
            _daily_loss_state["count"] = int(saved.get("count", 0))
            _daily_loss_state["drawdown_pct"] = float(saved.get("drawdown_pct", 0.0))
            print(f"[CIRCUIT BREAKER] Restored daily state: {_daily_loss_state}")
    except Exception as exc:  # noqa: BLE001 — corrupt file must never crash the bot
        print(f"[CIRCUIT BREAKER] Could not load state ({exc}); starting fresh.")


def _save_circuit_state() -> None:
    """Atomically persist the daily loss state to disk."""
    try:
        tmp = CIRCUIT_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_daily_loss_state, f)
        os.replace(tmp, CIRCUIT_STATE_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"[CIRCUIT BREAKER] Could not save state: {exc}")


def _roll_circuit_day(today: str) -> bool:
    """Reset the counters when the Taiwan day changes. Returns True if rolled."""
    s = _daily_loss_state
    if s["date"] != today:
        s["date"] = today
        s["count"] = 0
        s["drawdown_pct"] = 0.0
        _save_circuit_state()
        return True
    return False


def check_circuit_breaker() -> bool:
    """Returns True when the daily loss limit or drawdown cap is hit — halt new trades."""
    _load_circuit_state()
    today = get_now_taiwan().date().isoformat()
    s = _daily_loss_state
    if _roll_circuit_day(today):
        return False
    if s["count"] >= DAILY_MAX_LOSSES:
        print(f"[CIRCUIT BREAKER] {s['count']} losses today — no new trades.")
        return True
    if s["drawdown_pct"] <= DAILY_MAX_DRAWDOWN_PCT:
        print(f"[CIRCUIT BREAKER] Daily drawdown {s['drawdown_pct']:.2f}% — no new trades.")
        return True
    return False


def record_daily_loss(pnl_pct: float) -> None:
    """Record a closed losing trade into the daily circuit breaker state."""
    _load_circuit_state()
    today = get_now_taiwan().date().isoformat()
    s = _daily_loss_state
    _roll_circuit_day(today)
    s["count"] += 1
    s["drawdown_pct"] += pnl_pct  # pnl_pct is negative for losses
    _save_circuit_state()


def format_taiwan_time_from_ms(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return "-"
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def release_bot_lock() -> None:
    global bot_lock_handle
    if bot_lock_handle is None:
        return
    try:
        fcntl.flock(bot_lock_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        bot_lock_handle.close()
    except OSError:
        pass
    bot_lock_handle = None


def acquire_bot_lock() -> None:
    global bot_lock_handle
    if bot_lock_handle is not None:
        return

    handle = open(BOT_LOCK_FILE, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        print("Another bot.py process is already running. Exiting to prevent duplicate scans and Telegram alerts.")
        raise SystemExit(0) from None

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    bot_lock_handle = handle
    atexit.register(release_bot_lock)


def load_last_scan_symbols() -> list[str]:
    try:
        if not os.path.exists(DATA_FILE):
            return []
        with open(DATA_FILE, "r") as f:
            payload = json.load(f)
        symbols = []
        seen = set()
        saved_symbols = payload.get("scanned_symbols_list", [])
        if isinstance(saved_symbols, list):
            for symbol in saved_symbols:
                if symbol and symbol not in seen:
                    symbols.append(symbol)
                    seen.add(symbol)
                if len(symbols) >= SCAN_SYMBOL_LIMIT:
                    break
        if len(symbols) >= SCAN_SYMBOL_LIMIT:
            return symbols[:SCAN_SYMBOL_LIMIT]
        for signal in payload.get("signals", []):
            symbol = signal.get("symbol")
            if symbol and symbol not in seen:
                symbols.append(symbol)
                seen.add(symbol)
            if len(symbols) >= SCAN_SYMBOL_LIMIT:
                break
        scan_count = int(payload.get("scanned_symbols", 0) or 0)
        if scan_count < TOP_SYMBOL_LIMIT and len(symbols) < TOP_SYMBOL_LIMIT:
            return []
        return symbols[:SCAN_SYMBOL_LIMIT]
    except Exception as exc:
        print(f"Unable to load symbols from previous scan: {exc}")
        return []

def _is_futures_usdt_symbol(symbol: str) -> bool:
    if f":{QUOTE_ASSET}" not in symbol and f"/{QUOTE_ASSET}" not in symbol:
        return False
    # Skip quarterly futures (they have dates like BTCUSDT_240628)
    if "_" in symbol:
        return False
    return True

# Global cache for symbols to reduce API calls
SYMBOL_CACHE = {"timestamp": 0, "symbols": []}
CACHE_DURATION_HOURS = 4

def get_target_symbols() -> list[str]:
    if SYMBOLS:
        return SYMBOLS
    
    # Use cache if it's fresh (e.g., 4 hours)
    now = time.time()
    if (
        len(SYMBOL_CACHE["symbols"]) >= SCAN_SYMBOL_LIMIT
        and (now - SYMBOL_CACHE["timestamp"]) < (CACHE_DURATION_HOURS * 3600)
    ):
        print(f"Using cached symbol list ({len(SYMBOL_CACHE['symbols'])} pairs)...")
        return SYMBOL_CACHE["symbols"][:SCAN_SYMBOL_LIMIT]

    if not price_stream.wait_until_ready(timeout=max(WS_READY_TIMEOUT_SECONDS, 15)):
        print("WebSocket ticker snapshot is still warming up...")

    top_symbols = price_stream.get_top_symbols_by_quote_volume(SCAN_SYMBOL_LIMIT, QUOTE_ASSET)
    if len(top_symbols) >= SCAN_SYMBOL_LIMIT:
        print(f"Using WebSocket volume snapshot ({len(top_symbols)} pairs)...")
    else:
        if top_symbols:
            print(
                f"WebSocket symbol snapshot incomplete ({len(top_symbols)}/{SCAN_SYMBOL_LIMIT}). "
                "Falling back to REST tickers for full ranking..."
            )
        else:
            print("WebSocket snapshot not ready. Falling back to REST tickers for symbol ranking...")
        try:
            tickers = rest_client.call("fetch_tickers")
            symbol_volumes = []
            for symbol, ticker in tickers.items():
                if _is_futures_usdt_symbol(symbol):
                    volume = ticker.get("quoteVolume", 0) or 0
                    symbol_volumes.append((symbol, volume))
            symbol_volumes.sort(key=lambda x: x[1], reverse=True)
            top_symbols = [s[0] for s in symbol_volumes[:SCAN_SYMBOL_LIMIT]]
        except RateLimitCooldownError as exc:
            print(f"REST symbol refresh skipped: {exc}")
            top_symbols = load_last_scan_symbols()
            if top_symbols:
                print(f"Using {len(top_symbols)} symbols from last scan cache while REST cools down.")
            else:
                raise
    if len(top_symbols) < TOP_SYMBOL_LIMIT:
        fallback_symbols = load_last_scan_symbols()
        if len(fallback_symbols) >= TOP_SYMBOL_LIMIT:
            print(f"Recovered full symbol universe from last scan cache ({len(fallback_symbols)} pairs).")
            top_symbols = fallback_symbols

    # Drop Binance TradFi stock perps (AAPL, QQQ, …) — trading them needs a
    # separately signed agreement this account doesn't have, so any order there
    # is a guaranteed -4411 rejection. Ticker payloads don't carry the market
    # type, so consult whichever ccxt client has already loaded markets.
    if EXCLUDE_TRADFI_PERPS:
        markets = (getattr(exchange, "markets", None)
                   or getattr(price_stream.exchange, "markets", None) or {})
        if markets:
            top_symbols = [s for s in top_symbols if not is_tradfi_market(markets.get(s))]

    # Update cache (keep the full scan universe; tradeable subset is the top slice).
    SYMBOL_CACHE["symbols"] = top_symbols[:SCAN_SYMBOL_LIMIT]
    SYMBOL_CACHE["timestamp"] = time.time()

    return top_symbols[:SCAN_SYMBOL_LIMIT]


def get_live_tickers(symbols: list[str]) -> dict:
    """Prefer WebSocket prices and only use REST as a small fallback."""
    tickers = price_stream.get_prices(symbols)
    missing = [symbol for symbol in symbols if symbol not in tickers]
    if not missing:
        return tickers

    try:
        rest_tickers = rest_client.call("fetch_tickers", missing)
    except RateLimitCooldownError as exc:
        print(f"Skipping REST live-price fallback: {exc}")
        return tickers
    except Exception as exc:
        if "same type" in str(exc).lower():
            try:
                rest_tickers = rest_client.call("fetch_tickers")
            except Exception as snapshot_exc:  # noqa: BLE001
                print(f"Live-price snapshot fallback failed: {snapshot_exc}")
                return tickers
        else:
            print(f"Live-price REST fallback failed: {exc}")
            return tickers

    for symbol in missing:
        payload = rest_tickers.get(symbol)
        if payload and payload.get("last") is not None:
            tickers[symbol] = payload
    return tickers


def normalize_rest_ohlcv_to_closed(ohlcv: list[list], timeframe: str) -> list[list]:
    if not ohlcv:
        return []
    now_ms = int(time.time() * 1000)
    candle_seconds = timeframe_to_seconds(timeframe)
    closed = list(ohlcv)
    if closed and (closed[-1][0] + candle_seconds * 1000) > now_ms:
        closed = closed[:-1]
    return closed


def is_ohlcv_fresh(ohlcv: list[list], timeframe: str, *, max_age_factor: float = 2.0) -> bool:
    if not ohlcv:
        return False
    try:
        last_open_ms = int(ohlcv[-1][0])
    except (TypeError, ValueError, IndexError):
        return False
    candle_seconds = timeframe_to_seconds(timeframe)
    last_close_ms = last_open_ms + candle_seconds * 1000
    age_seconds = max(0.0, (time.time() * 1000 - last_close_ms) / 1000.0)
    return age_seconds <= (candle_seconds * max_age_factor)


def get_symbol_ohlcv(symbol: str, timeframe: str, limit: int = 260) -> tuple[list[list], str]:
    cached = kline_cache.get_ohlcv(symbol, timeframe, limit)
    cached_is_fresh = is_ohlcv_fresh(cached, timeframe)
    if len(cached) >= limit and cached_is_fresh:
        return cached, "cache"

    if rest_client.cooldown_remaining() > 0:
        if cached and cached_is_fresh:
            return cached, "cache-stale"
        return [], "cooldown-no-fresh-cache"

    try:
        ohlcv = rest_client.call("fetch_ohlcv", symbol, timeframe=timeframe, limit=limit + 5)
        closed = normalize_rest_ohlcv_to_closed(ohlcv, timeframe)
        if closed:
            kline_cache.seed_history(symbol, timeframe, closed)
            return closed[-limit:], "rest"
    except RateLimitCooldownError as exc:
        if cached and cached_is_fresh:
            print(f"Using cached candles for {symbol} {timeframe} during REST cooldown: {exc}")
            return cached, "cache-stale"
        raise
    except Exception as exc:
        if cached and cached_is_fresh:
            print(f"Using cached candles for {symbol} {timeframe} after REST failure: {exc}")
            return cached, "cache-stale"
        raise

    if cached and cached_is_fresh:
        return cached, "cache"
    return [], "stale-cache-no-refresh"

def calculate_rsi_full(prices, period=14):
    """Calculate full RSI array (returns np.array of RSI values)
    :param prices: list of prices
    :param period: RSI period
    :return: np.array of RSI values, or None if insufficient data
    """
    if len(prices) <= period:
        return None
    prices = np.array(prices, dtype=float)
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    rsi_values = []
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_val = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            rsi_val = 100 - (100 / (1 + rs))
        rsi_values.append(rsi_val)
    return np.array(rsi_values)

def calculate_rsi(prices, period=14):
    """Calculate last RSI value only (calls calculate_rsi_full and returns last)"""
    full_rsi = calculate_rsi_full(prices, period)
    if full_rsi is None or len(full_rsi) ==0:
        return None
    return full_rsi[-1]

def record_signal(symbol, direction, entry, tp1, tp2, sl, lights_count,
                  rsi_value=None, trend_aligned=None, smc_zone=None,
                  manage="bracket", trail_dist=None, strategy=None):
    """Save high-conviction signals to the database for performance tracking.

    `manage` is 'bracket' (S1/S2) or 'trailing' (S3/S4). For trailing trades the
    Chandelier state (peak/trough/cur_stop/last_bar_ts) is seeded here so the
    monitor can ratchet the live stop each closed 1h candle. `strategy` is the
    registry key that produced the trade (defaults to the live strategy) so the
    Performance page can scope its stats to one strategy."""
    if strategy is None:
        try:
            strategy = resolve_live_strategy()[0]
        except Exception:  # noqa: BLE001
            strategy = "default"
    if not all([app, db, SignalRecord]):
        return False, "database unavailable"

    with app.app_context():
        try:
            now = get_now_taiwan().replace(tzinfo=None)

            # Check if we already recorded a similar pending signal recently to avoid duplicates
            existing = SignalRecord.query.filter_by(
                symbol=symbol, 
                direction=direction, 
                status='PENDING'
            ).order_by(SignalRecord.timestamp.desc()).first()
            
            # If a pending signal for this symbol exists and was created in the last 2 hours, skip
            if existing and existing.timestamp and (now - existing.timestamp).total_seconds() < 7200:
                return False, "existing pending trade in last 2 hours"

            last_sl = SignalRecord.query.filter_by(
                symbol=symbol,
                direction=direction,
                status='SL',
            ).order_by(SignalRecord.exit_timestamp.desc()).first()
            if last_sl and last_sl.exit_timestamp and (now - last_sl.exit_timestamp).total_seconds() < 10800:
                return False, "same-direction cooldown after SL"

            is_trailing = manage == "trailing"
            new_record = SignalRecord(
                symbol=symbol,
                direction=direction,
                entry_price=entry,
                tp1_price=tp1,
                tp2_price=tp2,
                sl_price=sl,
                lights_count=lights_count,
                status='PENDING',
                timestamp=now,
                rsi_value=rsi_value,
                trend_aligned=trend_aligned,
                smc_zone=smc_zone,
                strategy=strategy,
                manage=manage,
                trail_dist=trail_dist,
                peak=entry if is_trailing else None,
                trough=entry if is_trailing else None,
                cur_stop=sl if is_trailing else None,
                # manage candles that close AFTER the fill; the initial SL protects
                # the position until the first 1h candle closes and the stop ratchets.
                last_bar_ts=int(time.time() * 1000) if is_trailing else None,
            )
            db.session.add(new_record)
            db.session.commit()
            print(f"Recorded signal for {symbol} ({lights_count} lights)")
            return True, "recorded"
        except Exception as e:
            db.session.rollback()
            print(f"Error recording signal: {e}")
            return False, f"recording error: {e}"


def _record_trade_status(record) -> str | None:
    status = (getattr(record, "status", "") or "").upper()
    if status == "PENDING":
        return "active"
    if status in {"TP", "TP1", "TP2"}:
        return "tp"
    if status == "SL":
        return "sl"
    return None


def _record_trade_reason(record) -> str:
    status = _record_trade_status(record)
    if status == "active":
        return "trade active"
    if status == "tp":
        return "TP target hit"
    if status == "sl":
        return "SL hit"
    return "trade recorded"


def build_queue_status_reason() -> str:
    return "queued from latest scan; waiting for price to reach updated entry"


def build_rejected_status_reason(record_reasons: list[str]) -> str:
    if not record_reasons:
        return "rejected on latest scan"
    return f"rejected on latest scan: {'; '.join(record_reasons)}"


def load_recent_record_states(max_closed_hours: int = 24) -> dict[tuple[str, str], dict]:
    if not all([app, db, SignalRecord]):
        return {}

    state_map: dict[tuple[str, str], dict] = {}
    now = get_now_taiwan().replace(tzinfo=None)
    cutoff = now - timedelta(hours=max_closed_hours)

    with app.app_context():
        try:
            records = SignalRecord.query.order_by(SignalRecord.timestamp.desc()).all()
            for record in records:
                trade_status = _record_trade_status(record)
                if not trade_status:
                    continue

                if trade_status != "active":
                    closed_at = record.exit_timestamp or record.timestamp
                    if not closed_at or closed_at < cutoff:
                        continue

                key = (record.symbol, record.direction)
                if key in state_map:
                    continue

                status_upper = (getattr(record, "status", "") or "").upper()
                if status_upper == "TP1_PARTIAL":
                    tp_price = record.tp2_price
                    sl_price = record.entry_price
                else:
                    tp_price = record.tp1_price
                    sl_price = record.sl_price

                state_map[key] = {
                    "trade_status": trade_status,
                    "record_status_reason": _record_trade_reason(record),
                    "entry": format_price(record.entry_price) if record.entry_price is not None else None,
                    "tp": format_price(tp_price) if tp_price is not None else None,
                    "tp2": format_price(record.tp2_price) if record.tp2_price is not None else None,
                    "sl": format_price(sl_price) if sl_price is not None else None,
                    "exit_price": format_price(record.exit_price) if record.exit_price is not None else None,
                }
        except Exception as exc:  # noqa: BLE001
            print(f"Unable to load recent record states: {exc}")

    return state_map

def update_pending_signals():
    """Activate queued entries first, then check live trades for TP or SL."""
    if not all([app, db, SignalRecord]):
        return

    apply_queue_clear_request()
    print("Checking pending signals for outcomes...")
    activate_queued_signals()
    with app.app_context():
        try:
            # Get both PENDING and TP1_PARTIAL signals
            active_signals = SignalRecord.query.filter(
                (SignalRecord.status == 'PENDING') | (SignalRecord.status == 'TP1_PARTIAL')
            ).all()
            if not active_signals:
                return

            # Get current prices and 1m OHLCV for all symbols in active signals
            symbols = list(set([s.symbol for s in active_signals]))
            tickers = get_live_tickers(symbols)

            for signal in active_signals:
                if signal.symbol not in tickers:
                    continue

                current_price = tickers[signal.symbol]['last']
                direction = signal.direction
                entry = signal.entry_price
                tp1 = signal.tp1_price
                tp2 = signal.tp2_price
                sl = signal.sl_price

                # Check 1m OHLCV to see if TP or SL was hit in between checks
                # First get 1m OHLCV (cache or REST)
                ohlcv_1m = []
                try:
                    ohlcv_1m, _ = get_symbol_ohlcv(signal.symbol, "1m", limit=2)
                except Exception:
                    pass
                tp1_hit_in_candle = False
                tp2_hit_in_candle = False
                sl_hit_in_candle = False
                if len(ohlcv_1m) >= 1:
                    last_candle = ohlcv_1m[-1]  # [timestamp, open, high, low, close, volume]
                    if direction == 'LONG':
                        high = last_candle[2]
                        low = last_candle[3]
                        if high >= tp1:
                            tp1_hit_in_candle = True
                        if high >= tp2:
                            tp2_hit_in_candle = True
                        if low <= sl:
                            sl_hit_in_candle = True
                    else:  # SHORT
                        high = last_candle[2]
                        low = last_candle[3]
                        if low <= tp1:
                            tp1_hit_in_candle = True
                        if low <= tp2:
                            tp2_hit_in_candle = True
                        if high >= sl:
                            sl_hit_in_candle = True

                # Handle PENDING signals first (check TP1)
                if signal.status == 'PENDING' and not signal.partial_tp1:
                    is_tp1 = (direction == 'LONG' and (current_price >= tp1 or tp1_hit_in_candle)) or (direction == 'SHORT' and (current_price <= tp1 or tp1_hit_in_candle))
                    is_sl = (direction == 'LONG' and (current_price <= sl or sl_hit_in_candle)) or (direction == 'SHORT' and (current_price >= sl or sl_hit_in_candle))
                    # Conservative tie-break: if the SAME 1m candle wicked BOTH the
                    # stop and the target we cannot know which printed first, so we
                    # resolve it as the stop (worst case). This keeps the win rate
                    # honest instead of optimistically counting a TP that may not
                    # have happened first.
                    if is_tp1 and is_sl:
                        is_tp1 = False
                    if is_tp1:
                        # Partial take profit: 50% at TP1, now trail remaining 50%
                        signal.status = 'TP1_PARTIAL'
                        signal.partial_tp1 = True
                        signal.runner_peak = tp1  # best price so far (TP1 just printed)
                        tp1_pct = ((tp1 - entry) / entry * 100) if direction == 'LONG' else ((entry - tp1) / entry * 100)
                        signal.pnl_pct = tp1_pct * 0.5
                        # Mirror the simulation on the live account: now that the
                        # first 50% is banked, move the exchange stop to breakeven.
                        try:
                            executor.move_stop_to_breakeven(signal.symbol, direction, entry)
                        except Exception as exc:  # noqa: BLE001 — never break tracking
                            print(f"[executor] breakeven hook error for {signal.symbol}: {exc}")
                        update_signal_state_in_ui(
                            signal.symbol,
                            direction,
                            trade_status="tp1_partial",
                            reason=f"TP1 hit at +{tp1_pct:.1f}% (50% closed), trailing rest to TP2",
                            current_price=current_price,
                            entry=entry,
                            tp=tp2,
                            sl=entry,  # Trailing stop at entry price
                            exit_price=tp1,
                        )
                        notify_s1_follow(s1_follow_exit(
                            "tp1", signal.symbol, direction, tp1_pct,
                            note="先平一半 · 停損移到進場價 · 剩餘續抱到 TP2",
                        ))
                        s1_bybit_mirror.mirror_tp1(signal.symbol, direction, entry)
                        continue

                    # SL (also reached here when TP1+SL hit the same candle → SL wins)
                    if is_sl:
                        signal.status = 'SL'
                        signal.exit_price = sl
                        signal.exit_timestamp = get_now_taiwan().replace(tzinfo=None)
                        signal.pnl_pct = ((sl - entry) / entry * 100) if direction == 'LONG' else ((entry - sl) / entry * 100)
                        record_daily_loss(signal.pnl_pct)
                        executor.on_trade_closed(signal.symbol, direction)
                        update_signal_state_in_ui(
                            signal.symbol,
                            direction,
                            trade_status="sl",
                            reason="SL hit",
                            current_price=current_price,
                            entry=entry,
                            tp=tp1,
                            sl=sl,
                            exit_price=sl,
                        )
                        notify_s1_follow(s1_follow_exit(
                            "sl", signal.symbol, direction, signal.pnl_pct,
                            note="觸及停損 · 本單結束",
                        ))
                        s1_bybit_mirror.mirror_close(signal.symbol, "sl")
                        continue

                # Handle TP1_PARTIAL signals — the runner (50%) is managed EXACTLY
                # like the live Binance bracket: a hard TP2 at 2R (reduce-only TP
                # order) and a breakeven stop (moved to entry after TP1). Whichever
                # the candle reaches first closes the runner. This guarantees the
                # dashboard simulation == the live exchange result.
                elif signal.status == 'TP1_PARTIAL':
                    tp1_pct = ((tp1 - entry) / entry * 100) if direction == 'LONG' else ((entry - tp1) / entry * 100)

                    is_tp2 = (direction == 'LONG' and (current_price >= tp2 or tp2_hit_in_candle)) or \
                             (direction == 'SHORT' and (current_price <= tp2 or tp2_hit_in_candle))
                    be_hit_in_candle = False
                    if len(ohlcv_1m) >= 1:
                        if direction == 'LONG' and low <= entry:
                            be_hit_in_candle = True
                        elif direction == 'SHORT' and high >= entry:
                            be_hit_in_candle = True
                    is_breakeven = (direction == 'LONG' and (current_price <= entry or be_hit_in_candle)) or \
                                   (direction == 'SHORT' and (current_price >= entry or be_hit_in_candle))
                    # Conservative tie-break: if one candle hit BOTH TP2 and the
                    # breakeven stop, resolve as breakeven (the worse outcome).
                    if is_tp2 and is_breakeven:
                        is_tp2 = False

                    if is_tp2:
                        tp2_pct = ((tp2 - entry) / entry * 100) if direction == 'LONG' else ((entry - tp2) / entry * 100)
                        signal.status = 'TP'
                        signal.exit_price = tp2
                        signal.exit_timestamp = get_now_taiwan().replace(tzinfo=None)
                        signal.pnl_pct = tp1_pct * 0.5 + tp2_pct * 0.5
                        executor.on_trade_closed(signal.symbol, direction)
                        update_signal_state_in_ui(
                            signal.symbol, direction,
                            trade_status="tp",
                            reason=f"TP2 hit at +{tp2_pct:.1f}% — full position closed (+{signal.pnl_pct:.1f}% total)",
                            current_price=current_price, entry=entry, tp=tp2, sl=entry, exit_price=tp2,
                        )
                        notify_s1_follow(s1_follow_exit(
                            "tp2", signal.symbol, direction, signal.pnl_pct,
                            note=f"全部平倉 · 續抱段 +{tp2_pct:.1f}%",
                        ))
                        s1_bybit_mirror.mirror_close(signal.symbol, "tp2")
                        continue

                    if is_breakeven:
                        # Runner stopped at breakeven → only the TP1 half is booked.
                        signal.status = 'TP'
                        signal.exit_price = entry
                        signal.exit_timestamp = get_now_taiwan().replace(tzinfo=None)
                        signal.pnl_pct = tp1_pct * 0.5
                        executor.on_trade_closed(signal.symbol, direction)
                        update_signal_state_in_ui(
                            signal.symbol, direction,
                            trade_status="tp",
                            reason=f"Runner stopped at breakeven — TP1 profit locked (+{signal.pnl_pct:.1f}% total)",
                            current_price=current_price, entry=entry, tp=tp2, sl=entry, exit_price=entry,
                        )
                        notify_s1_follow(s1_follow_exit(
                            "be", signal.symbol, direction, signal.pnl_pct,
                            note="剩餘倉位回到進場價平倉 · TP1 獲利已入袋",
                        ))
                        s1_bybit_mirror.mirror_close(signal.symbol, "be")
                        continue

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Error updating signals: {e}")

def target_reached_before_entry(direction, current_price, tp1) -> bool:
    """True if price has already reached the first target while still waiting to
    fill the entry — the intended move happened, so chasing it is pointless."""
    if not CANCEL_QUEUE_IF_TARGET_HIT or tp1 is None:
        return False
    if direction == "LONG":
        return current_price >= tp1
    return current_price <= tp1


def activate_queued_signals():
    """Convert queued setups into real trades only when price reaches entry.

    Each pending setup is evaluated in priority order every minute:
      1. Target already hit  → cancel (move happened without us; chasing loses)
      2. Stop already hit     → cancel (setup invalidated before fill)
      3. Entry not yet touched → keep waiting
      4. Entry touched         → fill the trade
    """
    global queued_signals

    if not queued_signals:
        return

    try:
        symbols = list({payload["symbol"] for payload in queued_signals.values()})
        tickers = get_live_tickers(symbols)
    except Exception as e:
        print(f"Error checking queued entries: {e}")
        return

    for queue_key, payload in list(queued_signals.items()):
        symbol = payload["symbol"]
        ticker = tickers.get(symbol)
        if not ticker:
            continue

        current_price = ticker["last"]
        direction = payload["direction"]
        entry = payload["entry"]
        tp1 = payload["tp"]
        sl = payload["sl"]

        # 1. Target already reached before the entry filled → cancel.
        if target_reached_before_entry(direction, current_price, tp1):
            print(f"Queue cancelled for {symbol}: price reached TP {format_price(tp1)} before entry — move already happened.")
            executor.cancel_resting_order(symbol, direction, reason="target reached before entry")
            update_signal_state_in_ui(
                symbol,
                direction,
                trade_status="rejected",
                reason="cancelled: price reached target before entry filled (move already happened, chasing avoided)",
                current_price=current_price,
                entry=entry,
                tp=tp1,
                sl=sl,
            )
            notify_s1_follow(s1_follow_cancel(symbol, direction, "價格已先觸及目標、未成交"))
            queued_signals.pop(queue_key, None)
            continue

        # 2. Stop already reached before fill → invalidate.
        stop_hit = (
            (direction == "LONG" and current_price <= sl) or
            (direction == "SHORT" and current_price >= sl)
        )
        if stop_hit:
            print(f"Queue invalidated for {symbol}: price moved past SL before activation.")
            executor.cancel_resting_order(symbol, direction, reason="stop reached before entry")
            update_signal_state_in_ui(
                symbol,
                direction,
                trade_status="rejected",
                reason="rejected before activation: price moved through stop before entry touch",
                current_price=current_price,
                entry=entry,
                tp=tp1,
                sl=sl,
            )
            notify_s1_follow(s1_follow_cancel(symbol, direction, "價格已先觸及停損、未成交"))
            queued_signals.pop(queue_key, None)
            continue

        # 3. Entry touched? Check the live price AND the latest 1m candle's wick.
        #    The queue is only evaluated once a minute, so a fast spike through
        #    the entry between checks would otherwise be missed. The 1m high/low
        #    captures that intra-minute wick (same way TP/SL detection works).
        candle_high = candle_low = None
        try:
            ohlcv_1m, _ = get_symbol_ohlcv(symbol, "1m", limit=2)
            if ohlcv_1m:
                candle_high = ohlcv_1m[-1][2]
                candle_low = ohlcv_1m[-1][3]
        except Exception:
            pass

        if direction == "LONG":
            entry_hit = current_price <= entry or (candle_low is not None and candle_low <= entry)
        else:
            entry_hit = current_price >= entry or (candle_high is not None and candle_high >= entry)
        if not entry_hit:
            continue

        # 4. Entry touched → fill at the PLANNED entry price (limit-order fill),
        #    so the TP/SL distances stay exactly as designed regardless of where
        #    the price is at the moment we detect the touch.
        filled_entry = entry
        tp_price = payload["tp"]
        tp2_price = payload.get("tp2", payload["tp"])
        sl_price = payload["sl"]

        # Backstop against phantom records: in the resting-order model a setup with
        # no order actually on the book (skipped below-min-size or exchange-rejected,
        # so place_resting_order returned None) has NO live position. Recording it
        # here would write a PENDING row that never matches the account. Skip it.
        if executor.uses_resting_orders() and not executor.has_resting_order(symbol, direction):
            print(f"Queue skipped for {symbol}: no resting order on the exchange "
                  f"(entry was not placed) — not recording.")
            update_signal_state_in_ui(
                symbol,
                direction,
                trade_status="rejected",
                reason="entry order not placed (below min order size or exchange-rejected) — not recorded",
                current_price=current_price,
                entry=entry,
                tp=tp1,
                sl=sl,
            )
            notify_s1_follow(s1_follow_cancel(symbol, direction, "掛單已不在委託簿、未成交"))
            queued_signals.pop(queue_key, None)
            continue

        recorded, reason = record_signal(
            symbol,
            direction,
            filled_entry,
            tp_price,
            tp2_price,
            sl_price,
            payload["lights_count"],
            rsi_value=payload.get("rsi_value"),
            trend_aligned=payload.get("trend_aligned"),
            smc_zone=payload.get("smc_zone"),
            manage=payload.get("manage", "bracket"),
            trail_dist=payload.get("trail_dist"),
            strategy=payload.get("strategy"),
        )
        if recorded:
            print(f"Queue triggered for {symbol} at entry {format_price(filled_entry)}")
            try:
                if executor.uses_resting_orders():
                    # The resting LIMIT + bracket is already on the exchange and
                    # has now filled — just stop tracking it as pending (the SL/TP
                    # bracket stays live to manage the open position).
                    executor.on_resting_filled(symbol, direction)
                else:
                    # Legacy market-on-touch model (dry-run logs, live sends).
                    executor.open_trade(
                        symbol,
                        direction,
                        filled_entry,
                        sl_price,
                        tp_price,
                        tp2_price,
                        payload["lights_count"],
                        bool(payload.get("trend_aligned")),
                        manage=payload.get("manage", "bracket"),
                    )
            except Exception as exc:  # noqa: BLE001 — never let execution break tracking
                print(f"[executor] open_trade error for {symbol}: {exc}")
            update_signal_state_in_ui(
                symbol,
                direction,
                trade_status="active",
                reason="entry touched, trade active",
                current_price=current_price,
                entry=filled_entry,
                tp=tp_price,
                sl=sl_price,
            )
            notify_s1_follow(
                s1_follow_card(
                    "✅ 進場成交", symbol, direction,
                    filled_entry, sl_price, tp_price, tp2_price,
                    timeframe=payload.get("timeframe"),
                    footer="已進場 · 依計畫嚴守停損",
                )
            )
            # 🪞 mirror the fill onto the real Bybit account (fixed notional;
            # no-op unless S1_BYBIT_MIRROR — and never breaks S1 execution)
            s1_bybit_mirror.mirror_open(symbol, direction, filled_entry, sl_price)
        else:
            print(f"Queue trigger skipped for {symbol}: {reason}")
            update_signal_state_in_ui(
                symbol,
                direction,
                trade_status="rejected",
                reason=reason,
                current_price=current_price,
                entry=entry,
                tp=payload["tp"],
                sl=payload["sl"],
            )

        queued_signals.pop(queue_key, None)

def ensure_database_ready():
    """Create SQLite tables when the bot starts without the web app running first."""
    if not all([app, db]):
        return

    with app.app_context():
        try:
            db.create_all()
            
            # Manual Migrations for SignalRecord
            from sqlalchemy import inspect, text
            if 'signals' in db.engines:
                signals_engine = db.engines['signals']
                sig_inspector = inspect(signals_engine)
                if 'signal_record' in sig_inspector.get_table_names():
                    sig_columns = [c['name'] for c in sig_inspector.get_columns('signal_record')]
                    with signals_engine.connect() as conn:
                        # Add partial_tp1 if needed
                        if 'partial_tp1' not in sig_columns:
                            conn.execute(text("ALTER TABLE signal_record ADD COLUMN partial_tp1 BOOLEAN DEFAULT FALSE"))
                            conn.commit()
                            print("Added partial_tp1 column to signal_record table (from bot)")
                        # Add runner_peak (best price reached after TP1, for the trailing runner)
                        if 'runner_peak' not in sig_columns:
                            conn.execute(text("ALTER TABLE signal_record ADD COLUMN runner_peak FLOAT"))
                            conn.commit()
                            print("Added runner_peak column to signal_record table (from bot)")
                        # Strategy management + live trailing-stop state (S3/S4)
                        for col, ddl in (
                            ("strategy", "ADD COLUMN strategy VARCHAR(20) DEFAULT 'default'"),
                            ("manage", "ADD COLUMN manage VARCHAR(10) DEFAULT 'bracket'"),
                            ("trail_dist", "ADD COLUMN trail_dist FLOAT"),
                            ("peak", "ADD COLUMN peak FLOAT"),
                            ("trough", "ADD COLUMN trough FLOAT"),
                            ("cur_stop", "ADD COLUMN cur_stop FLOAT"),
                            ("last_bar_ts", "ADD COLUMN last_bar_ts BIGINT"),
                        ):
                            if col not in sig_columns:
                                conn.execute(text(f"ALTER TABLE signal_record {ddl}"))
                                conn.commit()
                                print(f"Added {col} column to signal_record table (from bot)")
                            
        except Exception as e:
            print(f"Error creating database tables: {e}")

def calculate_bollinger_bands(prices, window=20, num_std=2):
    if len(prices) < window:
        return None, None, None, None
    
    prices_series = pd.Series(prices)
    middle_band = prices_series.rolling(window=window).mean()
    std_dev = prices_series.rolling(window=window).std()
    upper_band = middle_band + (num_std * std_dev)
    lower_band = middle_band - (num_std * std_dev)
    
    # BB Width = (Upper - Lower) / Middle
    bb_width = (upper_band - lower_band) / middle_band

    return upper_band.iloc[-1], middle_band.iloc[-1], lower_band.iloc[-1], bb_width.iloc[-1]


def bollinger_width_series(prices, window=20, num_std=2):
    """Full Bollinger-band-width series in one pass (for squeeze detection).
    Returns a numpy array aligned to prices, or None if too short."""
    if len(prices) < window:
        return None
    s = pd.Series(prices, dtype=float)
    mid = s.rolling(window).mean()
    std = s.rolling(window).std()
    width = (2 * num_std * std) / mid  # (upper - lower) / middle
    return width.to_numpy()

def calculate_obv(prices, volumes):
    if len(prices) < 2:
        return [0]
    
    obv = [0]
    for i in range(1, len(prices)):
        if prices[i] > prices[i-1]:
            obv.append(obv[-1] + volumes[i])
        elif prices[i] < prices[i-1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return np.array(obv)

def calculate_atr(ohlcv, period=14):
    if len(ohlcv) < period + 1:
        return None
    
    highs = np.array([x[2] for x in ohlcv])
    lows = np.array([x[3] for x in ohlcv])
    closes = np.array([x[4] for x in ohlcv])
    
    tr = np.maximum(
        highs[1:] - lows[1:], 
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    )
    atr = np.mean(tr[-period:])
    return atr


_btc_regime_state = "neutral"  # latest computed BTC regime, surfaced to the UI


def check_btc_regime() -> str:
    """Bitcoin trend regime on the higher timeframe: 'bull', 'bear' or 'neutral'.

    Bull  = BTC price above a RISING EMA  → favour LONGs, block counter-trend SHORTs.
    Bear  = BTC price below a FALLING EMA → favour SHORTs, block counter-trend LONGs.
    Neutral (chop) = no block either way. Computed once per scan."""
    global _btc_regime_state
    if not ENABLE_BTC_REGIME_FILTER:
        _btc_regime_state = "off"
        return "neutral"
    regime = "neutral"
    try:
        need = BTC_REGIME_EMA + BTC_REGIME_SLOPE_LOOKBACK + 5
        ohlcv, _ = get_symbol_ohlcv(BTC_REGIME_SYMBOL, BTC_REGIME_TIMEFRAME, limit=need)
        closes = [x[4] for x in ohlcv]
        if len(closes) >= BTC_REGIME_EMA + BTC_REGIME_SLOPE_LOOKBACK:
            ema = pd.Series(closes).ewm(span=BTC_REGIME_EMA, adjust=False).mean()
            ema_now = ema.iloc[-1]
            ema_prev = ema.iloc[-1 - BTC_REGIME_SLOPE_LOOKBACK]
            price = closes[-1]
            if price > ema_now and ema_now > ema_prev:
                regime = "bull"
            elif price < ema_now and ema_now < ema_prev:
                regime = "bear"
    except Exception as e:  # noqa: BLE001 — never let the regime check break a scan
        print(f"BTC regime check error: {e}")
    _btc_regime_state = regime
    return regime


def check_4h_trend(symbol, current_price):
    try:
        ohlcv_4h, _ = get_symbol_ohlcv(symbol, "4h", limit=100)
        if len(ohlcv_4h) < 50:
            return None, None, None, "Insufficient 4h data"
        prices_4h = [x[4] for x in ohlcv_4h]
        ema50_4h = calculate_ema(prices_4h, 50)
        swing_high_4h = max([x[2] for x in ohlcv_4h[-50:]])
        swing_low_4h = min([x[3] for x in ohlcv_4h[-50:]])
        return ema50_4h, swing_high_4h, swing_low_4h, None
    except Exception as e:
        print(f"Error checking 4h trend for {symbol}: {e}")
        return None, None, None, str(e)





def calculate_position_size(lights_count, is_4h_aligned):
    if not is_4h_aligned:
        return POSITION_SIZE_COUNTER_TREND
    if lights_count >= 6:
        return POSITION_SIZE_6_LIGHTS
    if lights_count >=5:
        return POSITION_SIZE_5_LIGHTS
    return POSITION_SIZE_4_LIGHTS

def calculate_strategy_score(prices, volumes, rsi_values):
    """Compute a directional strategy score.  Returns (bull_score, bear_score, details)."""
    bull_score = 0
    bear_score = 0
    details = []
    
    if len(prices) < 50 or len(volumes) < 21 or len(rsi_values) < 11:
        return 0, 0, ["Insufficient data"]

    current_rsi = rsi_values[-1]
    prev_rsi = rsi_values[-2]
    current_price = prices[-1]
    
    # 1. RSI Logic (direction-aware)
    # Rising from oversold → bullish
    if any(r < 30 for r in rsi_values[-10:]) and current_rsi > prev_rsi:
        bull_score += 2
        details.append("RSI Recovery (+2 bull)")
    # Falling from overbought → bearish
    if any(r > 70 for r in rsi_values[-10:]) and current_rsi < prev_rsi:
        bear_score += 2
        details.append("RSI Rejection (+2 bear)")
    # RSI trend
    if current_rsi > 50:
        bull_score += 1
        details.append("RSI > 50 (+1 bull)")
    elif current_rsi < 50:
        bear_score += 1
        details.append("RSI < 50 (+1 bear)")
    
    # 2. Volume Logic 
    avg_vol_20 = np.mean(volumes[-20:])
    current_vol = volumes[-1]
    prev_price = prices[-2]
    if current_vol > 3 * avg_vol_20:
        if current_price > prev_price:
            bull_score += 3
            details.append("Massive Bull Volume 3x (+3 bull)")
        elif current_price < prev_price:
            bear_score += 3
            details.append("Massive Bear Volume 3x (+3 bear)")
    elif current_vol > 2 * avg_vol_20:
        if current_price > prev_price:
            bull_score += 2
            details.append("Bull Volume Surge 2x (+2 bull)")
        elif current_price < prev_price:
            bear_score += 2
            details.append("Bear Volume Surge 2x (+2 bear)")
        
    # 3. OBV Logic (direction-aware)
    obv_values = calculate_obv(prices, volumes)
    if obv_values[-1] > obv_values[-5]: 
        bull_score += 2
        details.append("OBV Trending Up (+2 bull)")
    elif obv_values[-1] < obv_values[-5]:
        bear_score += 2
        details.append("OBV Trending Down (+2 bear)")
        
    # 4. Bollinger Squeeze — context only (no score). Computed in one vectorized
    #    pass instead of rebuilding the bands 30× per symbol.
    upper, middle, lower, current_width = calculate_bollinger_bands(prices)
    bb_width_series = bollinger_width_series(prices)
    if current_width is not None and bb_width_series is not None:
        prior = bb_width_series[-31:-1]
        prior = prior[~np.isnan(prior)]
        if prior.size > 0 and current_width <= prior.min() * 1.15:
            details.append("BB Squeeze")

    # 5. Breakout — confirmed by volume only.
    #    A Bollinger-band break and a 20-bar structure break describe the SAME
    #    event, so they are merged into ONE signal (not double-counted), and a
    #    breakout on weak volume is treated as a likely fakeout and ignored.
    breakout_vol_ok = current_vol >= BREAKOUT_VOLUME_MULTIPLIER * avg_vol_20
    resistance = max(prices[-21:-1])
    support = min(prices[-21:-1])
    bull_breakout = (current_price > resistance) or (upper is not None and current_price > upper)
    bear_breakout = (current_price < support) or (lower is not None and current_price < lower)

    if bull_breakout and breakout_vol_ok:
        bull_score += 2
        details.append("Breakout ↑ on volume (+2 bull)")
    elif bull_breakout:
        details.append("Breakout ↑ (weak volume — ignored)")
    if bear_breakout and breakout_vol_ok:
        bear_score += 2
        details.append("Breakdown ↓ on volume (+2 bear)")
    elif bear_breakout:
        details.append("Breakdown ↓ (weak volume — ignored)")

    return bull_score, bear_score, details

def get_conviction_level(lights_count):
    """Conviction based on how many of the 5 strategy lights are active."""
    if lights_count >= 5: return "🚀 MAX CONVICTION"
    if lights_count >= 4: return "🔥 HIGH CONVICTION"
    if lights_count >= 3: return "👀 WATCHLIST"
    return "Ignore"

def format_price(val: float) -> str:
    if val is None: return "N/A"
    if val < 0.0001: return f"{val:.8f}"
    if val < 0.01: return f"{val:.6f}"
    if val < 1.0: return f"{val:.4f}"
    return f"{val:.2f}"


def pair_name(symbol) -> str:
    """'EVAA/USDT:USDT' → 'EVAA/USDT' for clean Telegram display."""
    base = symbol.split("/")[0].split(":")[0]
    return f"{base}/{QUOTE_ASSET}"


def _signed_pct(entry, target, is_long) -> str:
    """% move entry→target, signed so it's POSITIVE when favourable to the trade
    (TP shows +, SL shows −) for both LONG and SHORT."""
    if not entry or target is None:
        return ""
    raw = (target - entry) / entry * 100.0
    return f"{(raw if is_long else -raw):+.1f}%"


def format_trade_card(headline, symbol, direction, entry, sl, tp,
                      *, timeframe=None, lights=None, rsi=None, footer=None) -> str:
    """Build one clean, monospace-aligned Telegram trade card (HTML parse_mode).

    `headline` is the status chip (e.g. '📥 QUEUED'); `footer` is the bottom status
    line. The Entry/TP/SL block is wrapped in <pre> so its columns line up; the
    chart link sits below as a tappable link. `tp` should be the FINAL target (TP2)
    so the R:R shown reflects the real reward — the 50%-at-TP1 partial is reported
    by its own lifecycle message when it fires."""
    is_long = str(direction).upper() == "LONG"
    base = symbol.split("/")[0].split(":")[0]
    pair = pair_name(symbol)
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{base}{QUOTE_ASSET}.P"
    tf = f"  ({timeframe})" if timeframe else ""

    rows = [f"Entry {format_price(entry)}"]
    if tp is not None:
        rows.append(f"TP    {format_price(tp)}  ({_signed_pct(entry, tp, is_long)})")
    if sl is not None:
        rows.append(f"SL    {format_price(sl)}  ({_signed_pct(entry, sl, is_long)})")
    price_block = "\n".join(rows)

    meta = []
    if tp is not None and sl is not None and entry:
        risk, reward = abs(entry - sl), abs(tp - entry)
        if risk > 0:
            meta.append(f"R:R {reward / risk:.1f}")
    if lights is not None:
        meta.append(f"{int(lights)} lights")
    if rsi is not None:
        meta.append(f"RSI {rsi:.0f}")
    meta_line = " · ".join(meta)

    lines = [f"{headline}  {dir_tag(direction)} · {pair}{tf}",
             f"<pre>{price_block}</pre>"]
    if meta_line:
        lines.append(meta_line)
    if footer:
        lines.append(footer)
    lines.append(f'<a href="{tv_url}">📈 chart →</a>')
    return "\n".join(lines)


# ── Strategy-1 copy-trade feed (📈 S1 交易訊號 topic) ─────────────────────────
# The full live lifecycle in 中文, quoting Bybit prices/links (the user promotes
# Bybit) so followers can mirror each trade. Everything here routes to
# channel="s1signals"; the plain English cards above stay for internal use.
S1_FOLLOW_CHANNEL = "s1signals"


def s1_follow_card(headline, symbol, direction, entry, sl, tp1, tp2, *,
                   timeframe=None, footer=None) -> str:
    """One 中文 copy-trade card in the shared house style (tg_format): headline ·
    方向 · 標的 · 週期 / 槓桿·R:R / monospace 進場停損目標 block / Bybit link /
    免責. Used for 掛單 (limit placed) and 進場成交 (filled)."""
    is_long = str(direction).upper() == "LONG"
    base = symbol.split("/")[0].split(":")[0]
    head = f"{headline} · {tg_format.dir_zh(direction)} {pair_name(symbol)}"
    if timeframe:
        head += f" · {timeframe}"

    meta = [f"槓桿 {LEVERAGE}x"]
    if entry and sl and tp2 and abs(entry - sl) > 0:
        meta.append(f"風險報酬 {abs(tp2 - entry) / abs(entry - sl):.1f}R")

    lines = [head, " · ".join(meta)]
    plan = tg_format.mono_plan(entry, sl, tp1, tp2, is_long=is_long)
    if plan:
        lines += [tg_format.DIV, plan, tg_format.DIV]
    if footer:
        lines.append(footer)
    lines.append(tg_format.bybit_line(base, entry))
    lines.append("⚠️ 非投資建議 · 到目標先平半、停損移進場")
    return "\n".join(x for x in lines if x)


def s1_follow_exit(kind, symbol, direction, pnl_pct, *, note=None) -> str:
    """One-line 中文 exit update for the follow feed. `kind` ∈
    {tp1, sl, tp2, be}; `pnl_pct` is already signed (loss is negative)."""
    pair = pair_name(symbol)
    pct = f"+{pnl_pct:.1f}%" if pnl_pct >= 0 else f"−{abs(pnl_pct):.1f}%"
    verb = {"tp1": "🎯 TP1 達標", "sl": "🛑 停損出場",
            "tp2": "🏆 止盈達標", "be": "⚖️ 保本出場"}[kind]
    lines = [f"{verb} · {tg_format.dir_zh(direction)} {pair}  {pct}"]
    if note:
        lines.append(note)
    return "\n".join(lines)


def s1_follow_cancel(symbol, direction, reason_zh) -> str:
    """取消掛單 notice — a follower who mirrored the limit must know to cancel it."""
    return (f"🚫 取消掛單 · {tg_format.dir_zh(direction)} {pair_name(symbol)}\n"
            f"{reason_zh}，如已掛單請一併取消")


def notify_s1_follow(message: str) -> None:
    """Send an S1 copy-trade message to the 📈 S1 交易訊號 topic (HTML)."""
    notify(message, parse_mode="HTML", channel=S1_FOLLOW_CHANNEL)


def determine_trade_direction(
    current_rsi,
    bull_strat_score,
    bear_strat_score,
    bullish_momentum,
    bearish_momentum,
    strat_4,
    tsi_hint,
    strat_5,
    macd_hint,
    ema_9,
    ema_20,
    ema_50,
    current_price,
    smc,
):
    """
    Vote-based LONG/SHORT with fixed directional bias.
    Now direction-aware: every factor votes for its true direction.
    """
    long_votes = 0
    short_votes = 0

    # RSI extremes (strong directional signal)
    if current_rsi <= RSI_LOWER_THRESHOLD:
        long_votes += 4
    elif current_rsi >= RSI_UPPER_THRESHOLD:
        short_votes += 4
    elif current_rsi >= 70:
        short_votes += 2
    elif current_rsi <= 30:
        long_votes += 2

    # Momentum (EMA cross + volume)
    if bullish_momentum:
        long_votes += 3
    if bearish_momentum:
        short_votes += 3

    # TSI signal
    if strat_4:
        if tsi_hint is True:
            long_votes += 2
        elif tsi_hint is False:
            short_votes += 2

    # MACD signal (new 5th light)
    if strat_5:
        if macd_hint is True:
            long_votes += 2
        elif macd_hint is False:
            short_votes += 2

    # Strategy score is now direction-aware
    if bull_strat_score > bear_strat_score:
        long_votes += 2
    elif bear_strat_score > bull_strat_score:
        short_votes += 2

    # SMC zones
    zone = smc.get("zone_type", "neutral")
    if zone == "support":
        long_votes += 2
    elif zone == "resistance":
        short_votes += 2

    # SMC swing trend
    trend = smc.get("trend", "neutral")
    if trend == "bullish":
        long_votes += 1
    elif trend == "bearish":
        short_votes += 1

    # EMA 9/20 cross
    if ema_9 is not None and ema_20 is not None:
        if ema_9 > ema_20:
            long_votes += 1
        else:
            short_votes += 1

    # EMA 50 trend filter (important)
    if ema_50 is not None and current_price is not None:
        if current_price > ema_50:
            long_votes += 2
        else:
            short_votes += 2

    if long_votes > short_votes:
        return True
    if short_votes > long_votes:
        return False
    return current_rsi < 50


def calculate_trade_levels(current_price, is_long, active_lights_count, ohlcv_15m=None, smc=None):
    """Calculate trade levels with ATR-based SL, FVG midpoint entry, etc."""
    if active_lights_count < MIN_LIGHTS_FOR_ENTRY:
        return None, None, None, None

    atr = calculate_atr(ohlcv_15m) if ohlcv_15m else None
    fvg = smc.get("fvg") if smc else None
    swing_high = smc.get("swing_high") if smc else None
    swing_low = smc.get("swing_low") if smc else None

    # Determine entry price: prioritize FVG midpoint, then ATR offset
    # FVG midpoint is only valid if it gives a retrace entry:
    #   LONG  → midpoint must be BELOW current price (wait for pullback)
    #   SHORT → midpoint must be ABOVE current price (wait for throwback)
    entry = current_price
    fvg_entry_used = False
    if fvg:
        midpoint = fvg["midpoint"]
        direction_ok = (is_long and fvg["type"] == "bullish") or (not is_long and fvg["type"] == "bearish")
        retrace_ok = (is_long and midpoint < current_price) or (not is_long and midpoint > current_price)
        if direction_ok and retrace_ok:
            entry = midpoint
            fvg_entry_used = True

    if not fvg_entry_used:
        # Use ATR-based offset instead of fixed %
        if atr:
            offset_mult = (ATR_OFFSET_MULTIPLIER_MIN + ATR_OFFSET_MULTIPLIER_MAX) / 2
            offset = atr * offset_mult
            if is_long:
                entry = current_price - offset
            else:
                entry = current_price + offset
        else:
            # Fall back to fixed offset if no ATR
            if is_long:
                entry = current_price * (1 - ENTRY_OFFSET_PCT)
            else:
                entry = current_price * (1 + ENTRY_OFFSET_PCT)

    # Calculate SL: beyond nearest swing high/low, 1.5x ATR, cap at MAX_SL_PCT
    sl = None
    if atr:
        sl_distance = atr * ATR_SL_MULTIPLIER
        if is_long:
            sl = swing_low - sl_distance if swing_low else entry - sl_distance
            if (entry - sl) / entry > MAX_SL_PCT:
                sl = entry * (1 - MAX_SL_PCT)
        else:
            sl = swing_high + sl_distance if swing_high else entry + sl_distance
            if (sl - entry) / entry > MAX_SL_PCT:
                sl = entry * (1 + MAX_SL_PCT)
    else:
        # Fall back to fixed SL if no ATR
        if is_long:
            sl = entry * (1 - FIXED_SL_PCT)
        else:
            sl = entry * (1 + FIXED_SL_PCT)

    # Safety guard: a deep FVG entry can leave the structure-based stop on the
    # WRONG side of entry (e.g. swing_low above an FVG-midpoint long entry),
    # which would mean an instant stop-out and a negative/zero risk. Force the
    # stop back to the correct side at the max distance if that ever happens.
    if is_long and sl >= entry:
        sl = entry * (1 - MAX_SL_PCT)
    elif (not is_long) and sl <= entry:
        sl = entry * (1 + MAX_SL_PCT)

    # Calculate TP as a multiple of the ACTUAL risk (R = |entry - SL|) so the
    # reward scales with each coin's volatility and the real stop distance.
    # This is what makes the 1:2 risk-reward genuine instead of a flat 3%/6%.
    risk = abs(entry - sl)
    if risk <= 0:
        # Degenerate stop (shouldn't happen) — fall back to fixed % targets.
        if is_long:
            tp1 = entry * (1 + FIXED_TP_PCT)
            tp2 = entry * (1 + 2 * FIXED_TP_PCT)
        else:
            tp1 = entry * (1 - FIXED_TP_PCT)
            tp2 = entry * (1 - 2 * FIXED_TP_PCT)
    elif is_long:
        tp1 = entry + risk * ATR_TP1_MULTIPLIER
        tp2 = entry + risk * ATR_TP_MULTIPLIER
    else:
        tp1 = entry - risk * ATR_TP1_MULTIPLIER
        tp2 = entry - risk * ATR_TP_MULTIPLIER

    return entry, sl, tp1, tp2


def _rsi_alert_line(alert) -> str:
    """One compact, tappable RSI-extreme row, matching the trade-card aesthetic:
    '🟢 EVAA/USDT · RSI 8.4 · 0.7639 · 1h' with the pair name linked to TradingView.
    🟢 = oversold (bounce candidate), 🔴 = overbought (drop candidate)."""
    symbol = alert["symbol"]
    base = symbol.split("/")[0].split(":")[0]
    tv_url = f"https://www.tradingview.com/chart/?symbol=BINANCE:{base}{QUOTE_ASSET}.P"
    dot = "🟢" if alert["trigger"] == "oversold" else "🔴"
    return (f'{dot} <a href="{tv_url}">{pair_name(symbol)}</a> · '
            f'RSI {alert["rsi"]:.1f} · {format_price(alert["candle_close_price"])} · '
            f'{alert["timeframe"]}')


def send_batch_alerts(alerts, chunk_size=20):
    if not alerts:
        return 0
    total_sent = 0
    # One tappable line per coin (was 7 lines each). Chunked to stay under Telegram's
    # message-length limit; with the RSI ≥90/≤10 gate this is rarely more than a few.
    for i in range(0, len(alerts), chunk_size):
        chunk = alerts[i:i+chunk_size]
        tw_time = get_now_taiwan().strftime("%H:%M")
        part = "" if len(alerts) <= chunk_size else f" ({i // chunk_size + 1})"
        header = (f"📊 RSI 極端值 Extremes — {len(alerts)} 幣{part}  ·  {tw_time} 台北\n"
                  f"🟢 = 超賣(可能反彈) · 🔴 = 超買(可能回落)")
        message = "\n".join([header, ""] + [_rsi_alert_line(a) for a in chunk])
        sent = send_message(message, parse_mode="HTML")
        if sent:
            total_sent += len(chunk)
            print(f"Batch RSI-extreme alert sent ({i+1}-{i+len(chunk)}/{len(alerts)}): {len(chunk)} coins.")
        else:
            print(f"Batch RSI-extreme alert send failed ({i+1}-{i+len(chunk)}/{len(alerts)}); will retry next scan.")
    return total_sent


def send_compact_alert(symbol, timeframe, rsi, candle_close_price, trigger, candle_close_time_ms=None):
    state_label = "超買 OVERBOUGHT" if trigger == "overbought" else "超賣 OVERSOLD"
    tw_time = get_now_taiwan().strftime("%H:%M")
    line = _rsi_alert_line({
        "symbol": symbol, "timeframe": timeframe, "rsi": rsi,
        "candle_close_price": candle_close_price, "trigger": trigger,
    })
    message = f"📊 RSI Extreme · {state_label}  ·  {tw_time} TW\n\n{line}"

    sent = send_message(message, parse_mode="HTML")
    if sent:
        print(f"Alert sent for {symbol}: {state_label} RSI {rsi:.2f}")
    else:
        print(f"Alert send failed for {symbol}: {state_label} RSI {rsi:.2f}; will retry next scan.")
    return sent

def request_queue_clear() -> None:
    try:
        with open(QUEUE_CLEAR_REQUEST_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError as exc:
        print(f"Unable to write queue-clear request: {exc}")


def consume_queue_clear_request() -> bool:
    if not os.path.exists(QUEUE_CLEAR_REQUEST_FILE):
        return False
    try:
        os.remove(QUEUE_CLEAR_REQUEST_FILE)
        return True
    except OSError as exc:
        print(f"Unable to consume queue-clear request: {exc}")
        return False


def clear_queued_entries_from_ui() -> None:
    try:
        if not os.path.exists(DATA_FILE):
            return
        with open(DATA_FILE, "r") as f:
            payload = json.load(f)
        signals = payload.get("signals", [])
        if not isinstance(signals, list):
            return
        payload["signals"] = [
            item for item in signals
            if not (
                isinstance(item, dict)
                and (
                    item.get("trade_status") == "queued"
                    or (item.get("trade_queued") and not item.get("trade_recorded"))
                )
            )
        ]
        temp_file = DATA_FILE + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(payload, f, default=_default_json_encoder)
        os.replace(temp_file, DATA_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"Unable to clear queued entries from UI data: {exc}")


def apply_queue_clear_request() -> bool:
    global queued_signals
    if not consume_queue_clear_request():
        return False
    # Cancel any resting orders for the setups being cleared so we don't leave
    # orphaned entries on the exchange.
    try:
        for payload in queued_signals.values():
            executor.cancel_resting_order(payload["symbol"], payload["direction"], reason="queue cleared")
            notify_s1_follow(s1_follow_cancel(payload["symbol"], payload["direction"], "掛單已手動清除"))
    except Exception as exc:  # noqa: BLE001
        print(f"[executor] cancel on queue-clear error: {exc}")
    queued_signals = {}
    print("Consumed queue-clear request. Queued entries will be replaced in new scan.")
    return True


def _default_json_encoder(obj):
    """Custom JSON encoder to handle numpy types and other non-serializable objects."""
    import numpy as np
    if isinstance(obj, (np.integer, np.int32, np.int64)):
        return int(obj)
    if isinstance(obj, (np.floating, np.float32, np.float64)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.bool_):
        return bool(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def update_ui_data(signals_data, scan_time=None, status="idle", scanned_symbols=None):
    """Save scan results to a JSON file for the Web UI to read."""
    if scan_time is None:
        scan_time = get_now_taiwan().strftime("%Y-%m-%d %H:%M:%S")
    
    data = {
        "last_update": scan_time,
        "status": status,
        "signals": signals_data,
        "scanned_symbols": len(scanned_symbols or []),
        "scanned_symbols_list": scanned_symbols or [],
        "btc_regime": _btc_regime_state,
    }
    try:
        # Write to a temporary file first to prevent corruption
        temp_file = DATA_FILE + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(data, f, default=_default_json_encoder)
        os.replace(temp_file, DATA_FILE)
    except Exception as e:
        print(f"Error updating UI data: {e}")


def update_signal_state_in_ui(
    symbol: str,
    direction: str,
    *,
    trade_status: str,
    reason: str,
    current_price: float | None = None,
    entry: float | None = None,
    tp: float | None = None,
    sl: float | None = None,
    exit_price: float | None = None,
) -> None:
    try:
        if not os.path.exists(DATA_FILE):
            return
        with open(DATA_FILE, "r") as f:
            payload = json.load(f)
        signals = payload.get("signals", [])
        if not isinstance(signals, list):
            return

        updated = 0
        for item in signals:
            if not isinstance(item, dict):
                continue
            if item.get("symbol") != symbol:
                continue
            if item.get("direction") != direction:
                continue

            item["trade_status"] = trade_status
            item["trade_queued"] = trade_status == "queued"
            item["trade_recorded"] = trade_status in {"active", "pending", "tp", "sl"}
            item["record_status_reason"] = reason
            if current_price is not None:
                item["current_price"] = current_price
            if entry is not None:
                item["entry"] = format_price(entry)
            if tp is not None:
                item["tp"] = format_price(tp)
            if sl is not None:
                item["sl"] = format_price(sl)
            if exit_price is not None:
                item["exit_price"] = format_price(exit_price)
            updated += 1

        if updated <= 0:
            return
        payload["last_update"] = get_now_taiwan().strftime("%Y-%m-%d %H:%M:%S")
        temp_file = DATA_FILE + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(payload, f, default=_default_json_encoder)
        os.replace(temp_file, DATA_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"Unable to update signal state in UI: {exc}")

# ── Non-default live strategy (S2/S3/S4) — registry dispatch + live trailing ──
# When config.LIVE_STRATEGY != "default" the live bot trades a backtest-registry
# strategy instead of the inline Strategy-1 confluence logic. The S1 path is left
# COMPLETELY untouched; the self-contained code below runs only for other keys.
def resolve_live_strategy():
    """The live bot trades Strategy 1 only. Kept as the single source of truth for
    signal tagging (save_signal) and the startup marker. Strategies 2–5 were
    removed 2026-06-28; only the 'default' bracket engine remains."""
    return "default", "bracket"


def _send_rsi_extreme_alert(extremes: list) -> None:
    """Send ONE Telegram digest per scan listing coins whose RSI hit a blow-off
    extreme (≥ RSI_ALERT_HIGH overbought / ≤ RSI_ALERT_LOW oversold) on the alert
    timeframe. Alert-only — it never touches trading. Silent when the watch is off
    or nothing is extreme, unless RSI_ALERT_ALWAYS forces a per-scan heartbeat."""
    if not RSI_ALERT_ENABLED:
        return
    if not extremes and not RSI_ALERT_ALWAYS:
        return

    def _price(e):
        p = e.get("price")
        return f"{p:.6g}" if isinstance(p, (int, float)) else "n/a"

    if not extremes:
        send_message(f"📊 RSI watch ({RSI_ALERT_TIMEFRAME}): no coins "
                     f"≥{RSI_ALERT_HIGH:g} or ≤{RSI_ALERT_LOW:g} this scan.",
                     force=True, channel="signals")
        return

    over = sorted([e for e in extremes if e["kind"] == "overbought"], key=lambda e: -e["rsi"])
    under = sorted([e for e in extremes if e["kind"] == "oversold"], key=lambda e: e["rsi"])
    lines = [f"📊 RSI EXTREMES ({RSI_ALERT_TIMEFRAME}) — {len(extremes)} coin(s)"]
    if over:
        lines.append(f"\n🔴 Overbought (RSI ≥ {RSI_ALERT_HIGH:g}):")
        lines += [f"  {e['symbol']}  ·  RSI {e['rsi']:.1f}  ·  {_price(e)}" for e in over]
    if under:
        lines.append(f"\n🟢 Oversold (RSI ≤ {RSI_ALERT_LOW:g}):")
        lines += [f"  {e['symbol']}  ·  RSI {e['rsi']:.1f}  ·  {_price(e)}" for e in under]
    try:
        send_message("\n".join(lines), force=True, channel="signals")   # signals bot, whitelisted through quiet mode
    except Exception as exc:  # noqa: BLE001 — an alert must never break the scan
        print(f"[bot] RSI-extreme alert send failed: {exc}")


def run_bot() -> None:
    global queued_signals
    scan_start_time = get_now_taiwan().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{scan_start_time}] Starting Strategy & RSI scan...")
    apply_queue_clear_request()
    
    # Load existing queued signals to preserve them
    existing_queued = {}  # key: (symbol, timeframe), value: (direction, signal_dict)
    existing_signals = []
    existing_scanned_symbols = []
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r") as f:
                existing_data = json.load(f)
                existing_signals = existing_data.get("signals", [])
                existing_scanned_symbols = existing_data.get("scanned_symbols_list", [])
                # Collect existing queued signals
                for sig in existing_signals:
                    if isinstance(sig, dict) and sig.get("trade_status") == "queued":
                        key = (sig.get("symbol"), sig.get("timeframe"))
                        existing_queued[key] = (sig.get("direction"), sig)
    except Exception:
        pass

    # Initialize new queued signals
    queued_signals = {}

    # Update status to "scanning" at the start
    update_ui_data(
        existing_signals,
        scan_time=scan_start_time,
        status="scanning",
        scanned_symbols=existing_scanned_symbols,
    )

    try:
        symbols = get_target_symbols()
        # Scan-wide / trade-narrow: the universe is volume-ranked, so the first
        # TOP_SYMBOL_LIMIT names are TRADEABLE; the rest are scanned for market
        # breadth only (scored + displayed, but never queued/recorded/ordered).
        tradeable_symbols = set(symbols[:TOP_SYMBOL_LIMIT])
        print(f"Scanning {len(symbols)} pairs ({len(tradeable_symbols)} tradeable, "
              f"{len(symbols) - len(tradeable_symbols)} watch-only)...")
    except Exception as exc:
        print(f"Failed to get target symbols: {exc}")
        update_ui_data(existing_signals, scan_time=scan_start_time, status="idle", scanned_symbols=existing_scanned_symbols)
        return

    all_signals_for_ui = []
    recent_record_states = load_recent_record_states()
    # Re-entry gate input: symbols already holding a live position on the exchange
    # (cached read-only snapshot — one API call per scan). Empty in dry-run / when
    # the account can't be read, so it never blocks paper trading.
    live_position_symbols = executor.open_position_symbols()
    # Compute the BTC regime ONCE per scan — it gates every alt's direction.
    btc_regime = check_btc_regime()
    if ENABLE_BTC_REGIME_FILTER:
        print(f"BTC regime: {btc_regime.upper()} "
              f"({'longs favoured, shorts blocked' if btc_regime=='bull' else 'shorts favoured, longs blocked' if btc_regime=='bear' else 'chop — both allowed'})")
    try:
        kline_cache.update_subscriptions(symbols, TIMEFRAMES)

        price_stream.wait_until_ready(timeout=WS_READY_TIMEOUT_SECONDS)
        ws_prices = price_stream.get_prices(symbols)
        if len(ws_prices) < int(len(symbols) * 0.6):
            price_stream.wait_until_ready(timeout=2)
            ws_prices = price_stream.get_prices(symbols)

        collected_alerts = []
        rsi_extremes = []          # coins at an RSI blow-off extreme → one Telegram digest per scan

        for symbol in symbols:
            is_tradeable = symbol in tradeable_symbols
            for timeframe in TIMEFRAMES:
                try:
                    current_signal_ui = None
                    ohlcv, candle_source = get_symbol_ohlcv(symbol, timeframe, limit=260)
                    # Data-quality gate: need enough closed candles AND the latest one
                    # must be fresh. Acting on a short or stale series produces garbage
                    # indicators (EMA200/TSI silently degrade, signals fire on old data).
                    if len(ohlcv) < MIN_CANDLES_FOR_SIGNAL:
                        continue
                    if not is_ohlcv_fresh(ohlcv, timeframe):
                        continue

                    prices = [x[4] for x in ohlcv]
                    volumes = [x[5] for x in ohlcv]
                    
                    closed_prices = prices
                    closed_volumes = volumes
                    candle_close_price = closed_prices[-1]
                    live_ws_last = price_stream.get_price(symbol)
                    snapshot_ws_last = (ws_prices.get(symbol) or {}).get("last")
                    scan_price = live_ws_last if live_ws_last is not None else snapshot_ws_last if snapshot_ws_last is not None else candle_close_price
                    current_price = scan_price
                    candle_close_time_ms = int(ohlcv[-1][0]) + (timeframe_to_seconds(timeframe) * 1000)
                    
                    rsi_values = calculate_rsi_full(closed_prices, period=RSI_PERIOD)
                    if rsi_values is None or len(rsi_values) == 0:
                        rsi_values = []
                    else:
                        rsi_values = list(rsi_values)  # Convert to list for compatibility
                    if not rsi_values:
                        continue
                        
                    current_rsi = rsi_values[-1]
                    # RSI extreme watch — collect coins whose RSI is at a blow-off
                    # extreme (≥ HIGH overbought / ≤ LOW oversold) on the alert
                    # timeframe, for a single Telegram digest at the end of the scan.
                    if (RSI_ALERT_ENABLED and timeframe == RSI_ALERT_TIMEFRAME
                            and current_rsi is not None
                            and (current_rsi >= RSI_ALERT_HIGH or current_rsi <= RSI_ALERT_LOW)):
                        rsi_extremes.append({
                            "symbol": symbol.split("/")[0].split(":")[0],
                            "tf": timeframe,
                            "rsi": round(float(current_rsi), 1),
                            "kind": "overbought" if current_rsi >= RSI_ALERT_HIGH else "oversold",
                            "price": current_price,
                        })
                    bull_score, bear_score, details = calculate_strategy_score(closed_prices, closed_volumes, rsi_values)
                    display_score = max(bull_score, bear_score)
                    
                    strat_1, stoch_rsi_detail, stoch_rsi_hint = check_stoch_rsi_signal(
                        closed_prices,
                        rsi_period=STOCH_RSI_RSI_PERIOD,
                        stoch_period=STOCH_RSI_STOCH_PERIOD,
                        k_smooth=STOCH_RSI_K_SMOOTH,
                        d_smooth=STOCH_RSI_D_SMOOTH,
                        oversold=STOCH_RSI_OVERSOLD,
                        overbought=STOCH_RSI_OVERBOUGHT,
                    )
                    if stoch_rsi_detail:
                        details.append(stoch_rsi_detail)

                    strat_2 = bull_score >= STRATEGY_SCORE_LIGHT_THRESHOLD or bear_score >= STRATEGY_SCORE_LIGHT_THRESHOLD
                    
                    ema_9 = calculate_ema(closed_prices, 9)
                    ema_20 = calculate_ema(closed_prices, 20)
                    ema_50 = calculate_ema(closed_prices, 50)
                    ema_200 = calculate_ema(closed_prices, 200)
                    avg_vol_20 = np.mean(closed_volumes[-20:])
                    current_vol = closed_volumes[-1]
                    
                    bullish_momentum = False
                    bearish_momentum = False
                    if ema_9 is not None and ema_20 is not None:
                        not_overbought = current_rsi is None or current_rsi <= 70
                        not_oversold = current_rsi is None or current_rsi >= 30
                        bullish_momentum = ema_9 > ema_20 and current_vol > 1.5 * avg_vol_20 and not_overbought
                        bearish_momentum = ema_9 < ema_20 and current_vol > 1.5 * avg_vol_20 and not_oversold
                    strat_3 = bullish_momentum or bearish_momentum

                    strat_4, tsi_detail, tsi_hint = check_tsi_signal(ohlcv)
                    if tsi_detail:
                        details.append(tsi_detail)

                    strat_5, macd_detail, macd_hint = check_macd_signal(closed_prices)
                    if macd_detail:
                        details.append(macd_detail)

                    smc = analyze_smc(ohlcv)

                    if strat_1 or strat_2 or strat_3 or strat_4 or strat_5 or display_score >= 4:
                        base_symbol = symbol.split("/")[0].split(":")[0]
                        strategy_lights = [bool(strat_1), bool(strat_2), bool(strat_3), bool(strat_4), bool(strat_5)]

                        is_long = determine_trade_direction(
                            current_rsi,
                            bull_score,
                            bear_score,
                            bullish_momentum,
                            bearish_momentum,
                            strat_4,
                            tsi_hint,
                            strat_5,
                            macd_hint,
                            ema_9,
                            ema_20,
                            ema_50,
                            current_price,
                            smc,
                        )

                        # L6: Volume Profile — price in discount (LONG) or premium (SHORT)
                        vp_data = calculate_volume_profile(ohlcv, lookback=50)
                        strat_6 = False
                        if vp_data is not None:
                            if is_long and vp_data["below_val"]:
                                strat_6 = True
                                details.append(f"+ VP: discount zone (below VAL {vp_data['val']:.4g})")
                            elif not is_long and vp_data["above_vah"]:
                                strat_6 = True
                                details.append(f"+ VP: premium zone (above VAH {vp_data['vah']:.4g})")
                            elif vp_data["poc_dist_pct"] < 0.5:
                                details.append(f"+ VP: near POC ({vp_data['poc_dist_pct']:.1f}%)")

                        # L7: Order Flow — cumulative delta + absorption/imbalance aligned
                        of_data = calculate_order_flow(ohlcv, lookback=20)
                        strat_7 = False
                        if of_data is not None:
                            if is_long and of_data["bullish"]:
                                strat_7 = True
                                details.append("+ OF: bullish delta/absorption")
                            elif not is_long and of_data["bearish"]:
                                strat_7 = True
                                details.append("+ OF: bearish delta/absorption")

                        strategy_lights.extend([bool(strat_6), bool(strat_7)])

                        # New filters start (after is_long is defined)
                        volume_ok = check_volume_gate(volumes, VOLUME_GATE_MULTIPLIER)
                        if not volume_ok:
                            details.append("⚠ Volume: Signal candle below average volume")

                        rsi_cross_ok = True
                        if REQUIRE_RSI_50_CROSS:
                            rsi_cross_ok = check_rsi_cross_after_extreme(rsi_values, is_long)
                            if not rsi_cross_ok:
                                details.append("⚠ RSI: No cross back above/below 50 after extreme")

                        # 4H trend check
                        ema50_4h, swing_high_4h, swing_low_4h, err_4h = check_4h_trend(symbol, current_price)
                        trend_4h_aligned = True
                        if ENABLE_4H_TREND_FILTER:
                            if ema50_4h is not None:
                                if is_long and current_price < ema50_4h:
                                    trend_4h_aligned = False
                                    details.append("⚠ 4H Trend: LONG below 4H EMA50")
                                elif not is_long and current_price > ema50_4h:
                                    trend_4h_aligned = False
                                    details.append("⚠ 4H Trend: SHORT above 4H EMA50")
                            elif not err_4h:
                                trend_4h_aligned = False
                                details.append("⚠ 4H Trend: no data")
                        if err_4h:
                            details.append(f"⚠ 4H Trend: {err_4h}")

                        # Hidden divergence check
                        hidden_div_ok = check_hidden_divergence(prices, rsi_values, is_long)
                        if hidden_div_ok:
                            details.append("+ Hidden divergence detected")

                        # SMC bonuses for lights
                        smc_bonus = 0
                        if is_long and smc.get("liquidity_sweep_bullish", False):
                            smc_bonus +=2
                            details.append("+ Liquidity sweep (bullish)")
                        elif not is_long and smc.get("liquidity_sweep_bearish", False):
                            smc_bonus +=2
                            details.append("+ Liquidity sweep (bearish)")
                        if is_long and smc.get("choch_bullish", False):
                            smc_bonus +=1
                            details.append("+ CHoCH (bullish)")
                        elif not is_long and smc.get("choch_bearish", False):
                            smc_bonus +=1
                            details.append("+ CHoCH (bearish)")
                        ob_grade = smc.get("ob_grade_bullish",0) if is_long else smc.get("ob_grade_bearish",0)
                        if ob_grade >0:
                            smc_bonus += ob_grade
                            details.append(f"+ OB grade +{ob_grade}")
                        # Reward the IDEAL location (symmetric): LONG at a discount/
                        # support zone, SHORT at a premium/resistance zone. Encourages
                        # high-edge entries without hard-requiring them.
                        smc_zone_now = smc.get("zone_type", "neutral")
                        if (is_long and smc_zone_now == "support") or ((not is_long) and smc_zone_now == "resistance"):
                            smc_bonus += SMC_IDEAL_ZONE_BONUS
                            details.append(f"+ ideal SMC zone +{SMC_IDEAL_ZONE_BONUS}")

                        # Cap the SMC bonus so a flood of confluence can't inflate the
                        # conviction tier past what the real strategy lights justify.
                        if smc_bonus > MAX_SMC_BONUS_LIGHTS:
                            smc_bonus = MAX_SMC_BONUS_LIGHTS
                            details.append(f"+ SMC bonus capped at +{MAX_SMC_BONUS_LIGHTS}")

                        smc_zone = smc.get("zone_type", "neutral")
                        smc_blocked = False
                        if is_long and smc_zone == "resistance":
                            smc_blocked = True
                            details.append("⚠ SMC: LONG blocked in Premium Zone")
                        elif not is_long and smc_zone == "support":
                            # In a confirmed BEAR regime, downtrends break support, so a
                            # trend-short into a discount zone is the trade, not a trap.
                            if ALLOW_BEAR_TREND_SHORTS_IN_DISCOUNT and btc_regime == "bear":
                                details.append("· SMC discount override (bear-trend short)")
                            else:
                                smc_blocked = True
                                details.append("⚠ SMC: SHORT blocked in Discount Zone")

                        trend_aligned = True
                        if ema_50 is not None:
                            if is_long and current_price < ema_50:
                                trend_aligned = False
                                details.append("⚠ Trend: LONG below EMA50")
                            elif not is_long and current_price > ema_50:
                                trend_aligned = False
                                details.append("⚠ Trend: SHORT above EMA50")

                        macro_trend_aligned = True
                        if ema_200 is not None:
                            if is_long and current_price < ema_200:
                                macro_trend_aligned = False
                                details.append("⚠ Macro Trend: LONG below EMA200")
                            elif not is_long and current_price > ema_200:
                                macro_trend_aligned = False
                                details.append("⚠ Macro Trend: SHORT above EMA200")

                        momentum_aligned = True
                        if len(rsi_values) >= 2:
                            if is_long and not (rsi_values[-1] > rsi_values[-2]):
                                momentum_aligned = False
                                details.append("⚠ RSI: LONG momentum not rising")
                            elif not is_long and not (rsi_values[-1] < rsi_values[-2]):
                                momentum_aligned = False
                                details.append("⚠ RSI: SHORT momentum not falling")

                        base_lights_count = sum(strategy_lights)
                        active_lights_count = base_lights_count + smc_bonus
                        score_edge = (bull_score - bear_score) if is_long else (bear_score - bull_score)
                        directional_bias_ok = score_edge >= 2
                        if not directional_bias_ok:
                            details.append("⚠ Direction: weak bull/bear score edge")

                        base_lights_ok = base_lights_count >= MIN_BASE_LIGHTS
                        if not base_lights_ok:
                            details.append(f"⚠ Lights: fewer than {MIN_BASE_LIGHTS} real strategy lights")

                        effective_lights = active_lights_count

                        entry_price, sl_price, tp1_price, tp2_price = calculate_trade_levels(scan_price, is_long, effective_lights, ohlcv, smc)
                        direction_str = "LONG" if is_long else "SHORT"

                        existing_trade_state = recent_record_states.get((symbol, direction_str))
                        has_active_trade = bool(existing_trade_state and existing_trade_state.get("trade_status") == "active")

                        # Symmetric conviction bar — LONG and SHORT both need the same lights.
                        required_lights = MIN_LIGHTS_FOR_RECORD if is_long else MIN_LIGHTS_SHORT

                        # Symmetric RSI guard — avoid entering at an exhausted extreme
                        # (don't buy a blow-off top, don't short a capitulation bottom).
                        rsi_filter_ok = True
                        if current_rsi is not None:
                            if is_long and current_rsi >= RSI_BLOCK_LONG_ABOVE:
                                rsi_filter_ok = False
                            elif (not is_long) and current_rsi <= RSI_BLOCK_SHORT_BELOW:
                                rsi_filter_ok = False

                        # Zone gating is handled symmetrically by smc_blocked below
                        # (LONG never at premium, SHORT never at discount). No extra
                        # one-sided requirement, so both directions trade equally.
                        smc_zone_ok = True

                        # BTC regime filter — don't fight Bitcoin. Block LONGs while
                        # BTC is in a bear regime and SHORTs while BTC is bullish.
                        btc_regime_ok = True
                        if ENABLE_BTC_REGIME_FILTER:
                            if is_long and btc_regime == "bear":
                                btc_regime_ok = False
                            elif (not is_long) and btc_regime == "bull":
                                btc_regime_ok = False
                        # Optional longs-only mode for maximum consistency.
                        shorts_ok = ENABLE_SHORTS or is_long

                        # VWAP confirmation — only take LONGs above the rolling VWAP
                        # (buyers in control) and SHORTs below it (sellers in control).
                        vwap_ok = True
                        if ENABLE_VWAP_FILTER:
                            vwap = calculate_vwap(ohlcv, VWAP_PERIOD)
                            if vwap is not None:
                                if is_long and scan_price < vwap:
                                    vwap_ok = False
                                elif (not is_long) and scan_price > vwap:
                                    vwap_ok = False
                        if not vwap_ok:
                            details.append("⚠ VWAP: price on the wrong side of fair value")

                        if not btc_regime_ok:
                            details.append(f"⚠ BTC regime {btc_regime.upper()} blocks this {'LONG' if is_long else 'SHORT'}")

                        # Don't queue a setup that is already extended past its
                        # target — price has moved too far to enter (would chase).
                        not_extended = True
                        if tp1_price is not None:
                            if is_long and scan_price >= tp1_price:
                                not_extended = False
                            elif (not is_long) and scan_price <= tp1_price:
                                not_extended = False
                        if not not_extended:
                            details.append("⚠ Setup: price already reached target zone (too extended to enter)")

                        # ADX trend-strength gate (optional, default OFF) — an
                        # EMA-cross system bleeds in chop, so require a real trend
                        # before committing. Computed on the scan-timeframe candles.
                        adx_ok = True
                        adx_now = None
                        if ENABLE_ADX_FILTER:
                            _adx = adx_components(ohlcv, ADX_PERIOD)
                            if _adx is not None:
                                adx_now = _adx["adx"]
                                strong = adx_now >= ADX_MIN_THRESHOLD
                                rising = _adx["rising"] or not ADX_REQUIRE_RISING
                                adx_ok = strong and rising
                                if not adx_ok:
                                    details.append(
                                        f"⚠ ADX {adx_now:.1f}: trend too weak "
                                        f"(need ≥{ADX_MIN_THRESHOLD:g}"
                                        f"{' & rising' if ADX_REQUIRE_RISING else ''})"
                                    )
                            # Can't compute ADX (short history) → leave adx_ok True;
                            # don't block on degraded data.

                        trade_qualified = (
                            effective_lights >= required_lights
                            and base_lights_ok
                            and trend_aligned
                            and macro_trend_aligned
                            and momentum_aligned
                            and directional_bias_ok
                            and not smc_blocked
                            and rsi_filter_ok
                            and smc_zone_ok
                            and btc_regime_ok
                            and shorts_ok
                            and vwap_ok
                            and not_extended
                            and volume_ok
                            and (not REQUIRE_RSI_50_CROSS or rsi_cross_ok)
                            and trend_4h_aligned
                            and adx_ok
                            and entry_price is not None
                            and not check_circuit_breaker()
                            and is_tradeable   # watch-only pairs (rank > TOP_SYMBOL_LIMIT) never trade
                        )

                        # Funding-rate sentiment filter (optional, default OFF).
                        # Only worth an API call once a setup is otherwise
                        # qualified, so gate on trade_qualified first: skip NEW
                        # longs into extreme positive funding (crowded longs) and
                        # NEW shorts into extreme negative funding.
                        funding_ok = True
                        funding_now = None
                        if ENABLE_FUNDING_FILTER and trade_qualified:
                            funding_now = executor.funding_rate(symbol)
                            if funding_now is not None:
                                if is_long and funding_now > FUNDING_MAX_LONG:
                                    funding_ok = False
                                elif (not is_long) and funding_now < FUNDING_MIN_SHORT:
                                    funding_ok = False
                            if not funding_ok:
                                trade_qualified = False
                                details.append(
                                    f"⚠ Funding {funding_now * 100:.3f}%/8h — "
                                    f"crowded {'longs' if is_long else 'shorts'}, skipping entry"
                                )

                        record_reasons = []
                        if effective_lights < required_lights:
                            record_reasons.append(f"effective lights below {required_lights}")
                        if not base_lights_ok:
                            record_reasons.append(f"fewer than {MIN_BASE_LIGHTS} base strategy lights")
                        if not trend_aligned:
                            record_reasons.append("EMA50 trend not aligned")
                        if not macro_trend_aligned:
                            record_reasons.append("EMA200 trend not aligned")
                        if not momentum_aligned:
                            record_reasons.append("RSI momentum not aligned")
                        if not directional_bias_ok:
                            record_reasons.append("bull/bear score edge too weak")
                        if smc_blocked:
                            record_reasons.append("SMC zone blocked this direction (LONG at premium / SHORT at discount)")
                        if not rsi_filter_ok:
                            if is_long:
                                record_reasons.append(f"LONG blocked: RSI ≥ {RSI_BLOCK_LONG_ABOVE} (overbought top)")
                            else:
                                record_reasons.append(f"SHORT blocked: RSI ≤ {RSI_BLOCK_SHORT_BELOW} (oversold bottom)")
                        if not not_extended:
                            record_reasons.append("price already reached target zone (too extended to enter)")
                        if not volume_ok:
                            record_reasons.append("volume gate failed")
                        if REQUIRE_RSI_50_CROSS and not rsi_cross_ok:
                            record_reasons.append("RSI cross back 50 failed")
                        if not trend_4h_aligned:
                            record_reasons.append("4H trend not aligned")
                        if not adx_ok:
                            record_reasons.append(
                                f"ADX trend strength below {ADX_MIN_THRESHOLD:g}"
                                + (" or not rising" if ADX_REQUIRE_RISING else "")
                            )
                        if not funding_ok:
                            record_reasons.append(
                                f"funding {funding_now * 100:.3f}%/8h too "
                                + ("high for a long" if is_long else "low for a short")
                            )
                        if not btc_regime_ok:
                            record_reasons.append(f"BTC regime {btc_regime.upper()} — counter-trend {'LONG' if is_long else 'SHORT'} blocked")
                        if entry_price is None:
                            record_reasons.append("trade plan not available")
                        if not is_tradeable:
                            record_reasons.append(f"watch-only — ranked outside the top {TOP_SYMBOL_LIMIT} tradeable pairs")

                        trade_recorded = False
                        setup_queued = False
                        record_status_reason = "not evaluated"
                        # Re-entry gate: never open a second position in a symbol that
                        # already holds a live position on the exchange (in-memory
                        # tracking is lost on restart and a terminal DB row can't be
                        # relied on — see MEGA). live_position_symbols is the cached
                        # snapshot taken once at the top of this scan.
                        already_open = symbol in live_position_symbols
                        if trade_qualified and not has_active_trade and not already_open:
                            queue_key = (symbol, timeframe, direction_str)
                            # Alert only on a NEW queue — not every scan while it waits.
                            is_new_queue = queue_key not in queued_signals
                            # Concurrency cap: gate ONLY genuinely new commitments.
                            # A setup already resting/queued must keep refreshing
                            # (otherwise reconcile_resting would cancel it), so it
                            # bypasses the cap — it's already counted.
                            # Correlation / same-direction cap (optional, default
                            # OFF): don't let a basket of same-side alt trades
                            # become one big correlated bet. Counts live positions
                            # + still-queued setups already committed this side.
                            direction_capped = False
                            if ENABLE_DIRECTION_CAP and is_new_queue and MAX_SAME_DIRECTION > 0:
                                live_same = executor.open_directional_counts().get(direction_str, 0)
                                queued_same = sum(1 for k in queued_signals if k[2] == direction_str)
                                direction_capped = (live_same + queued_same) >= MAX_SAME_DIRECTION
                            if is_new_queue and not executor.has_capacity():
                                record_status_reason = (
                                    f"concurrency cap full — "
                                    f"{executor.concurrent_commitments()}/{MAX_CONCURRENT_POSITIONS} "
                                    f"positions committed; not queued this scan"
                                )
                                print(
                                    f"[CAP] {symbol} {direction_str} not queued — "
                                    f"{executor.concurrent_commitments()}/{MAX_CONCURRENT_POSITIONS} concurrent"
                                )
                            elif direction_capped:
                                record_status_reason = (
                                    f"same-direction cap full — {MAX_SAME_DIRECTION} "
                                    f"{direction_str} positions already committed; not queued this scan"
                                )
                                print(
                                    f"[CAP] {symbol} {direction_str} not queued — "
                                    f"same-direction cap {MAX_SAME_DIRECTION}"
                                )
                            else:
                                # Resting-limit model: book the LIMIT entry + SL/TP
                                # bracket on the exchange FIRST. Only track the setup
                                # as queued when an order is actually resting — a
                                # skipped (below-min-size) or exchange-rejected order
                                # returns None and must NOT become a queued setup, or
                                # the price-touch path later records a phantom PENDING
                                # with no live position behind it.
                                placed = None
                                try:
                                    placed = executor.place_resting_order(
                                        symbol, direction_str, entry_price,
                                        sl_price, tp1_price, tp2_price,
                                        effective_lights, bool(trend_aligned),
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    print(f"[executor] place_resting_order error for {symbol}: {exc}")
                                if placed:
                                    setup_queued = True
                                    queued_signals[queue_key] = {
                                        "symbol": symbol,
                                        "timeframe": timeframe,
                                        "direction": direction_str,
                                        "entry": entry_price,
                                        "tp": tp1_price,
                                        "tp2": tp2_price,
                                        "sl": sl_price,
                                        "lights_count": effective_lights,
                                        "rsi_value": current_rsi,
                                        "trend_aligned": trend_aligned,
                                        "smc_zone": smc_zone,
                                    }
                                    record_status_reason = build_queue_status_reason()
                                    # Telegram alert the moment a setup is freshly queued
                                    # (i.e. the funnel's "queued" count just went up).
                                    if is_new_queue:
                                        notify_s1_follow(
                                            s1_follow_card(
                                                "⏳ 掛單待成交", symbol, direction_str,
                                                entry_price, sl_price, tp1_price, tp2_price,
                                                timeframe=timeframe,
                                                footer="已掛限價單，等待成交（可同步掛單）",
                                            )
                                        )
                                else:
                                    # No order on the book → don't queue, don't alert,
                                    # don't let a later price-touch record a phantom.
                                    queued_signals.pop(queue_key, None)
                                    record_status_reason = (
                                        "entry order not placed (below min order size "
                                        "or exchange-rejected) — not queued"
                                    )
                        elif trade_qualified and not has_active_trade and already_open:
                            record_status_reason = (
                                "skipped — a live position already exists on the exchange "
                                "for this symbol (no new entry while one is open)"
                            )
                        elif has_active_trade:
                            record_status_reason = existing_trade_state.get("record_status_reason") or "trade active"
                        else:
                            record_status_reason = build_rejected_status_reason(record_reasons)

                        if smc["details"]:
                            details.append(smc["summary"])
                        
                        current_signal_ui = {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "current_price": scan_price,
                            "rsi": current_rsi,
                            "score": display_score,
                            "direction": direction_str,
                            "conviction": get_conviction_level(effective_lights),
                            "lights": strategy_lights,
                            "effective_lights": effective_lights,
                            "trend_aligned": trend_aligned,
                            "smc_blocked": smc_blocked,
                            "tradeable": is_tradeable,
                            "trade_status": "queued" if setup_queued else "rejected",
                            "trade_queued": setup_queued,
                            "trade_recorded": trade_recorded,
                            "record_status_reason": record_status_reason,
                            "entry": format_price(entry_price) if entry_price else None,
                            "sl": format_price(sl_price) if sl_price else None,
                            "tp": format_price(tp1_price) if tp1_price else None,
                            "tp2": format_price(tp2_price) if tp2_price else None,
                            "details": details,
                            "smc": {
                                "zone_type": smc["zone_type"],
                                "zone_label": smc["zone_label"],
                                "summary": smc["summary"],
                                "details": smc["details"],
                                "trend": smc["trend"],
                                "swing_high": format_price(smc["swing_high"]) if smc["swing_high"] else None,
                                "swing_low": format_price(smc["swing_low"]) if smc["swing_low"] else None,
                                "dist_to_high_pct": smc["dist_to_high_pct"],
                                "dist_to_low_pct": smc["dist_to_low_pct"],
                            },
                            "tv_url": f"https://www.tradingview.com/chart/?symbol=BINANCE:{base_symbol}{QUOTE_ASSET}.P"
                        }

                        # Check for existing queued signal.
                        key = (symbol, timeframe)
                        if key in existing_queued:
                            existing_dir, existing_sig = existing_queued[key]
                            # Preserve a stable queued card ONLY if this fresh scan
                            # actually re-queued the same setup (order placed). If the
                            # scan now rejects it (price ran past target) or the entry
                            # order wasn't placed, the fresh rejected card wins so the
                            # stale "queued" card disappears.
                            if existing_dir == direction_str and setup_queued:
                                existing_sig["current_price"] = scan_price
                                current_signal_ui = existing_sig
                            # Consume it either way — this scan is now authoritative.
                            del existing_queued[key]

                        if existing_trade_state:
                            current_signal_ui["trade_status"] = existing_trade_state["trade_status"]
                            current_signal_ui["trade_queued"] = existing_trade_state["trade_status"] == "queued"
                            current_signal_ui["trade_recorded"] = existing_trade_state["trade_status"] in {"active", "pending", "tp", "sl"}
                            current_signal_ui["record_status_reason"] = existing_trade_state["record_status_reason"]
                            if existing_trade_state.get("entry") is not None:
                                current_signal_ui["entry"] = existing_trade_state["entry"]
                            if existing_trade_state.get("tp") is not None:
                                current_signal_ui["tp"] = existing_trade_state["tp"]
                            if existing_trade_state.get("tp2") is not None:
                                current_signal_ui["tp2"] = existing_trade_state["tp2"]
                            if existing_trade_state.get("sl") is not None:
                                current_signal_ui["sl"] = existing_trade_state["sl"]
                            if existing_trade_state.get("exit_price") is not None:
                                current_signal_ui["exit_price"] = existing_trade_state["exit_price"]
                        all_signals_for_ui.append(current_signal_ui)

                    # Batch RSI alerts fire ONLY on a GENUINE RSI extreme
                    # (RSI ≥ RSI_UPPER_THRESHOLD or ≤ RSI_LOWER_THRESHOLD) — this is
                    # what the startup banner promises. The old gate used the StochRSI
                    # K 80/20 cross, which is far looser and flooded Telegram with
                    # ~180 alerts/scan. Deriving the trigger straight from the real
                    # RSI-7 makes "limited to RSI ≥ 90 / ≤ 10" actually true.
                    should_alert = False
                    alert_trigger = None
                    if current_rsi is not None:
                        if current_rsi >= RSI_UPPER_THRESHOLD:
                            alert_trigger = "overbought"
                        elif current_rsi <= RSI_LOWER_THRESHOLD:
                            alert_trigger = "oversold"

                    if alert_trigger is None:
                        rsi_txt = f"{current_rsi:.1f}" if current_rsi is not None else "n/a"
                        telegram_reason = (f"RSI {rsi_txt} not extreme "
                                           f"(need ≥{RSI_UPPER_THRESHOLD:.0f} or ≤{RSI_LOWER_THRESHOLD:.0f})")
                        if last_alerts.get((symbol, timeframe)):
                            last_alerts[(symbol, timeframe)] = None
                    else:
                        telegram_reason = (
                            f"RSI {current_rsi:.1f} ≥ {RSI_UPPER_THRESHOLD:.0f} (Overbought)"
                            if alert_trigger == "overbought"
                            else f"RSI {current_rsi:.1f} ≤ {RSI_LOWER_THRESHOLD:.0f} (Oversold)"
                        )
                        if last_alerts.get((symbol, timeframe)) != alert_trigger:
                            should_alert = True
                        else:
                            telegram_reason = f"{telegram_reason} (already sent)"

                    if current_signal_ui is not None:
                        current_signal_ui["telegram_reason"] = telegram_reason
                        current_signal_ui["telegram_ready"] = should_alert

                    if should_alert:
                        # Collect alert info for batch send
                        collected_alerts.append({
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "rsi": current_rsi,
                            "candle_close_price": candle_close_price,
                            "trigger": alert_trigger,
                            "candle_close_time_ms": candle_close_time_ms
                        })
                        last_alerts[(symbol, timeframe)] = alert_trigger
                    
                except Exception as e:
                    print(f"Error scanning {symbol} {timeframe}: {e}")
        
        # Send batch alerts after all symbols are scanned
        send_batch_alerts(collected_alerts)
        # One RSI-extreme digest per scan (coins ≥90 / ≤10 RSI). Alert-only.
        _send_rsi_extreme_alert(rsi_extremes)
    finally:
        # Add back any remaining existing queued signals (not scanned in this run)
        for remaining_key, (remaining_dir, remaining_sig) in existing_queued.items():
            # Update current price if possible (try to get from websocket if available)
            try:
                price_from_ws = price_stream.get_price(remaining_key[0])
                if price_from_ws is not None:
                    remaining_sig["current_price"] = price_from_ws
            except:
                pass
            # Drop a carried-forward queued card whose target has already been
            # reached — the move happened, the setup is dead, don't keep showing it.
            if remaining_sig.get("trade_status") == "queued":
                try:
                    cp = float(remaining_sig.get("current_price"))
                    tp_val = float(str(remaining_sig.get("tp")))
                    if target_reached_before_entry(remaining_dir, cp, tp_val):
                        print(f"Dropping stale queued card for {remaining_key[0]}: target already reached before entry.")
                        executor.cancel_resting_order(remaining_key[0], remaining_dir, reason="stale: target reached")
                        notify_s1_follow(s1_follow_cancel(remaining_key[0], remaining_dir, "價格已先觸及目標、未成交"))
                        queued_signals.pop((remaining_key[0], remaining_key[1], remaining_dir), None)
                        continue
                except (TypeError, ValueError):
                    pass
            all_signals_for_ui.append(remaining_sig)

        # Pull any resting orders whose setup no longer qualifies this scan.
        try:
            active_keys = {(p["symbol"], p["direction"]) for p in queued_signals.values()}
            executor.reconcile_resting(active_keys)
        except Exception as exc:  # noqa: BLE001
            print(f"[executor] reconcile_resting error: {exc}")

        all_signals_for_ui.sort(key=lambda x: (x.get("effective_lights", 0), x["score"]), reverse=True)
        scan_end_time = get_now_taiwan().strftime("%Y-%m-%d %H:%M:%S")
        update_ui_data(all_signals_for_ui, scan_time=scan_end_time, status="idle", scanned_symbols=symbols)
        kline_cache.save_cache()
        print(f"[{scan_end_time}] Scan complete.")

def startup_message() -> None:
    print(f"Bot started. Telegram: trade-opportunity cards (queued/filled/exit) "
          f"+ RSI-extreme alerts limited to RSI >= {RSI_UPPER_THRESHOLD:.0f} "
          f"or RSI <= {RSI_LOWER_THRESHOLD:.0f}.")
    # Resolve + announce which strategy the live bot trades (validated against the
    # registry; falls back to S1 on an unknown key). Loud so a misconfig is obvious.
    try:
        import backtest as BT
        _key, _manage = resolve_live_strategy()
        _name = BT.STRATEGIES[_key]["name"]
        print(f"[strategy] LIVE strategy = {_key} ({_name}) — management: {_manage}")
        # Marker the web Account page reads to flag "saved ≠ running" divergence.
        try:
            with open(os.path.join(os.path.dirname(__file__), "bot_strategy.json"), "w") as _f:
                json.dump({"strategy": _key, "manage": _manage,
                           "started": get_now_taiwan().strftime("%Y-%m-%d %H:%M:%S")}, _f)
        except Exception:  # noqa: BLE001
            pass
        if _key != "default" and executor.is_live():
            send_message(f"🧠 LIVE strategy: {_name}\nManagement: {_manage} (S1 path "
                         f"{'OFF' if _key != 'default' else 'ON'}). Real orders armed.")
    except Exception as exc:  # noqa: BLE001 — never let this block startup
        print(f"[strategy] resolve error at startup: {exc}")
    # Live safety gates (run in order; each halts live trading on failure so the
    # bot falls back to dry-run instead of sending orders the exchange would
    # mishandle):
    #   1. Exchange access — authenticate against the CONFIGURED host. Catches
    #      keys minted on the wrong testnet site (host/key mismatch) before any
    #      order is sent.
    #   2. Position mode — confirm ONE-WAY mode; the reduceOnly/closePosition
    #      bracket orders are rejected in hedge mode, which would leave a filled
    #      entry with no stop.
    def _run_live_gate(check):
        ok, msg = check()
        print(f"[executor] {msg}")
        if not ok:
            executor.halt_live_trading(msg)
            try:
                send_message(f"⛔ LIVE TRADING HALTED — {msg}\nBot continues in DRY-RUN until fixed.")
            except Exception as exc:  # noqa: BLE001
                print(f"Telegram halt notice failed: {exc}")

    if executor.is_live():
        _run_live_gate(executor.verify_exchange_access)
    if executor.is_live():  # re-check: the access gate may have halted trading
        _run_live_gate(executor.verify_position_mode)
    line = executor.status_line()
    print(line)
    if executor.is_live():
        # Make it impossible to miss that real orders are armed.
        send_message(f"⚠️ {line}")

def shutdown_message() -> None:
    print("Bot stopped.")

def main() -> None:
    # SCAN_ONLY: a dashboard-refresh companion. It runs the full 1h scan (so the
    # main dashboard's scan_results.json keeps updating hourly) but NEVER holds the
    # bot lock — so it doesn't block the Strategy-2 live engine (which refuses to
    # trade while an S1 bot lock is held) — and force-halts live trading so it can
    # never place a real order, even if LIVE_TRADING=true. Used by run_all.sh when
    # S2 is the live engine but you still want the S1-based dashboard.
    scan_only = os.getenv("SCAN_ONLY", "").strip().lower() in ("1", "true", "yes", "on")
    if scan_only:
        executor.halt_live_trading("SCAN_ONLY — dashboard refresh companion; no orders placed")
        print("[bot] SCAN_ONLY mode: scanning to refresh the dashboard only — "
              "no bot lock, no orders. (S2 remains the live engine.)")
    else:
        acquire_bot_lock()
    ensure_database_ready()
    price_stream.start()
    kline_cache.start()
    price_stream.wait_until_ready(timeout=WS_READY_TIMEOUT_SECONDS)
    startup_message()

    # Run initial tasks
    update_pending_signals()
    run_bot()

    scheduler = BlockingScheduler()
    scheduler.add_job(update_pending_signals, "interval", minutes=1)
    scheduler.add_job(run_bot, "interval", minutes=CHECK_INTERVAL_MINUTES)

    def _shutdown(signum, frame):
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            scheduler.wakeup()  # unblocks _main_loop so start() can return
        except Exception:
            pass

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        price_stream.stop()

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        error_text = traceback.format_exc()
        print(error_text)
    finally:
        shutdown_message()
        # os._exit skips Python's atexit/threading cleanup, which prevents the
        # spurious "Exception ignored in threading._shutdown" traceback that
        # appears when Ctrl+C interrupts concurrent.futures thread joins.
        os._exit(0)
