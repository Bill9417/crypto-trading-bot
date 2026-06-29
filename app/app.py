import json
import os
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps


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

from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from config import (
    TOP_SYMBOL_LIMIT, SCAN_SYMBOL_LIMIT, CHECK_INTERVAL_MINUTES, RSI_UPPER_THRESHOLD, RSI_LOWER_THRESHOLD,
    ATR_SL_MULTIPLIER, ATR_TP_MULTIPLIER, MIN_LIGHTS_FOR_ALERT, MIN_LIGHTS_FOR_RECORD,
    REST_API_MAX_RETRIES, REST_API_MIN_INTERVAL_SEC, QUOTE_ASSET,
    DAILY_MAX_LOSSES, DAILY_MAX_DRAWDOWN_PCT,
    VOLUME_GATE_MULTIPLIER, MIN_BASE_LIGHTS,
    LEVERAGE, FIXED_MARGIN_USDT, MAX_MARGIN_USDT, MAX_CONCURRENT_POSITIONS,
    LIVE_PARTIAL_TP, LIVE_TP_TARGET,
)
from market_data import SafeBinanceClient, RateLimitCooldownError
import market_intel
import executor

def _resolve_secret_key() -> str:
    """A strong, persistent session key with zero manual setup.
    Order: FLASK_SECRET_KEY env → local .secret_key file → freshly generated
    (then saved). This guarantees the key is never the shared default, so nobody
    can forge a session cookie / admin login against the public domain."""
    env_key = os.getenv('FLASK_SECRET_KEY')
    if env_key and env_key != 'change-me-before-public-release':
        return env_key
    key_path = os.path.join(os.path.dirname(__file__), '.secret_key')
    try:
        with open(key_path, 'r', encoding='utf-8') as f:
            saved = f.read().strip()
            if saved:
                return saved
    except FileNotFoundError:
        pass
    new_key = secrets.token_hex(32)
    try:
        with open(key_path, 'w', encoding='utf-8') as f:
            f.write(new_key)
        os.chmod(key_path, 0o600)
    except OSError as e:
        print(f"[security] could not persist .secret_key ({e}); using an in-memory key (sessions reset on restart)")
    return new_key


app = Flask(__name__)
app.config['SECRET_KEY'] = _resolve_secret_key()
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['SQLALCHEMY_BINDS'] = {
    'signals': 'sqlite:///signals.db'
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# Re-read templates from disk on each request so HTML/CSS/JS edits show up on a
# plain browser refresh — no server restart needed (cheap; this app is local).
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_SESSION_COOKIE_SECURE', 'false').lower() == 'true'
app.config['PREFERRED_URL_SCHEME'] = os.getenv('FLASK_PREFERRED_URL_SCHEME', 'https')

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# User Model
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)

# Signal Record Model for Performance Tracking
class SignalRecord(db.Model):
    __bind_key__ = 'signals'
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(50), nullable=False)
    direction = db.Column(db.String(10), nullable=False)
    entry_price = db.Column(db.Float, nullable=False)
    tp1_price = db.Column(db.Float, nullable=False)
    tp2_price = db.Column(db.Float, nullable=False)
    sl_price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None))
    status = db.Column(db.String(20), default='PENDING')  # PENDING, TP1_PARTIAL, TP, TP2, SL, MANUAL_CLOSE
    exit_price = db.Column(db.Float, nullable=True)
    exit_timestamp = db.Column(db.DateTime, nullable=True)
    pnl_pct = db.Column(db.Float, nullable=True)
    lights_count = db.Column(db.Integer, default=0)
    # New columns for v2 analytics
    atr_value = db.Column(db.Float, nullable=True)
    rsi_value = db.Column(db.Float, nullable=True)
    trend_aligned = db.Column(db.Boolean, nullable=True)
    smc_zone = db.Column(db.String(20), nullable=True)
    rr_ratio = db.Column(db.Float, nullable=True)
    # New column for partial take profit
    partial_tp1 = db.Column(db.Boolean, default=False)
    # Best price reached after TP1 — drives the trailing-runner stop.
    runner_peak = db.Column(db.Float, nullable=True)
    # ── Strategy management model + live trailing-stop state (S3/S4) ──
    # Which backtest.STRATEGIES key produced this trade (default/trend_breakout/
    # trend_trailing/adaptive_trend). NULL on legacy rows = the original S1 ('default').
    strategy = db.Column(db.String(20), default='default')
    # 'bracket' (S1/S2: fixed SL + TP1/TP2) or 'trailing' (S3/S4: ratcheted stop,
    # no TP). Existing rows default to 'bracket' since they were all Strategy 1.
    manage = db.Column(db.String(10), default='bracket')
    trail_dist = db.Column(db.Float, nullable=True)   # Chandelier ATR distance
    peak = db.Column(db.Float, nullable=True)         # high-water mark (long)
    trough = db.Column(db.Float, nullable=True)       # low-water mark (short)
    cur_stop = db.Column(db.Float, nullable=True)     # current ratcheted stop price
    last_bar_ts = db.Column(db.BigInteger, nullable=True)  # last 1h bar ts managed (ms)

# Admin required decorator
def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# Safer Binance REST client for web-side price refreshes
rest_client = SafeBinanceClient(
    min_rest_interval=REST_API_MIN_INTERVAL_SEC,
    max_retries=REST_API_MAX_RETRIES,
)

# Data file path (shared with bot.py)
DATA_FILE = os.path.join(os.path.dirname(__file__), "scan_results.json")
CLEAR_QUEUE_REQUEST_FILE = os.path.join(os.path.dirname(__file__), "clear_queue.request")
ALLOW_PUBLIC_REGISTRATION = os.getenv('ALLOW_PUBLIC_REGISTRATION', 'false').lower() == 'true'


def ensure_db_columns():
    """Ensure all new columns are added to the database"""
    try:
        with app.app_context():
            # Check if partial_tp1 column exists
            from sqlalchemy import inspect
            signals_engine = db.engines['signals']
            inspector = inspect(signals_engine)
            columns = [col['name'] for col in inspector.get_columns('signal_record')]

            if 'partial_tp1' not in columns:
                # Add partial_tp1 column
                with signals_engine.connect() as conn:
                    conn.execute(db.text("ALTER TABLE signal_record ADD COLUMN partial_tp1 BOOLEAN DEFAULT FALSE;"))
                    conn.commit()
                print("Added partial_tp1 column to signal_record table")
                
    except Exception as e:
        print(f"Error checking/adding DB columns: {e}")


def scan_age_seconds(last_update: str):
    """Seconds since the last scan, parsed from the bot's local-time stamp
    ("YYYY-MM-DD HH:MM:SS"). Returns None if it can't be parsed. The bot and web
    run on the same machine/timezone, so a naive local comparison is correct."""
    if not last_update or last_update in ("Never", "Error loading"):
        return None
    try:
        when = datetime.strptime(last_update, "%Y-%m-%d %H:%M:%S")
        return max(0, int((datetime.now() - when).total_seconds()))
    except (ValueError, TypeError):
        return None


def default_scan_data(last_update: str = "Never") -> dict:
    return {
        "last_update": last_update,
        "status": "idle",
        "signals": [],
        "scanned_symbols": 0,
        "scanned_symbols_list": [],
    }


def load_data():
    if not os.path.exists(DATA_FILE):
        return default_scan_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_scan_data("Error loading")

# --- Signal funnel -------------------------------------------------------
# Ordered gates a setup must clear before it can queue. Each entry is
# (key, human label, substring that appears in record_status_reason when the
# gate FAILS). The order mirrors the bot's logical screening so the funnel is a
# genuine "survivors after each stage" view, not an arbitrary ranking.
FUNNEL_GATES = [
    ("tradeable",   "Tradeable (top-N by volume)",      "watch-only"),
    ("lights",      "Enough effective lights",          "effective lights below"),
    ("base_lights", "Enough base strategy lights",      "base strategy lights"),
    ("ema50",       "EMA50 trend aligned",              "EMA50 trend not aligned"),
    ("ema200",      "EMA200 trend aligned",             "EMA200 trend not aligned"),
    ("trend_4h",    "4H trend aligned",                 "4H trend not aligned"),
    ("rsi_mom",     "RSI momentum aligned",             "RSI momentum not aligned"),
    ("score_edge",  "Bull/bear score edge",             "score edge too weak"),
    ("rsi_filter",  "RSI not at an extreme",            "blocked: RSI"),
    ("smc",         "SMC zone allows direction",        "SMC zone blocked"),
    ("btc_regime",  "BTC regime allows direction",      "BTC regime"),
    ("volume",      "Volume gate (≥ multiplier)",       "volume gate failed"),
    ("not_extended","Not over-extended past target",    "too extended"),
    ("trade_plan",  "Trade plan available",             "trade plan not available"),
]


def build_funnel(data: dict) -> dict:
    """Turn the latest scan into a drop-off funnel + a 'which gate blocks most'
    leaderboard, so 'why no trade?' is answerable at a glance."""
    # Fresh setups only: those evaluated and rejected this scan, or currently
    # queued. Signals already in a trade (status "trade active" / "TP target hit"
    # / "SL hit") aren't seeking entry, so they'd distort the screening funnel.
    all_signals = data.get("signals", []) or []
    signals = [s for s in all_signals
               if s.get("trade_queued") or s.get("trade_status") == "rejected"]
    total = len(signals)

    def reason_of(s):
        return s.get("record_status_reason", "") or ""

    # Sequential funnel: survivors that have not yet failed any gate up to stage i.
    survivors = list(signals)
    stages = [{"key": "scanned", "label": "Scanned this pass",
               "survivors": total, "dropped": 0}]
    for key, label, needle in FUNNEL_GATES:
        before = len(survivors)
        survivors = [s for s in survivors if needle not in reason_of(s)]
        stages.append({
            "key": key, "label": label,
            "survivors": len(survivors), "dropped": before - len(survivors),
        })

    queued = sum(1 for s in signals if s.get("trade_queued"))
    stages.append({"key": "queued", "label": "Queued (waiting for entry)",
                   "survivors": queued, "dropped": max(0, len(survivors) - queued)})

    # Independent blocker leaderboard: how many of ALL signals each gate blocks
    # (a signal can fail several). This is the 'which knob matters' view.
    leaderboard = []
    for key, label, needle in FUNNEL_GATES:
        n = sum(1 for s in signals if needle in reason_of(s))
        leaderboard.append({
            "key": key, "label": label, "count": n,
            "pct": round(100 * n / total, 1) if total else 0.0,
        })
    leaderboard.sort(key=lambda r: r["count"], reverse=True)

    # Closest-to-qualifying tradeable setups (fewest failed gates).
    def n_failed(s):
        return sum(1 for _, _, needle in FUNNEL_GATES if needle in reason_of(s))
    tradeable = [s for s in signals if s.get("tradeable")]
    closest = sorted(tradeable, key=n_failed)
    closest_rows = []
    for s in closest[:8]:
        reason = reason_of(s)
        body = reason.split("rejected on latest scan:")[-1].strip() if "rejected" in reason else reason
        closest_rows.append({
            "symbol": s.get("symbol", "?"),
            "direction": (s.get("direction") or "").upper(),
            "lights": s.get("effective_lights"),
            "fails": n_failed(s),
            "reason": body[:160],
            "tv_url": s.get("tv_url"),
        })

    return {
        "last_update": data.get("last_update"),
        "btc_regime": data.get("btc_regime", "unknown"),
        "total": total,
        "tradeable": len(tradeable),
        "queued": queued,
        "stages": stages,
        "leaderboard": leaderboard,
        "closest": closest_rows,
        "thresholds": {
            "volume_mult": VOLUME_GATE_MULTIPLIER,
            "min_lights": MIN_LIGHTS_FOR_RECORD,
            "min_base_lights": MIN_BASE_LIGHTS,
            "top_symbol_limit": TOP_SYMBOL_LIMIT,
        },
    }


def build_top_entries(data: dict, n: int = 5) -> dict:
    """The best entry candidates from the latest scan, ranked by how well they
    fit the strategy. Queued setups (cleared every gate, resting at entry) rank
    first as 'ready'; high-confluence near-misses follow as 'watch', each tagged
    with what still blocks them. Honest: a watch item is NOT a confirmed entry."""
    signals = data.get("signals", []) or []
    cands = []
    for s in signals:
        if not s.get("tradeable"):
            continue
        # Only fresh setups — skip anything already in/through a trade.
        if not (s.get("trade_queued") or s.get("trade_status") == "rejected"):
            continue
        reason = s.get("record_status_reason", "") or ""
        queued = bool(s.get("trade_queued"))
        lights = s.get("effective_lights") or 0
        # Quality bar: ready setups always; watch items need real confluence.
        if not queued and lights < 4:
            continue
        fails = sum(1 for _, _, needle in FUNNEL_GATES if needle in reason)
        blockers = [p.strip() for p in reason.split("rejected on latest scan:")[-1].split(";")
                    if p.strip()] if (not queued and "rejected" in reason) else []
        cands.append({
            "symbol": s.get("symbol", "?"),
            "direction": (s.get("direction") or "").upper(),
            "lights": lights,
            "conviction": s.get("conviction"),
            "rsi": s.get("rsi"),
            "entry": s.get("entry"),
            "tp": s.get("tp"),
            "sl": s.get("sl"),
            "queued": queued,
            "fails": fails,
            "blockers": blockers[:2],
            "tv_url": s.get("tv_url"),
        })
    # Rank: ready (queued) first, then more lights, then fewer failed gates.
    cands.sort(key=lambda c: (not c["queued"], -c["lights"], c["fails"]))
    return {
        "last_update": data.get("last_update"),
        "btc_regime": data.get("btc_regime", "unknown"),
        "ready": sum(1 for c in cands if c["queued"]),
        "entries": cands[:n],
    }


def format_price(val: float) -> str:
    if val is None: return "N/A"
    if val < 0.0001: return f"{val:.8f}"
    if val < 0.01: return f"{val:.6f}"
    if val < 1.0: return f"{val:.4f}"
    return f"{val:.2f}"


def safe_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_live_tickers(symbols):
    unique_symbols = []
    seen = set()
    for symbol in symbols:
        if not symbol or symbol in seen:
            continue
        unique_symbols.append(symbol)
        seen.add(symbol)

    if not unique_symbols:
        return {}

    try:
        return rest_client.call("fetch_tickers", unique_symbols)
    except RateLimitCooldownError as exc:
        print(f"Live-price refresh skipped during REST cooldown: {exc}")
        return {}
    except Exception as exc:
        if "same type" not in str(exc).lower():
            print(f"Live-price refresh failed: {exc}")
            return {}
        try:
            snapshot = rest_client.call("fetch_tickers")
            return {symbol: snapshot[symbol] for symbol in unique_symbols if symbol in snapshot}
        except Exception as snapshot_exc:  # noqa: BLE001
            print(f"Live-price snapshot fallback failed: {snapshot_exc}")
            return {}


def csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_hex(16)
        session['_csrf_token'] = token
    return token


def validate_csrf():
    expected = session.get('_csrf_token')
    supplied = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
    if not expected or expected != supplied:
        abort(400, description="Invalid CSRF token")


def request_queue_clear():
    try:
        with open(CLEAR_QUEUE_REQUEST_FILE, "w", encoding="utf-8") as f:
            f.write(str(datetime.now().timestamp()))
    except OSError as exc:
        print(f"Error writing queue-clear request: {exc}")


def clear_queued_signals_from_scan_data():
    data = load_data()
    signals = data.get("signals", [])
    if not isinstance(signals, list):
        return
    data["signals"] = [
        s for s in signals
        if not (
            isinstance(s, dict)
            and (s.get("trade_status") == "queued" or (s.get("trade_queued") and not s.get("trade_recorded")))
        )
    ]
    temp_path = DATA_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, default=_default_json_encoder)
    os.replace(temp_path, DATA_FILE)


def scan_record_trade_status(record) -> str | None:
    status = (getattr(record, "status", "") or "").upper()
    if status == "PENDING":
        return "active"
    if status == "TP1_PARTIAL":
        return "tp1_partial"
    if status in {"TP", "TP1", "TP2"}:
        return "tp"
    if status == "SL":
        return "sl"
    if status == "MANUAL_CLOSE":
        return "manual_close"
    return None


def scan_record_trade_reason(record) -> str:
    status = (getattr(record, "status", "") or "").upper()
    pnl = getattr(record, "pnl_pct", None)
    if status == "TP1_PARTIAL":
        booked = f"+{pnl:.1f}% banked" if pnl is not None else "50% closed"
        return f"Partial take profit at TP1 ({booked}), trailing rest to TP2"
    if status == "PENDING":
        return "trade active"
    trade_status = scan_record_trade_status(record)
    if trade_status == "tp":
        return f"TP hit ({'+' if pnl and pnl > 0 else ''}{pnl:.1f}%)" if pnl is not None else "TP target hit"
    if trade_status == "sl":
        return f"SL hit ({pnl:.1f}%)" if pnl is not None else "SL stop hit"
    if trade_status == "manual_close":
        return "Trade manually closed by user"
    return "trade recorded"


def load_recent_record_states(max_closed_hours: int = 24) -> dict[tuple[str, str], dict]:
    state_map: dict[tuple[str, str], dict] = {}
    now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    cutoff = now - timedelta(hours=max_closed_hours)

    records = SignalRecord.query.order_by(SignalRecord.timestamp.desc()).all()
    for record in records:
        trade_status = scan_record_trade_status(record)
        if not trade_status:
            continue

        if trade_status != "active" and trade_status != "tp1_partial":
            closed_at = record.exit_timestamp or record.timestamp
            if not closed_at or closed_at < cutoff:
                continue

        key = (record.symbol, record.direction)
        if key in state_map:
            continue

        # Determine which TP/SL to show
        status = (getattr(record, "status", "") or "").upper()
        if status == "TP1_PARTIAL":
            # Show TP2 as target and entry as stop
            tp_price = record.tp2_price
            sl_price = record.entry_price
        else:
            tp_price = record.tp1_price
            sl_price = record.sl_price

        state_map[key] = {
            "trade_status": trade_status,
            "record_status_reason": scan_record_trade_reason(record),
            "entry": format_price(record.entry_price) if record.entry_price is not None else None,
            "tp": format_price(tp_price) if tp_price is not None else None,
            "tp2": format_price(record.tp2_price) if record.tp2_price is not None else None,
            "sl": format_price(sl_price) if sl_price is not None else None,
            "exit_price": format_price(record.exit_price) if record.exit_price is not None else None,
        }

    return state_map


def sync_record_states_in_scan_data() -> None:
    data = load_data()
    signals = data.get("signals", [])
    if not isinstance(signals, list):
        return

    state_map = load_recent_record_states()
    for item in signals:
        if not isinstance(item, dict):
            continue

        key = (item.get("symbol"), item.get("direction"))
        state = state_map.get(key)
        if state:
            item["trade_status"] = state["trade_status"]
            item["trade_queued"] = False
            item["trade_recorded"] = True
            item["record_status_reason"] = state["record_status_reason"]
            item["entry"] = state["entry"]
            item["tp"] = state["tp"]
            item["tp2"] = state.get("tp2")
            item["sl"] = state["sl"]
            if state.get("exit_price") is not None:
                item["exit_price"] = state["exit_price"]
            else:
                item.pop("exit_price", None)
            continue

        if (item.get("trade_status") or "").lower() not in {"active", "pending", "tp", "sl", "manual_close", "tp1_partial"}:
            continue

        item.pop("trade_status", None)
        item["trade_queued"] = False
        item["trade_recorded"] = False
        item.pop("record_status_reason", None)
        item.pop("entry", None)
        item.pop("tp", None)
        item.pop("sl", None)
        item.pop("exit_price", None)

    temp_path = DATA_FILE + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, default=_default_json_encoder)
    os.replace(temp_path, DATA_FILE)


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}


@app.before_request
def protect_post_requests():
    if request.method == "POST":
        validate_csrf()


def is_win_record(record) -> bool:
    return record.status in ['TP', 'TP1', 'TP2']


def is_loss_record(record) -> bool:
    return record.status == 'SL'


def is_closed_record(record) -> bool:
    return record.status in ['TP', 'TP1', 'TP2', 'SL', 'MANUAL_CLOSE']


def generate_tradingview_url(symbol: str) -> str:
    base_symbol = symbol.split("/")[0].split(":")[0]
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{base_symbol}{QUOTE_ASSET}.P"


def build_win_rate_stats(records):
    wins = sum(1 for r in records if is_win_record(r))
    losses = sum(1 for r in records if is_loss_record(r))
    closed = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "closed": closed,
        "win_rate": round((wins / closed * 100), 2) if closed > 0 else 0.0,
    }


def get_qualified_records(records):
    return [r for r in records if (r.lights_count or 0) >= MIN_LIGHTS_FOR_RECORD]


def _edge_block(group):
    """Win-rate + expectancy summary for a list of closed records."""
    wins = sum(1 for r in group if is_win_record(r))
    losses = sum(1 for r in group if is_loss_record(r))
    decided = wins + losses
    pnls = [r.pnl_pct for r in group if r.pnl_pct is not None]
    expectancy = (sum(pnls) / len(pnls)) if pnls else 0.0
    return {
        "total": decided,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / decided * 100, 1) if decided else 0.0,
        "expectancy": round(expectancy, 2),
        "total_pnl": round(sum(pnls), 2),
    }


def build_circuit_state(records):
    """Daily circuit-breaker state, mirroring bot.py's enforced logic exactly:
    the bot halts NEW trades when, for the current day (Taiwan time), the count
    of LOSING (SL) trades reaches DAILY_MAX_LOSSES, OR the summed PnL of those
    losing trades falls to DAILY_MAX_DRAWDOWN_PCT. (Wins do NOT offset the
    drawdown trigger — it tracks loss magnitude only, like record_daily_loss.)
    """
    closed = [r for r in records if is_win_record(r) or is_loss_record(r)]
    now_tw = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    today = now_tw.date()
    todays = [r for r in closed if (r.exit_timestamp or r.timestamp)
              and (r.exit_timestamp or r.timestamp).date() == today]

    todays_losses = [r for r in todays if is_loss_record(r)]
    losses_today = len(todays_losses)
    # Sum of losing-trade PnL only (negative) — matches the bot's drawdown_pct.
    loss_drawdown = round(sum((r.pnl_pct or 0.0) for r in todays_losses), 2)
    # Net P&L of all closed trades today (wins + losses) — shown for context.
    today_net_pnl = round(sum((r.pnl_pct or 0.0) for r in todays), 2)

    halted_losses = losses_today >= DAILY_MAX_LOSSES
    halted_dd = loss_drawdown <= DAILY_MAX_DRAWDOWN_PCT
    return {
        "today_trades": len(todays),
        "today_net_pnl": today_net_pnl,
        "losses_today": losses_today,
        "loss_drawdown": loss_drawdown,
        "max_losses": DAILY_MAX_LOSSES,
        "max_drawdown_pct": DAILY_MAX_DRAWDOWN_PCT,
        "halted": bool(halted_losses or halted_dd),
        "halt_reason": ("loss limit reached" if halted_losses
                        else "daily drawdown hit" if halted_dd else ""),
    }


def build_performance_analytics(records):
    """Compute drawdown, edge breakdowns, calendar P&L, time-of-day and the
    daily circuit-breaker state from the qualified records. Pure read-only —
    everything is derived from existing SignalRecord fields."""
    closed = [r for r in records if is_win_record(r) or is_loss_record(r)]
    closed.sort(key=lambda r: (r.exit_timestamp or r.timestamp))

    # ── Equity curve + drawdown (underwater) ────────────────────────────
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    peak_date = None
    curve = []
    for r in closed:
        equity += (r.pnl_pct or 0.0)
        if equity >= peak:
            peak = equity
            peak_date = r.exit_timestamp or r.timestamp
        dd = equity - peak  # <= 0
        max_dd = min(max_dd, dd)
        when = (r.exit_timestamp or r.timestamp)
        curve.append({
            "t": when.strftime("%Y-%m-%d %H:%M") if when else "",
            "equity": round(equity, 2),
            "drawdown": round(dd, 2),
        })
    current_dd = curve[-1]["drawdown"] if curve else 0.0
    # Days underwater = since the equity last printed a new high.
    dd_duration_days = 0.0
    if current_dd < 0 and peak_date and closed:
        last_date = closed[-1].exit_timestamp or closed[-1].timestamp
        if last_date:
            dd_duration_days = round((last_date - peak_date).total_seconds() / 86400, 1)

    # ── Edge breakdowns ─────────────────────────────────────────────────
    zones = {}
    for r in closed:
        zones.setdefault(r.smc_zone or "None", []).append(r)
    edge_by_smc = {k: _edge_block(v) for k, v in zones.items()}

    edge_by_trend = {
        "Aligned": _edge_block([r for r in closed if r.trend_aligned is True]),
        "Contrary": _edge_block([r for r in closed if r.trend_aligned is False]),
    }

    # ── LONG vs SHORT split (includes still-open trades so you can see shorts
    #    firing before they close) ─────────────────────────────────────────
    def _dir_block(direction):
        rs = [r for r in records if r.direction == direction]
        decided = [r for r in rs if is_win_record(r) or is_loss_record(r)]
        wins = sum(1 for r in decided if is_win_record(r))
        losses = sum(1 for r in decided if is_loss_record(r))
        open_n = len(rs) - len(decided)
        pnl = sum(r.pnl_pct for r in rs if r.pnl_pct is not None)
        block = _edge_block(decided)  # win_rate + expectancy over closed
        block.update({
            "signals": len(rs),
            "open": open_n,
            "wins": wins,
            "losses": losses,
            "total_pnl": round(pnl, 2),
        })
        return block
    direction_split = {"LONG": _dir_block("LONG"), "SHORT": _dir_block("SHORT")}

    # ── Calendar P&L (by realised/exit date) ────────────────────────────
    calendar = {}
    for r in closed:
        when = r.exit_timestamp or r.timestamp
        if not when:
            continue
        key = when.strftime("%Y-%m-%d")
        calendar[key] = round(calendar.get(key, 0.0) + (r.pnl_pct or 0.0), 2)

    # ── Time-of-day / day-of-week edge (by entry time) ──────────────────
    dow_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_dow = {i: [] for i in range(7)}
    by_hour = {i: [] for i in range(24)}
    for r in closed:
        if not r.timestamp:
            continue
        by_dow[r.timestamp.weekday()].append(r)
        by_hour[r.timestamp.hour].append(r)
    weekday_stats = [{"label": dow_labels[i], **_edge_block(by_dow[i])} for i in range(7)]
    hour_stats = [{"label": f"{i:02d}", **_edge_block(by_hour[i])} for i in range(24)]

    circuit = build_circuit_state(records)

    # ── Total-PnL breakdown (every record that contributes to the number) ──
    contributors = []
    for r in records:
        if r.pnl_pct is None:
            continue
        contributors.append({
            "symbol": r.symbol.split("/")[0],
            "direction": r.direction,
            "status": r.status,
            "partial": r.status == "TP1_PARTIAL",
            "pnl": round(r.pnl_pct, 2),
        })
    contributors.sort(key=lambda x: x["pnl"], reverse=True)
    pnl_realized = round(sum(c["pnl"] for c in contributors if not c["partial"]), 2)
    pnl_open = round(sum(c["pnl"] for c in contributors if c["partial"]), 2)
    pnl_total = round(pnl_realized + pnl_open, 2)

    # ── Per conviction-tier breakdown (which trades won / lost / still in) ──
    def _outcome(r):
        if r.status in ("TP", "TP1", "TP2"):
            return "WIN"
        if r.status == "SL":
            return "LOSS"
        if r.status == "TP1_PARTIAL":
            return "PARTIAL"
        if r.status == "MANUAL_CLOSE":
            return "CLOSED"
        return "PENDING"

    def _tier_list(predicate):
        # ordered: open/in-trade first, then wins, then losses; newest first within
        order = {"PARTIAL": 0, "PENDING": 1, "WIN": 2, "CLOSED": 3, "LOSS": 4}
        items = []
        for r in records:
            if not predicate(r.lights_count or 0):
                continue
            items.append({
                "symbol": r.symbol.split("/")[0],
                "direction": r.direction,
                "status": r.status,
                "outcome": _outcome(r),
                "pnl": round(r.pnl_pct, 2) if r.pnl_pct is not None else None,
                "ts": r.timestamp.timestamp() if r.timestamp else 0,
            })
        items.sort(key=lambda x: (order.get(x["outcome"], 9), -x["ts"]))
        return items

    tier_breakdown = {
        "4": _tier_list(lambda n: n == 4),
        "5": _tier_list(lambda n: n == 5),
        "6": _tier_list(lambda n: n >= 6),
    }

    return {
        "curve": curve,
        "max_drawdown": round(max_dd, 2),
        "current_drawdown": round(current_dd, 2),
        "dd_duration_days": dd_duration_days,
        "edge_by_smc": edge_by_smc,
        "edge_by_trend": edge_by_trend,
        "direction_split": direction_split,
        "calendar": calendar,
        "weekday_stats": weekday_stats,
        "hour_stats": hour_stats,
        "circuit": circuit,
        "pnl_contributors": contributors,
        "pnl_realized": pnl_realized,
        "pnl_open": pnl_open,
        "pnl_total": pnl_total,
        "tier_breakdown": tier_breakdown,
    }


def build_dashboard_summary(scan_data=None):
    if scan_data is None:
        scan_data = load_data()

    signals = [s for s in scan_data.get("signals", []) if isinstance(s, dict)]
    total_signals = len(signals)
    long_signals = sum(1 for s in signals if s.get("direction") == "LONG")
    short_signals = sum(1 for s in signals if s.get("direction") == "SHORT")
    queued_count = sum(1 for s in signals if (s.get("trade_status") or "").lower() == "queued")
    active_signal_count = sum(1 for s in signals if (s.get("trade_status") or "").lower() in {"active", "pending", "tp1_partial"})
    rejected_count = sum(1 for s in signals if (s.get("trade_status") or "").lower() == "rejected")
    tp_count = sum(1 for s in signals if (s.get("trade_status") or "").lower() == "tp")
    sl_count = sum(1 for s in signals if (s.get("trade_status") or "").lower() == "sl")
    max_conviction_count = sum(1 for s in signals if safe_float(s.get("effective_lights"), 0) >= 5)
    high_conviction_count = sum(1 for s in signals if safe_float(s.get("effective_lights"), 0) >= 4)

    rsi_values = [safe_float(s.get("rsi"), None) for s in signals if s.get("rsi") not in (None, "")]
    rsi_values = [r for r in rsi_values if r is not None]
    avg_rsi = round(sum(rsi_values) / len(rsi_values), 1) if rsi_values else 0.0
    overbought_count = sum(1 for r in rsi_values if r >= RSI_UPPER_THRESHOLD)
    oversold_count = sum(1 for r in rsi_values if r <= RSI_LOWER_THRESHOLD)

    total_bias = long_signals + short_signals
    long_signal_pct = round((long_signals / total_bias) * 100, 1) if total_bias else 0.0
    short_signal_pct = round((short_signals / total_bias) * 100, 1) if total_bias else 0.0
    if long_signals > short_signals:
        signal_bias = "LONG lean"
    elif short_signals > long_signals:
        signal_bias = "SHORT lean"
    else:
        signal_bias = "Balanced"

    records = get_qualified_records(SignalRecord.query.order_by(SignalRecord.timestamp.desc()).all())
    pending_records = [r for r in records if r.status in ('PENDING', 'TP1_PARTIAL')]
    closed_records = [r for r in records if is_win_record(r) or is_loss_record(r)]
    recent_closed = sorted(closed_records, key=lambda r: r.exit_timestamp or r.timestamp, reverse=True)[:20]
    recent_stats = build_win_rate_stats(recent_closed)

    avg_win = round(
        sum(r.pnl_pct for r in closed_records if is_win_record(r) and r.pnl_pct is not None) / max(1, sum(1 for r in closed_records if is_win_record(r))),
        2,
    ) if any(is_win_record(r) and r.pnl_pct is not None for r in closed_records) else 0.0
    avg_loss = round(
        sum(r.pnl_pct for r in closed_records if is_loss_record(r) and r.pnl_pct is not None) / max(1, sum(1 for r in closed_records if is_loss_record(r))),
        2,
    ) if any(is_loss_record(r) and r.pnl_pct is not None for r in closed_records) else 0.0

    def build_subset_stats(subset):
        stats = build_win_rate_stats(subset)
        return {
            "wins": stats["wins"],
            "losses": stats["losses"],
            "closed": stats["closed"],
            "win_rate": round(stats["win_rate"], 1),
        }

    long_records = [r for r in closed_records if r.direction == "LONG"]
    short_records = [r for r in closed_records if r.direction == "SHORT"]
    lights_4_records = [r for r in closed_records if (r.lights_count or 0) == 4]
    lights_5_records = [r for r in closed_records if (r.lights_count or 0) == 5]
    lights_6plus_records = [r for r in closed_records if (r.lights_count or 0) >= 6]

    direction_stats = {
        "long": build_subset_stats(long_records),
        "short": build_subset_stats(short_records),
    }
    conviction_stats = {
        "lights_4": build_subset_stats(lights_4_records),
        "lights_5": build_subset_stats(lights_5_records),
        "lights_6plus": build_subset_stats(lights_6plus_records),
    }

    best_edge_label = "No closed trades yet"
    best_edge_note = "Need more history"
    candidates = []
    if direction_stats["long"]["closed"] > 0:
        candidates.append(("LONG side", direction_stats["long"]["win_rate"], direction_stats["long"]["closed"]))
    if direction_stats["short"]["closed"] > 0:
        candidates.append(("SHORT side", direction_stats["short"]["win_rate"], direction_stats["short"]["closed"]))
    if conviction_stats["lights_4"]["closed"] > 0:
        candidates.append(("4-light setups", conviction_stats["lights_4"]["win_rate"], conviction_stats["lights_4"]["closed"]))
    if conviction_stats["lights_5"]["closed"] > 0:
        candidates.append(("5-light setups", conviction_stats["lights_5"]["win_rate"], conviction_stats["lights_5"]["closed"]))
    if conviction_stats["lights_6plus"]["closed"] > 0:
        candidates.append(("6+ light setups", conviction_stats["lights_6plus"]["win_rate"], conviction_stats["lights_6plus"]["closed"]))
    if candidates:
        best_name, best_wr, best_closed = max(candidates, key=lambda item: (item[1], item[2]))
        best_edge_label = best_name
        best_edge_note = f"{best_wr:.1f}% win rate over {best_closed} closed trades"

    return {
        "btc_regime": scan_data.get("btc_regime", "neutral"),
        "signals": {
            "total": total_signals,
            "long": long_signals,
            "short": short_signals,
            "queued": queued_count,
            "active": active_signal_count,
            "rejected": rejected_count,
            "tp": tp_count,
            "sl": sl_count,
            "avg_rsi": avg_rsi,
            "overbought": overbought_count,
            "oversold": oversold_count,
            "max_conviction": max_conviction_count,
            "high_conviction": high_conviction_count,
            "long_pct": long_signal_pct,
            "short_pct": short_signal_pct,
            "bias": signal_bias,
        },
        "trades": {
            "qualified_total": len(records),
            "pending": len(pending_records),
            "closed": len(closed_records),
            "recent_closed": recent_stats["closed"],
            "recent_win_rate": round(recent_stats["win_rate"], 1),
            "recent_wins": recent_stats["wins"],
            "recent_losses": recent_stats["losses"],
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "best_edge_label": best_edge_label,
            "best_edge_note": best_edge_note,
        },
        "direction": direction_stats,
        "conviction": conviction_stats,
    }

def clean_signals(signals, records):
    # Collect symbols that already have active/pending records
    active_symbols = set()
    for rec in records:
        if rec.status in ["PENDING", "TP1_PARTIAL"]:
            active_symbols.add(rec.symbol)
    
    cleaned_signals = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        
        is_queued = signal.get("trade_status") == "queued" or (
            not signal.get("trade_status") and signal.get("trade_queued") and not signal.get("trade_recorded")
        )
        
        # Skip queued signals if symbol already has active/pending record
        if is_queued and signal.get("symbol") in active_symbols:
            continue
            
        cleaned_signals.append(signal)
    
    return cleaned_signals

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

@app.route("/register", methods=['GET', 'POST'])
def register():
    if not ALLOW_PUBLIC_REGISTRATION:
        if request.method == 'POST':
            flash('Public registration is disabled. Please contact the admin.', 'danger')
            return redirect(url_for('login'))
        return render_template("register.html", registration_enabled=False)

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user_exists = User.query.filter_by(username=username).first()
        if user_exists:
            flash('Username already exists.', 'danger')
            return redirect(url_for('register'))
        
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        
        # The first user registered becomes an admin automatically
        is_first_user = User.query.count() == 0
        new_user = User(username=username, password=hashed_password, is_admin=is_first_user)
        
        db.session.add(new_user)
        db.session.commit()
        
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))
    
    return render_template("register.html", registration_enabled=True)

@app.route("/admin/users")
@admin_required
def admin_users():
    users = User.query.all()
    return render_template("admin_users.html", users=users)

@app.route("/admin/create_user", methods=['POST'])
@admin_required
def create_user():
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    is_admin = request.form.get('is_admin') == 'on'

    if not username or not password:
        flash("Username and password are required.", "danger")
        return redirect(url_for('admin_users'))
    if User.query.filter_by(username=username).first():
        flash("Username already exists.", "danger")
        return redirect(url_for('admin_users'))

    hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
    db.session.add(User(username=username, password=hashed_password, is_admin=is_admin))
    db.session.commit()
    flash(f"User {username} created successfully.", "success")
    return redirect(url_for('admin_users'))


@app.route("/admin/delete_user/<int:user_id>", methods=['POST'])
@admin_required
def delete_user(user_id):
    if user_id == current_user.id:
        flash("You cannot delete yourself!", "danger")
        return redirect(url_for('admin_users'))
    
    user = db.session.get(User, user_id)
    if user:
        username = user.username
        db.session.delete(user)
        db.session.commit()
        flash(f"User {username} has been deleted.", "success")
    else:
        flash("User not found.", "danger")

    return redirect(url_for('admin_users'))

def _perf_selected_strategy():
    """Which strategy the Performance page scopes its stats to: ?strategy=<key|all>,
    defaulting to the strategy the live bot is RUNNING (the 'dashboard strategy')."""
    import backtest as BT
    arg = (request.args.get("strategy") or "").strip()
    if arg == "all" or arg in BT.STRATEGIES:
        return arg
    st = _live_strategy_state()
    return st.get("running") or st.get("saved") or "default"


def _filter_records_by_strategy(records, strategy):
    """Scope SignalRecords to one strategy. Legacy rows (NULL strategy) count as
    'default' (S1), since the live bot only ran S1 before strategy tagging existed."""
    if not strategy or strategy == "all":
        return records
    return [r for r in records if (getattr(r, "strategy", None) or "default") == strategy]


def _perf_strategy_context(selected):
    import backtest as BT
    return {
        "selected": selected,
        "name": ("All strategies" if selected == "all"
                 else BT.STRATEGIES.get(selected, {}).get("name", selected)),
        "strategies": [{"key": k, "name": v["name"]} for k, v in BT.STRATEGIES.items()],
    }


@app.route("/performance")
@login_required
def performance():
    try:
        strategy = _perf_selected_strategy()
        all_records = SignalRecord.query.order_by(SignalRecord.timestamp.desc()).all()
        all_records = _filter_records_by_strategy(all_records, strategy)
        records = sorted(
            get_qualified_records(all_records),
            key=lambda r: ((r.lights_count or 0), r.timestamp),
            reverse=True,
        )
        # Add TradingView URL to each record
        for r in records:
            r.tv_url = generate_tradingview_url(r.symbol)

        # Calculate stats for qualified 4/5-light records only
        total = len(records)
        win_records = [r for r in records if is_win_record(r)]
        loss_records = [r for r in records if is_loss_record(r)]
        wins = len(win_records)
        losses = len(loss_records)
        pending = len([r for r in records if r.status in ('PENDING', 'TP1_PARTIAL')])
        
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        total_pnl = sum([r.pnl_pct for r in records if r.pnl_pct is not None])
        
        # Advanced stats
        avg_win = sum([r.pnl_pct for r in win_records if r.pnl_pct is not None]) / wins if wins > 0 else 0
        avg_loss = sum([r.pnl_pct for r in loss_records if r.pnl_pct is not None]) / losses if losses > 0 else 0

        # R-multiple metrics: realised reward measured in units of risk (R), where
        # R = the stop distance |entry - SL|. This is the true health of the edge.
        def _realized_r(r):
            if r.entry_price and r.sl_price and r.pnl_pct is not None:
                risk_pct = abs(r.entry_price - r.sl_price) / r.entry_price * 100
                if risk_pct > 0:
                    return r.pnl_pct / risk_pct
            return None
        win_rs = [v for v in (_realized_r(r) for r in win_records) if v is not None]
        loss_rs = [v for v in (_realized_r(r) for r in loss_records) if v is not None]
        avg_win_r = (sum(win_rs) / len(win_rs)) if win_rs else 0
        avg_loss_r = (sum(loss_rs) / len(loss_rs)) if loss_rs else 0
        decided_n = len(win_rs) + len(loss_rs)
        # Expectancy in R = average R you make per trade. Positive = real edge.
        expectancy_r = ((sum(win_rs) + sum(loss_rs)) / decided_n) if decided_n else 0
        gross_wins = sum([r.pnl_pct for r in win_records if r.pnl_pct is not None and r.pnl_pct > 0])
        gross_losses = abs(sum([r.pnl_pct for r in loss_records if r.pnl_pct is not None and r.pnl_pct < 0]))
        if gross_losses > 0:
            profit_factor = gross_wins / gross_losses
            profit_factor_display = f"{profit_factor:.2f}"
        elif gross_wins > 0:
            profit_factor = float('inf')
            profit_factor_display = "INF"
        else:
            profit_factor = 0
            profit_factor_display = "0.00"
        
        # Best/worst trade
        closed_records = [r for r in records if r.pnl_pct is not None]
        best_trade = max(closed_records, key=lambda r: r.pnl_pct).pnl_pct if closed_records else 0
        worst_trade = min(closed_records, key=lambda r: r.pnl_pct).pnl_pct if closed_records else 0
        
        # Average duration
        durations = []
        for r in closed_records:
            if r.exit_timestamp and r.timestamp:
                try:
                    dur = (r.exit_timestamp - r.timestamp).total_seconds() / 3600
                    durations.append(dur)
                except Exception as e:
                    print(f"Error calculating duration for record {r.id}: {e}")
        avg_duration = sum(durations) / len(durations) if durations else 0
        
        # Current streak
        streak = 0
        streak_type = ""
        # Sort by exit_timestamp if available, otherwise timestamp
        sorted_closed = sorted(closed_records, key=lambda x: x.exit_timestamp or x.timestamp, reverse=True)
        for r in sorted_closed:
            if is_win_record(r):
                if streak_type == "" or streak_type == "win":
                    streak += 1
                    streak_type = "win"
                else:
                    break
            elif is_loss_record(r):
                if streak_type == "" or streak_type == "loss":
                    streak += 1
                    streak_type = "loss"
                else:
                    break

        # Expectancy = average PnL the strategy returns per closed trade.
        # A positive number means each trade is, on average, profitable.
        decided = wins + losses
        expectancy = ((wins * avg_win) + (losses * avg_loss)) / decided if decided > 0 else 0
        # Average realised reward-to-risk across trades that recorded an RR.
        rr_vals = [r.rr_ratio for r in records if getattr(r, "rr_ratio", None)]
        avg_rr = sum(rr_vals) / len(rr_vals) if rr_vals else 0

        lights_4_records = [r for r in records if (r.lights_count or 0) == 4]
        lights_5_records = [r for r in records if (r.lights_count or 0) == 5]
        lights_6plus_records = [r for r in records if (r.lights_count or 0) >= 6]
        tier_stats = {
            "lights_4": {
                "total": len(lights_4_records),
                **build_win_rate_stats(lights_4_records),
            },
            "lights_5": {
                "total": len(lights_5_records),
                **build_win_rate_stats(lights_5_records),
            },
            "lights_6plus": {
                "total": len(lights_6plus_records),
                **build_win_rate_stats(lights_6plus_records),
            },
            "combined": {
                "total": len(records),
                **build_win_rate_stats(records),
            },
        }
        
        scan_data = load_data()
        
        # Clean queued signals
        cleaned_scan_signals = clean_signals(scan_data.get("signals", []), records)
        
        queued_signals = []
        for signal in cleaned_scan_signals:
            if not isinstance(signal, dict):
                continue
            is_queued = signal.get("trade_status") == "queued" or (
                not signal.get("trade_status") and signal.get("trade_queued") and not signal.get("trade_recorded")
            )
            if not is_queued:
                continue
            normalized = dict(signal)
            normalized["rsi"] = safe_float(signal.get("rsi"), 0.0)
            normalized["current_price"] = safe_float(signal.get("current_price"), 0.0)
            normalized["entry"] = safe_float(signal.get("entry"), 0.0)
            normalized["tp"] = safe_float(signal.get("tp"), 0.0)
            normalized["sl"] = safe_float(signal.get("sl"), 0.0)
            queued_signals.append(normalized)

        stats = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl or 0, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else profit_factor,
            "profit_factor_display": profit_factor_display,
            "best_trade": round(best_trade, 2),
            "worst_trade": round(worst_trade, 2),
            "avg_duration_hrs": round(avg_duration, 1),
            "streak": streak,
            "streak_type": streak_type,
            "expectancy": round(expectancy, 2),
            "avg_rr": round(avg_rr, 2),
            "avg_win_r": round(avg_win_r, 2),
            "avg_loss_r": round(avg_loss_r, 2),
            "expectancy_r": round(expectancy_r, 2),
            "decided": decided_n,
        }
        
        analytics = build_performance_analytics(records)

        return render_template(
            "performance.html",
            records=records,
            stats=stats,
            tier_stats=tier_stats,
            queued_signals=queued_signals,
            analytics=analytics,
            user=current_user,
            perf_strategy=_perf_strategy_context(strategy),
        )
    except Exception as e:
        print(f"Error in performance route: {e}")
        import traceback
        traceback.print_exc()
        return f"Error loading performance page: {e}", 500

@app.route("/admin/delete_signal/<int:signal_id>", methods=['POST'])
@admin_required
def delete_signal(signal_id):
    signal = db.session.get(SignalRecord, signal_id)
    if signal:
        db.session.delete(signal)
        db.session.commit()
        sync_record_states_in_scan_data()
        flash("Signal record deleted.", "success")
    else:
        flash("Signal not found.", "danger")
    return redirect(url_for('performance'))

@app.route("/admin/clear_all_signals", methods=['POST'])
@admin_required
def clear_all_signals():
    try:
        num_deleted = SignalRecord.query.delete()
        db.session.commit()
        request_queue_clear()
        clear_queued_signals_from_scan_data()
        sync_record_states_in_scan_data()
        flash(f"Successfully cleared {num_deleted} signal records.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error clearing signals: {e}", "danger")
    return redirect(url_for('performance'))


@app.route("/admin/manual_close_signal/<int:signal_id>", methods=['POST'])
@admin_required
def manual_close_signal(signal_id):
    try:
        signal = db.session.get(SignalRecord, signal_id)
        if not signal:
            flash("Signal not found.", "danger")
            return redirect(url_for('performance'))
        original_status = signal.status
        if original_status not in ('PENDING', 'TP1_PARTIAL'):
            flash("Only pending or partial signals can be manually closed.", "danger")
            return redirect(url_for('performance'))
        
        # Get current live price if possible
        current_price = None
        try:
            tickers = fetch_live_tickers([signal.symbol])
            if tickers and signal.symbol in tickers:
                current_price = tickers[signal.symbol].get('last')
        except Exception:
            pass
        
        signal.status = 'MANUAL_CLOSE'
        signal.exit_timestamp = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        signal.exit_price = current_price
        if original_status != 'TP1_PARTIAL':
            signal.pnl_pct = None  # Manual close is neutral: no win/loss AND no PnL contribution (unless partial TP was already hit)
        # If TP1_PARTIAL, keep the existing partial PnL
        db.session.commit()
        sync_record_states_in_scan_data()
        flash(f"Signal {signal_id} manually closed successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error closing signal: {e}", "danger")
    return redirect(url_for('performance'))


@app.route("/admin/remove_queued_signal", methods=['POST'])
@admin_required
def remove_queued_signal():
    try:
        symbol = request.form.get('symbol')
        direction = request.form.get('direction')
        
        if not symbol or not direction:
            flash("Missing required parameters.", "danger")
            return redirect(url_for('performance'))
        
        # Update scan data to remove queued signal
        data = load_data()
        if 'signals' in data and isinstance(data['signals'], list):
            updated_signals = []
            for s in data['signals']:
                if isinstance(s, dict):
                    if s.get('symbol') == symbol and s.get('direction') == direction and s.get('trade_status') == 'queued':
                        continue
                updated_signals.append(s)
            data['signals'] = updated_signals
            
            # Write updated data
            temp_path = DATA_FILE + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, default=_default_json_encoder)
            os.replace(temp_path, DATA_FILE)
        
        flash(f"Queued signal for {symbol} {direction} removed successfully.", "success")
    except Exception as e:
        flash(f"Error removing queued signal: {e}", "danger")
    return redirect(url_for('performance'))

@app.route("/api/performance_stats")
@login_required
def get_performance_stats():
    """Return aggregate analytics data for charts (scoped to the selected strategy)."""
    records = _filter_records_by_strategy(
        SignalRecord.query.order_by(SignalRecord.timestamp.asc()).all(),
        _perf_selected_strategy())
    records = get_qualified_records(records)
    
    # Cumulative PnL over time
    cumulative_pnl = []
    running_pnl = 0
    for r in records:
        if r.pnl_pct is not None:
            running_pnl += r.pnl_pct
            cumulative_pnl.append({
                "timestamp": r.exit_timestamp.strftime("%m/%d %H:%M") if r.exit_timestamp else r.timestamp.strftime("%m/%d %H:%M"),
                "pnl": round(running_pnl, 2)
            })
    
    # Win rate by lights count
    lights_stats = {}
    for r in records:
        lc = r.lights_count or 0
        if lc not in lights_stats:
            lights_stats[lc] = {"wins": 0, "losses": 0, "total": 0}
        lights_stats[lc]["total"] += 1
        if is_win_record(r):
            lights_stats[lc]["wins"] += 1
        elif is_loss_record(r):
            lights_stats[lc]["losses"] += 1
    
    lights_chart = []
    for lc in sorted(lights_stats.keys()):
        s = lights_stats[lc]
        wr = (s["wins"] / (s["wins"] + s["losses"]) * 100) if (s["wins"] + s["losses"]) > 0 else 0
        lights_chart.append({"lights": lc, "win_rate": round(wr, 1), "total": s["total"]})
    
    # Direction breakdown
    dir_stats = {"LONG": {"wins": 0, "losses": 0}, "SHORT": {"wins": 0, "losses": 0}}
    for r in records:
        d = r.direction
        if d in dir_stats:
            if is_win_record(r):
                dir_stats[d]["wins"] += 1
            elif is_loss_record(r):
                dir_stats[d]["losses"] += 1
    
    # PnL distribution
    pnl_values = [round(r.pnl_pct, 2) for r in records if r.pnl_pct is not None]

    # Win rate by SMC zone
    smc_stats = {}
    for r in records:
        zone = r.smc_zone or "unknown"
        if zone not in smc_stats:
            smc_stats[zone] = {"wins": 0, "losses": 0}
        if is_win_record(r):
            smc_stats[zone]["wins"] += 1
        elif is_loss_record(r):
            smc_stats[zone]["losses"] += 1
    
    smc_chart = []
    for zone, s in smc_stats.items():
        wr = (s["wins"] / (s["wins"] + s["losses"]) * 100) if (s["wins"] + s["losses"]) > 0 else 0
        smc_chart.append({"zone": zone, "win_rate": round(wr, 1), "total": s["wins"] + s["losses"]})

    return jsonify({
        "cumulative_pnl": cumulative_pnl,
        "lights_chart": lights_chart,
        "direction": dir_stats,
        "pnl_distribution": pnl_values,
        "smc_chart": smc_chart,
    })

@app.route("/api/performance_live")
@login_required
def get_performance_live():
    scoped = _filter_records_by_strategy(
        SignalRecord.query.order_by(SignalRecord.timestamp.desc()).all(),
        _perf_selected_strategy())
    records = sorted(
        get_qualified_records(scoped),
        key=lambda r: ((r.lights_count or 0), r.timestamp),
        reverse=True,
    )
    if not records:
        return jsonify([])

    pending_records = [r for r in records if r.status in ('PENDING', 'TP1_PARTIAL')]
    symbols = list({r.symbol for r in pending_records})
    try:
        tickers = fetch_live_tickers(symbols) if symbols else {}
        live_data = []

        for r in records:
            if r.status in ('PENDING', 'TP1_PARTIAL'):
                ticker = tickers.get(r.symbol)
                if not ticker or ticker.get("last") is None:
                    live_data.append({
                        "id": r.id,
                        "symbol": r.symbol,
                        "live_price": None,
                        "pnl_pct": round(r.pnl_pct, 2) if r.pnl_pct is not None else None,
                        "exit_price": format_price(r.exit_price) if r.exit_price is not None else None,
                        "is_closed": False,
                        "status": r.status,
                    })
                    continue

                current_price = ticker["last"]
                entry = r.entry_price
                
                # Calculate live PnL: for TP1_PARTIAL, we already have partial PnL, but show live for remaining
                if r.status == 'PENDING':
                    if r.direction == 'LONG':
                        live_pnl = ((current_price - entry) / entry) * 100
                    else:
                        live_pnl = ((entry - current_price) / entry) * 100
                else:  # TP1_PARTIAL
                    # For partial TP, we show the partial PnL as base
                    live_pnl = r.pnl_pct or 0

                live_data.append({
                    "id": r.id,
                    "symbol": r.symbol,
                    "live_price": format_price(current_price),
                    "pnl_pct": round(live_pnl, 2),
                    "exit_price": None,
                    "is_closed": False,
                    "status": r.status,
                })
                continue

            live_data.append({
                "id": r.id,
                "symbol": r.symbol,
                "live_price": None,
                "pnl_pct": round(r.pnl_pct, 2) if r.pnl_pct is not None else None,
                "exit_price": format_price(r.exit_price) if r.exit_price is not None else None,
                "is_closed": True,
                "status": r.status,
            })

        return jsonify(live_data)
    except Exception as e:
        print(f"Error fetching performance live prices: {e}")
        return jsonify([]), 500

# ── brute-force throttle (in-memory; fine for a single-process app) ──────────
# The site is internet-facing, so the login page WILL get probed by bots. After
# LOGIN_MAX_FAILS failed attempts from one IP inside LOGIN_WINDOW_SEC, reject for
# the rest of the window. A correct login clears the counter for that IP.
_login_fails: dict = {}          # ip -> [timestamps of recent failures]
LOGIN_MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "8"))
LOGIN_WINDOW_SEC = int(os.getenv("LOGIN_WINDOW_SEC", "900"))   # 15 min


def _login_blocked(ip: str) -> bool:
    now = datetime.now().timestamp()
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW_SEC]
    _login_fails[ip] = fails
    return len(fails) >= LOGIN_MAX_FAILS


def _record_login_fail(ip: str) -> None:
    _login_fails.setdefault(ip, []).append(datetime.now().timestamp())


@app.route("/login", methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or "unknown"
        if _login_blocked(ip):
            flash('Too many failed attempts. Try again in a few minutes.', 'danger')
            return render_template("login.html", registration_enabled=ALLOW_PUBLIC_REGISTRATION), 429
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            _login_fails.pop(ip, None)          # clear the counter on success
            login_user(user)
            return redirect(url_for('index'))
        else:
            _record_login_fail(ip)
            flash('Login failed. Check your username and password.', 'danger')

    return render_template("login.html", registration_enabled=ALLOW_PUBLIC_REGISTRATION)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route("/")
@login_required
def index():
    data = load_data()

    # Funnel summary for the dashboard tile (computed from raw signals, before
    # clean_signals mutates data["signals"] below).
    try:
        _f = build_funnel(data)
        funnel_summary = {
            "queued": _f["queued"],
            "total": _f["total"],
            "top_blocker": _f["leaderboard"][0] if _f["leaderboard"] else None,
        }
    except Exception as e:
        print(f"Error building funnel summary: {e}")
        funnel_summary = {"queued": 0, "total": 0, "top_blocker": None}

    # Best entry candidates for the dashboard panel (from raw signals).
    try:
        top_entries = build_top_entries(data)
    except Exception as e:
        print(f"Error building top entries: {e}")
        top_entries = {"last_update": data.get("last_update"), "btc_regime": "unknown", "ready": 0, "entries": []}

    # Calculate quick stats for live banner
    try:
        records = get_qualified_records(SignalRecord.query.all())
        quick_stats = build_win_rate_stats(records)
        total_pnl = sum(r.pnl_pct for r in records if r.pnl_pct is not None)
        # Trades that hit TP1 and banked 50% but are still running the other
        # half toward TP2. Their booked profit IS in total_pnl but they are not
        # yet "closed", so surface the count to explain the number.
        open_partials = sum(1 for r in records if r.status == 'TP1_PARTIAL')

        # Clean signals
        cleaned_signals = clean_signals(data.get("signals", []), records)
        data["signals"] = cleaned_signals

        dashboard_summary = build_dashboard_summary(data)

        perf_stats = {
            "win_rate": round(quick_stats["win_rate"], 1),
            "total_pnl": round(total_pnl, 1),
            "open_partials": open_partials
        }
        circuit = build_circuit_state(records)
    except Exception as e:
        print(f"Error calculating dashboard stats: {e}")
        perf_stats = {"win_rate": 0.0, "total_pnl": 0.0, "open_partials": 0}
        circuit = build_circuit_state([])
        dashboard_summary = build_dashboard_summary({"signals": []})
        data["signals"] = []

    return render_template(
        "index.html", 
        signals=data["signals"], 
        last_update=data["last_update"],
        symbol_limit=TOP_SYMBOL_LIMIT,
        scan_limit=SCAN_SYMBOL_LIMIT,
        trade_limit=TOP_SYMBOL_LIMIT,
        interval=CHECK_INTERVAL_MINUTES,
        rsi_upper=RSI_UPPER_THRESHOLD,
        rsi_lower=RSI_LOWER_THRESHOLD,
        min_alert_lights=MIN_LIGHTS_FOR_ALERT,
        user=current_user,
        perf_stats=perf_stats,
        dashboard_summary=dashboard_summary,
        circuit=circuit,
        funnel_summary=funnel_summary,
        top_entries=top_entries,
        live_strategy=_live_strategy_state(),
    )

@app.route("/api/scan_data")
@login_required
def get_scan_data():
    data = load_data()
    records = get_qualified_records(SignalRecord.query.all())
    cleaned_signals = clean_signals(data.get("signals", []), records)
    data["signals"] = cleaned_signals
    data["age_sec"] = scan_age_seconds(data.get("last_update"))
    data["interval_min"] = CHECK_INTERVAL_MINUTES
    return jsonify(data)


@app.route("/api/dashboard_summary")
@login_required
def get_dashboard_summary():
    return jsonify(build_dashboard_summary(load_data()))


@app.route("/funnel")
@login_required
def funnel():
    return render_template("funnel.html", funnel=build_funnel(load_data()), user=current_user)


@app.route("/api/funnel")
@login_required
def get_funnel():
    return jsonify(build_funnel(load_data()))


@app.route("/api/top_entries")
@login_required
def get_top_entries():
    return jsonify(build_top_entries(load_data()))


# The stand-alone Strategy-2 scanner is a SECOND live engine, selectable in the
# same admin switcher. It is not a backtest.STRATEGIES entry (it's a separate
# process armed by STRATEGY2_LIVE), so it gets a synthetic key here.
S2_ENGINE_KEY = "strategy2_live"
S2_ENGINE_NAME = "Strategy 2 — TV.pine Confluence (15m live)"
S2_ENGINE_DESC = ("Stand-alone 15m TV.pine confluence scanner. Trades only the "
                  "highest-conviction signals (long ≥85 / short ≤15) on the 25 USDT "
                  "account. Runs INSTEAD of S1 — one engine at a time (./run_all.sh).")


def _scanner_live_engine(max_age_sec=900):
    """True only if the S2 scanner is running AS THE LIVE ENGINE — it rewrote its
    signals file within max_age_sec (every sweep, ~5 min) AND that file reports
    live execution armed. An alert-only scanner (live=false, running alongside S1)
    does NOT count as the live engine."""
    import time
    try:
        path = os.path.join(os.path.dirname(__file__), "strategy2_signals.json")
        with open(path) as f:
            data = json.load(f) or {}
        fresh = (time.time() - float(data.get("generated_at", 0))) <= max_age_sec
        return bool(fresh and data.get("live"))
    except Exception:  # noqa: BLE001
        return False


def _live_strategy_state():
    """Saved (.env) vs running live ENGINE for the admin switcher.

    Two engines can be the live one, and only ONE runs at a time (see run_all.sh):
      • a backtest.STRATEGIES entry (currently just 'default' = S1 Wolf bot),
        selected via LIVE_STRATEGY; the S1 bot reads it at startup.
      • the stand-alone Strategy-2 scanner ('strategy2_live'), armed via
        STRATEGY2_LIVE. Picking it makes S2 the live engine instead of S1.
    'saved' can lead 'running' until a restart (./run_all.sh) — shown as a
    divergence banner."""
    import backtest as BT
    import config as _config
    import strategy2_live as S2L

    s2_armed = (_config.read_env_var("STRATEGY2_LIVE", "false") or "false").strip().lower() \
        in ("1", "true", "yes", "on")

    # Saved (configured) engine.
    if s2_armed:
        saved, saved_name = S2_ENGINE_KEY, S2_ENGINE_NAME
    else:
        saved = _config.read_env_var("LIVE_STRATEGY", "default")
        if saved not in BT.STRATEGIES:
            saved = "default"
        saved_name = BT.STRATEGIES[saved]["name"]

    # Running engine — the one ACTUALLY placing trades right now, by process
    # ground-truth (not the saved .env), so a pending switch shows as divergence:
    #   • S1 = the bot holds its PID lock.
    #   • S2 = the scanner is alive AND reports itself as the live engine.
    try:
        s1_running = S2L.s1_bot_running()
    except Exception:  # noqa: BLE001
        s1_running = False

    bot_marker = None
    try:
        marker = os.path.join(os.path.dirname(__file__), "bot_strategy.json")
        if os.path.exists(marker):
            with open(marker) as f:
                bot_marker = (json.load(f) or {}).get("strategy")
    except Exception:  # noqa: BLE001
        bot_marker = None

    if s1_running:
        running = bot_marker if bot_marker in BT.STRATEGIES else "default"
        running_name = (BT.STRATEGIES.get(running) or {}).get("name")
    elif _scanner_live_engine():
        running, running_name = S2_ENGINE_KEY, S2_ENGINE_NAME
    else:
        running, running_name = None, None

    strategies = [{"key": k, "name": v["name"], "desc": v["desc"], "manage": "bracket"}
                  for k, v in BT.STRATEGIES.items()]
    strategies.append({"key": S2_ENGINE_KEY, "name": S2_ENGINE_NAME,
                       "desc": S2_ENGINE_DESC, "manage": "bracket"})

    return {
        "saved": saved,
        "saved_name": saved_name,
        "running": running,
        "running_name": running_name,
        "diverged": bool(running and running != saved),
        "strategies": strategies,
    }


@app.route("/account")
@admin_required
def account():
    """Live Binance USD-M futures account page — balance, positions, open orders,
    so you never need to open the Binance app."""
    return render_template(
        "account.html",
        user=current_user,
        status_line=executor.status_line(),
        cfg={
            "leverage": LEVERAGE,
            "margin": FIXED_MARGIN_USDT,
            "max_margin": MAX_MARGIN_USDT,
            "max_concurrent": MAX_CONCURRENT_POSITIONS,
            "partial_tp": LIVE_PARTIAL_TP,
            "tp_target": LIVE_TP_TARGET,
        },
        live_strategy=_live_strategy_state(),
    )


@app.route("/api/account/live_strategy", methods=["GET", "POST"])
@admin_required
def api_account_live_strategy():
    """GET: current saved/running live strategy. POST: persist a new LIVE_STRATEGY
    to .env (takes effect only after the bot is restarted)."""
    import backtest as BT
    import config as _config
    if request.method == "GET":
        return jsonify(_live_strategy_state())
    validate_csrf()
    key = (request.form.get("strategy") or "").strip()
    try:
        if key == S2_ENGINE_KEY:
            # Make Strategy 2 the live engine. run_all.sh sees STRATEGY2_LIVE=true
            # and starts the S2 scanner INSTEAD of the S1 bot — one at a time.
            _config.set_env_var("STRATEGY2_LIVE", "true")
        elif key in BT.STRATEGIES:
            # An S1-bot strategy: disarm S2 and select it for the bot.
            _config.set_env_var("STRATEGY2_LIVE", "false")
            _config.set_env_var("LIVE_STRATEGY", key)
        else:
            return jsonify({"ok": False, "error": f"unknown strategy {key!r}"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
    state = _live_strategy_state()
    return jsonify({"ok": True, "saved": state["saved"], "restart_required": True, "state": state})


@app.route("/api/account")
@admin_required
def api_account():
    """Read-only JSON snapshot of the live futures account (cached ~5s), enriched
    with each position's bot-DB status so the page can flag divergence — a live
    position the bot believes is already closed (the exact EVAA failure mode)."""
    snap = executor.account_snapshot()
    try:
        positions = snap.get("positions") or []
        if positions:
            symbols = {p.get("symbol") for p in positions if p.get("symbol")}
            latest = {}
            for rec in (SignalRecord.query
                        .filter(SignalRecord.symbol.in_(symbols))
                        .order_by(SignalRecord.timestamp.desc()).all()):
                latest.setdefault(rec.symbol, rec)  # newest per symbol
            for p in positions:
                rec = latest.get(p.get("symbol"))
                status = (rec.status if rec else None)
                p["db_status"] = status
                # Bot thinks this is closed (or never tracked it) but it's live.
                p["db_diverged"] = bool(status and status not in ("PENDING", "TP1_PARTIAL"))
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the snapshot
        print(f"[account] db-status enrichment note: {exc}")
    return jsonify(snap)


@app.route("/api/account/history")
@admin_required
def api_account_history():
    """Realized-P&L history (bot + manual trades) straight from Binance."""
    return jsonify(executor.realized_pnl_history(limit=80))


def _parse_price(raw):
    try:
        return float(raw) if raw not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


@app.route("/api/account/close", methods=["POST"])
@admin_required
def api_account_close():
    """MANUAL: close an open position at market (reduce-only)."""
    symbol = request.form.get("symbol")
    if not symbol:
        return jsonify({"ok": False, "error": "missing symbol"}), 400
    return jsonify(executor.close_position_market(symbol))


@app.route("/api/account/protect", methods=["POST"])
@admin_required
def api_account_protect():
    """MANUAL: set/replace stop-loss and/or take-profit for a position."""
    symbol = request.form.get("symbol")
    if not symbol:
        return jsonify({"ok": False, "error": "missing symbol"}), 400
    return jsonify(executor.set_protection(
        symbol,
        sl=_parse_price(request.form.get("sl")),
        tp=_parse_price(request.form.get("tp")),
    ))


@app.route("/api/account/cancel_protection", methods=["POST"])
@admin_required
def api_account_cancel_protection():
    """MANUAL: cancel ALL stop-loss / take-profit orders for a position (Reset),
    so a fresh Set doesn't stack a duplicate conditional record on Binance."""
    symbol = request.form.get("symbol")
    if not symbol:
        return jsonify({"ok": False, "error": "missing symbol"}), 400
    return jsonify(executor.cancel_protection(symbol))


@app.route("/sw.js")
def service_worker():
    """Serve the service worker from root so its scope covers the whole app."""
    from flask import send_from_directory
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/manifest.webmanifest")
def web_manifest():
    from flask import send_from_directory
    return send_from_directory(app.static_folder, "manifest.webmanifest",
                               mimetype="application/manifest+json")


@app.route("/strategy2")
@login_required
def strategy2():
    """Strategy 2 — live confidence meter that mirrors the TradingView indicator
    (TV.pine). The page renders the gauge + an embedded TradingView chart and polls
    /api/strategy2 for the live 0–100 score. The symbol list reuses the scanner's
    universe so it matches what the dashboard is watching."""
    data = load_data()
    syms = sorted({s.get("symbol") for s in data.get("signals", []) if s.get("symbol")})
    default_symbol = "BTC/USDT:USDT"
    if default_symbol not in syms:
        syms = [default_symbol] + syms
    return render_template("strategy2.html", user=current_user,
                           symbols=syms, default_symbol=default_symbol)


@app.route("/api/strategy2")
@app.route("/api/strategy2/<path:symbol>")
@login_required
def api_strategy2(symbol="BTC/USDT:USDT"):
    """Live confidence-meter JSON for one symbol. Reuses the shared rest_client
    (same throttle/cooldown as the rest of the app) and the pure-compute meter in
    strategy2_meter.py. 450 1h candles cover the outer tunnel EMA338 + Vegas SMA5."""
    import strategy2_meter
    try:
        ohlcv = rest_client.call("fetch_ohlcv", symbol, "1h", None, 450)
    except RateLimitCooldownError as exc:
        return jsonify({"symbol": symbol, "error": str(exc)}), 200
    except Exception as exc:  # noqa: BLE001 — never 500 the dashboard
        return jsonify({"symbol": symbol, "error": f"fetch failed: {exc}"}), 200
    meter = strategy2_meter.compute_meter(ohlcv)
    meter["symbol"] = symbol
    return jsonify(meter)


@app.route("/api/strategy2_ohlcv")
@app.route("/api/strategy2_ohlcv/<path:symbol>")
@login_required
def api_strategy2_ohlcv(symbol="BTC/USDT:USDT"):
    """Raw 1h OHLC for the Strategy-2 chart. The Lightweight-Charts mirror draws the
    candles + the full TV.pine EMA stack client-side (each EMA its own colour), so it
    uses the SAME 1h/450 window the meter does — the on-chart EMAs line up exactly with
    the confidence factors. Distinct path so it never collides with the <path:symbol>
    meter route. Volume is dropped to keep the payload small."""
    try:
        ohlcv = rest_client.call("fetch_ohlcv", symbol, "1h", None, 450)
    except RateLimitCooldownError as exc:
        return jsonify({"symbol": symbol, "error": str(exc), "candles": []}), 200
    except Exception as exc:  # noqa: BLE001 — never 500 the dashboard
        return jsonify({"symbol": symbol, "error": f"fetch failed: {exc}", "candles": []}), 200
    candles = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])]
               for c in (ohlcv or [])]
    return jsonify({"symbol": symbol, "timeframe": "1h", "candles": candles})


@app.route("/api/strategy2_signals")
@login_required
def api_strategy2_signals():
    """Recent fired 15m signals from the stand-alone scanner (strategy2_scanner.py).
    Read-only: returns an idle payload if the scanner isn't running yet. Distinct
    path (not /api/strategy2/...) so it never collides with the <path:symbol> route."""
    path = os.path.join(os.path.dirname(__file__), "strategy2_signals.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception:  # noqa: BLE001
        return jsonify({"generated_at": 0, "scanning": 0, "done": 0,
                        "timeframe": "15m", "signals": []})


@app.route("/api/strategy2_live_status")
@login_required
def api_strategy2_live_status():
    """Live-execution status for the /strategy2 'Live Trading Rules' panel. Reads
    the .env fresh so the toggle reflects what the scanner will do on next start."""
    import strategy2_live
    try:
        return jsonify(strategy2_live.status())
    except Exception as exc:  # noqa: BLE001 — never 500 the dashboard
        return jsonify({"error": str(exc), "enabled": False})


@app.route("/market")
@login_required
def market():
    """Market Intel page — futures stats, on-chain TVL and crypto news.

    Fully isolated from the scanner/bot: all data comes from read-only public
    APIs via market_intel.py. The initial render passes a best-effort snapshot;
    the page then auto-refreshes from /api/market_intel.
    """
    try:
        intel = market_intel.market_intel(top_n=15)
    except Exception as e:  # noqa: BLE001 — never let this page break
        print(f"Market intel error: {e}")
        intel = {"generated_at": 0, "futures": [], "onchain": {"chains": [], "total_tvl": 0.0},
                 "news": [], "errors": [str(e)]}
    try:
        circuit = build_circuit_state(get_qualified_records(SignalRecord.query.all()))
    except Exception as e:  # noqa: BLE001
        print(f"Circuit state error: {e}")
        circuit = build_circuit_state([])
    return render_template("market.html", intel=intel, circuit=circuit)


@app.route("/api/market_intel")
@login_required
def api_market_intel():
    try:
        return jsonify(market_intel.market_intel(top_n=15))
    except Exception as e:  # noqa: BLE001
        return jsonify({"generated_at": 0, "futures": [], "onchain": {"chains": [], "total_tvl": 0.0},
                        "news": [], "errors": [str(e)]}), 200

@app.route("/api/news")
@login_required
def api_news():
    """Lightweight headlines for the dashboard. Only the cached RSS feeds —
    skips the heavier futures / positioning / TVL fetches that
    /api/market_intel does — so the home page poll stays cheap."""
    try:
        nw = market_intel.news()
        return jsonify({"items": nw.get("items", []), "errors": nw.get("errors", [])})
    except Exception as e:  # noqa: BLE001
        return jsonify({"items": [], "errors": [str(e)]}), 200


def build_briefing():
    """Computed 'today' briefing — BTC + US indices + altcoin breadth, with a
    short rule-based read. Reuses cached market_intel fetches and the existing
    dashboard summary. Read-only and fully fail-soft."""
    import time as _time
    out = {"generated_at": int(_time.time()), "btc": {}, "stocks": [],
           "alts": {}, "fng": {}, "verdict": "", "errors": []}

    # Crypto regime + altcoin breadth (local DB / scan — cheap).
    regime, sig = "neutral", {}
    try:
        ds = build_dashboard_summary()
        regime = (ds.get("btc_regime") or "neutral").lower()
        sig = ds.get("signals", {})
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"summary: {e}")

    # BTC price / 24h / funding (cached ~45s).
    btc = {"regime": regime}
    try:
        snap = market_intel.btc_snapshot()
        btc["price"], btc["change_pct"], btc["funding"] = (
            snap.get("price"), snap.get("change_pct"), snap.get("funding_rate"))
        out["errors"].extend(snap.get("errors", []))
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"btc: {e}")

    # Whale vs retail positioning for BTC.
    try:
        ls = market_intel.long_short(("BTCUSDT",))
        if ls.get("rows"):
            r0 = ls["rows"][0]
            btc["whale_long"], btc["retail_long"] = r0.get("whale_long"), r0.get("retail_long")
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"positioning: {e}")

    wl, rl = btc.get("whale_long"), btc.get("retail_long")
    if wl is not None and rl is not None:
        if rl - wl >= 0.12:
            btc["note"] = f"crowd {rl*100:.0f}% long vs whales {wl*100:.0f}% — crowd over-eager"
        elif wl - rl >= 0.12:
            btc["note"] = f"whales {wl*100:.0f}% long vs crowd {rl*100:.0f}% — smart money leads"
        else:
            btc["note"] = f"whales {wl*100:.0f}% / crowd {rl*100:.0f}% long — aligned"
    out["btc"] = btc

    # Crypto Fear & Greed sentiment (cached 10 min).
    try:
        fg = market_intel.fear_greed()
        if fg.get("value") is not None:
            out["fng"] = {"value": fg["value"], "label": fg.get("label"),
                          "week_ago": fg.get("week_ago"), "history": fg.get("history", [])}
        out["errors"].extend(fg.get("errors", []))
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"fng: {e}")

    # US indices (cached 10 min).
    stock_rows = []
    try:
        st = market_intel.stocks()
        stock_rows = st.get("rows", [])
        out["errors"].extend(st.get("errors", []))
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"stocks: {e}")
    out["stocks"] = stock_rows

    # Altcoin breadth read.
    longs, shorts = sig.get("long", 0), sig.get("short", 0)
    if shorts > max(1, longs) * 1.5:
        alt_tone = "shorts dominate — weak breadth"
    elif longs > max(1, shorts) * 1.5:
        alt_tone = "longs dominate — strong breadth"
    else:
        alt_tone = "mixed breadth"
    out["alts"] = {"longs": longs, "shorts": shorts, "avg_rsi": sig.get("avg_rsi", 0),
                   "bias": sig.get("bias", "Balanced"), "tone": alt_tone}

    # Overall verdict from regime + equity tone.
    chgs = [s.get("change_pct") for s in stock_rows if s.get("change_pct") is not None]
    stock_avg = (sum(chgs) / len(chgs)) if chgs else None
    if regime == "bear":
        verdict = "Risk-off crypto — trade with the bear, shorts favored"
        if stock_avg is not None and stock_avg > 0.2:
            verdict += " (equities green but crypto lagging — stay cautious)"
    elif regime == "bull":
        verdict = "Risk-on — longs favored with BTC"
        if stock_avg is not None and stock_avg < -0.2:
            verdict += " (crypto strong despite soft equities)"
    else:
        verdict = "Choppy / neutral — both sides allowed, stay selective"

    # Fear & Greed adds a contrarian nuance — the extremes are the actionable read.
    fng_val = out.get("fng", {}).get("value")
    if fng_val is not None:
        if fng_val < 25:
            verdict += " · extreme fear — capitulation risk, watch for a relief bounce"
        elif fng_val > 75:
            verdict += " · extreme greed — froth, watch for a pullback"
        elif regime == "neutral" and fng_val < 45:
            verdict += " · fearful tape leans defensive"
        elif regime == "neutral" and fng_val > 55:
            verdict += " · greedy tape leans risk-on"

    out["verdict"] = verdict
    return out


@app.route("/api/briefing")
@login_required
def api_briefing():
    try:
        return jsonify(build_briefing())
    except Exception as e:  # noqa: BLE001
        return jsonify({"generated_at": 0, "btc": {}, "stocks": [], "alts": {},
                        "verdict": "", "errors": [str(e)]}), 200

@app.route("/api/live_prices")
@login_required
def get_live_prices():
    data = load_data()
    signals = data.get("signals", [])
    if not signals:
        return jsonify([])

    try:
        tickers = fetch_live_tickers([s["symbol"] for s in signals])
        live_data = []
        for s in signals:
            symbol = s["symbol"]
            current_price = tickers[symbol]["last"] if symbol in tickers else None

            live_data.append({
                "symbol": symbol,
                "live_price": format_price(current_price),
            })
        return jsonify(live_data)
    except Exception as e:
        print(f"Error fetching live prices: {e}")
        return jsonify([]), 500

@app.errorhandler(400)
def bad_request(error):
    # Self-heal stale/cross-origin CSRF failures instead of dropping the user on a
    # dead "Invalid CSRF token" page. This is common when the dashboard is reached
    # through a tunnel (e.g. trycloudflare): the browser's token came from a
    # different origin/session than the one the POST landed in. For a normal browser
    # request we flash + redirect back so the next GET issues a fresh, matching token
    # (CSRF protection itself is unchanged). API/JSON callers still get a hard 400.
    desc = str(getattr(error, "description", "") or "")
    wants_html = "text/html" in request.headers.get("Accept", "")
    used_header_token = request.headers.get("X-CSRFToken") is not None
    if "CSRF" in desc and wants_html and not used_header_token:
        flash(
            "Your session expired or the page was stale (common when opening the "
            "dashboard through a new tunnel URL). Please try again.",
            "warning",
        )
        return redirect(request.referrer or url_for("login"))
    return desc or "400 Bad Request", 400

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return "500 Internal Server Error - Check console logs", 500

if __name__ == "__main__":
    with app.app_context():
        db.create_all() # Create database tables for all binds
        
        # Manual Migrations and Fixes
        from sqlalchemy import inspect, text
        
        # 1. Promote first user to admin if no admin exists
        first_user = User.query.first()
        if first_user and not User.query.filter_by(is_admin=True).first():
            first_user.is_admin = True
            db.session.commit()
            print(f"Promoted {first_user.username} to Admin")

        # 2. Check User table columns (default engine)
        user_inspector = inspect(db.engine)
        user_columns = [c['name'] for c in user_inspector.get_columns('user')]
        if 'is_admin' not in user_columns:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE user ADD COLUMN is_admin BOOLEAN DEFAULT 0"))
                conn.commit()
                print("Added is_admin to user table")

        # 3. Check SignalRecord table columns (signals bind)
        signals_engine = db.engines['signals']
        sig_inspector = inspect(signals_engine)
        if 'signal_record' in sig_inspector.get_table_names():
            sig_columns = [c['name'] for c in sig_inspector.get_columns('signal_record')]
            with signals_engine.connect() as conn:
                # Existing migrations
                if 'pnl_pct' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN pnl_pct FLOAT"))
                if 'lights_count' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN lights_count INTEGER DEFAULT 0"))
                if 'exit_timestamp' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN exit_timestamp DATETIME"))
                # New v2 migrations
                if 'atr_value' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN atr_value FLOAT"))
                if 'rsi_value' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN rsi_value FLOAT"))
                if 'trend_aligned' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN trend_aligned BOOLEAN"))
                if 'smc_zone' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN smc_zone VARCHAR(20)"))
                if 'rr_ratio' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN rr_ratio FLOAT"))
                if 'partial_tp1' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN partial_tp1 BOOLEAN DEFAULT FALSE"))
                if 'runner_peak' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN runner_peak FLOAT"))
                # Strategy management + live trailing-stop state (S3/S4)
                if 'strategy' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN strategy VARCHAR(20) DEFAULT 'default'"))
                if 'manage' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN manage VARCHAR(10) DEFAULT 'bracket'"))
                if 'trail_dist' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN trail_dist FLOAT"))
                if 'peak' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN peak FLOAT"))
                if 'trough' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN trough FLOAT"))
                if 'cur_stop' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN cur_stop FLOAT"))
                if 'last_bar_ts' not in sig_columns:
                    conn.execute(text("ALTER TABLE signal_record ADD COLUMN last_bar_ts BIGINT"))
                conn.commit()
                print("Migrated signal_record table (v2 + strategy/trailing columns)")
                
    app_host = os.getenv("FLASK_HOST", "127.0.0.1")
    app_port = int(os.getenv("FLASK_PORT", "4000"))
    app_debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    # Auto-reload the server when a .py file changes (dev convenience). Enabled by
    # FLASK_RELOAD=true OR full debug. Reload works WITHOUT the debugger so you
    # don't expose the interactive console just to get hot-reload.
    app_reload = app_debug or os.getenv("FLASK_RELOAD", "false").lower() == "true"
    app.run(host=app_host, port=app_port, debug=app_debug, use_reloader=app_reload)
