import gzip
import json
import os
import re
import secrets
import time
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
    MIN_LIGHTS_FOR_ALERT, MIN_LIGHTS_FOR_RECORD,
    REST_API_MAX_RETRIES, REST_API_MIN_INTERVAL_SEC, QUOTE_ASSET,
    DAILY_MAX_LOSSES, DAILY_MAX_DRAWDOWN_PCT,
    VOLUME_GATE_MULTIPLIER, MIN_BASE_LIGHTS,
    LEVERAGE, FIXED_MARGIN_USDT, MAX_MARGIN_USDT, MAX_CONCURRENT_POSITIONS,
    LIVE_PARTIAL_TP, LIVE_TP_TARGET,
)
from market_data import SafeBinanceClient, RateLimitCooldownError
import liquidations
import market_intel
import executor
import price_alerts
import pulse
import restart_ctl
import strategy3_exec
import strategy3_scanner

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
# Static assets cache for 7 days in the browser — safe because every template
# links them with ?v=ASSET_VER (bump it on any css/js edit). Without this the
# browser revalidates each file per page load, which drags over the tunnel.
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 7 * 24 * 3600
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_SESSION_COOKIE_SECURE', 'false').lower() == 'true'
app.config['PREFERRED_URL_SCHEME'] = os.getenv('FLASK_PREFERRED_URL_SCHEME', 'https')
# Idle timeout: this dashboard shows real account balances/positions, so a
# browser tab left open indefinitely shouldn't stay authenticated forever.
# session.permanent=True (set at login) + this lifetime makes the cookie
# expire after N idle hours; Flask re-stamps the expiry on every request by
# default (SESSION_REFRESH_EACH_REQUEST), so active use is never interrupted
# — only genuine inactivity times out.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(
    hours=float(os.getenv('SESSION_IDLE_HOURS', '24')))

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
    # ── ONE ACCOUNT, ONE ACTIVE LOGIN (2026-08-11) ──────────────────────────
    # Rotated on every successful login. The value is baked into the session
    # cookie by get_id(), so the moment it changes every OTHER device holding
    # a cookie for this account is carrying a stale token and is signed out on
    # its next request. That is what stops one login being passed around.
    session_token = db.Column(db.String(64))
    # Purely so the owner can see a takeover happened and from where. Never
    # used to ALLOW or DENY — see the note in load_user about why IP is the
    # wrong lever.
    last_login_at = db.Column(db.DateTime)
    last_login_ip = db.Column(db.String(64))

    def get_id(self):
        """flask-login stores this string in the cookie and hands it back to
        load_user(). Embedding the token is what makes the cookie *versioned*
        rather than a permanent bearer of the account."""
        return f"{self.id}|{self.session_token or ''}"

    def new_session_token(self) -> str:
        self.session_token = secrets.token_urlsafe(32)
        return self.session_token

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

# Admin required decorator. Owner-only data (real balances, positions, P&L,
# ops) — registered MEMBERS are welcome on the signal pages but never here.
# API paths answer 403 JSON so a member's fetch() degrades cleanly instead of
# receiving a full dashboard-HTML redirect body.
def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            if request.path.startswith('/api/'):
                return jsonify({"error": "admin only"}), 403
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
            # What the card SHOWS. "MUBARAK/USDT:USDT" is 17 unbreakable
            # characters in a fifth of a row, which is what pushed one card's
            # name across its neighbour. Every other strip already shows the
            # base, so this makes them consistent too.
            "base": (s.get("base") or str(s.get("symbol", "?")).split("/")[0]),
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


def build_best_s1_trade(data: dict) -> dict:
    """Single best Strategy 1 candidate right now — entry/SL/TP, R:R and a
    plain-English reason. Half of the Funnel page's 'best trade' hero (the
    other half is Strategy 3, see build_best_s3_trade). Ready (queued) setups
    always outrank watch-only near-misses; if nothing is close, best is None
    rather than forcing a pick."""
    signals = data.get("signals", []) or []
    cands = []
    for s in signals:
        if not s.get("tradeable"):
            continue
        if not (s.get("trade_queued") or s.get("trade_status") == "rejected"):
            continue
        reason = s.get("record_status_reason", "") or ""
        queued = bool(s.get("trade_queued"))
        lights = s.get("effective_lights") or 0
        if not queued and lights < 4:
            continue
        fails = sum(1 for _, _, needle in FUNNEL_GATES if needle in reason)
        entry, sl = _parse_price(s.get("entry")), _parse_price(s.get("sl"))
        tp, tp2 = _parse_price(s.get("tp")), _parse_price(s.get("tp2"))
        rr = None
        rr_ref = tp2 or tp
        if entry and sl and rr_ref:
            risk = abs(entry - sl)
            rr = round(abs(rr_ref - entry) / risk, 2) if risk else None
        why = [d for d in (s.get("details") or []) if d][:2]
        smc = s.get("smc") or {}
        if smc.get("summary"):
            why.append(smc["summary"])
        blockers = [p.strip() for p in reason.split("rejected on latest scan:")[-1].split(";")
                    if p.strip()] if (not queued and "rejected" in reason) else []
        cands.append({
            "symbol": s.get("symbol", "?"),
            "direction": (s.get("direction") or "").upper(),
            "lights": lights,
            "conviction": s.get("conviction"),
            "rsi": s.get("rsi"),
            "entry": entry, "sl": sl, "tp": tp, "tp2": tp2,
            "rr": rr,
            "queued": queued,
            "fails": fails,
            "why": why[:3],
            "blockers": blockers[:2],
            "tv_url": s.get("tv_url"),
        })
    cands.sort(key=lambda c: (not c["queued"], -c["lights"], c["fails"]))
    return {
        "best": cands[0] if cands else None,
        "runner_up": cands[1] if len(cands) > 1 else None,
        "considered": len(cands),
    }


def build_best_s3_trade() -> dict:
    """Best Strategy 3 (Vegas Flag Flip) candidate right now, across
    config.STRATEGY3_SYMBOLS — the S3 half of the Funnel page's 'best trade'
    hero. 'Armed' (the MSB flip already fired, waiting only on Vegas
    agreement) ranks above a plain score-distance guess, since it reflects
    the real arm-and-fire state machine (strategy3_scanner.decide), not a
    heuristic reconstruction of it."""
    import config
    state = strategy3_scanner.load_state()
    rows = []
    for base in config.STRATEGY3_SYMBOLS:
        sym = f"{base}/{config.QUOTE_ASSET}:{config.QUOTE_ASSET}"
        st = state.get(sym, {})
        score = st.get("last_score")
        vegas = st.get("last_vegas")
        holding = st.get("pos_dir")
        armed = bool(st.get("last_flag")) and not st.get("consumed") and not holding
        params = config.strategy3_params(base)

        if params.get("engine") == "occ":
            # OCC symbols have no confluence score/Vegas — describe the
            # open/close-cross state instead of pretending a flag-flip state.
            trend = st.get("last_trend")
            if holding:
                note = f"OCC engine · holding {holding.upper()} until the opposite cross."
            elif trend:
                note = (f"OCC engine · 90m close-MA trend is {trend} — flat until "
                        f"the next open/close cross (stop-and-reverse).")
            else:
                note = "OCC engine · no closed 90m bucket seen yet."
            rows.append({
                "symbol": base, "timeframe": params["timeframe"],
                "notional": params["margin"] * params["leverage"], "leverage": params["leverage"],
                "score": None, "vegas": None, "msb": None,
                "armed_flag": st.get("last_flag"), "holding": holding,
                "lean": None, "dist_to_threshold": None,
                "vegas_agrees": False, "note": note, "seen": st.get("last_seen"),
            })
            continue

        lean = dist = None
        if score is not None:
            long_gap = config.STRATEGY3_SCORE_TH - score
            short_gap = score - (100 - config.STRATEGY3_SCORE_TH)
            lean, dist = ("long", long_gap) if long_gap <= short_gap else ("short", short_gap)
        vegas_agrees = bool(vegas is not None and lean and
                            ((lean == "long" and vegas > 0) or (lean == "short" and vegas < 0)))

        # IMPORTANT: score crossing the threshold does NOT by itself predict a
        # flag — a flag only fires when the MSB trend structure FLIPS while
        # score/ADX/direction line up on that same bar. So only the "armed"
        # state (a flag already fired and is waiting on Vegas) is a genuine
        # "about to trade" signal; anything else is descriptive context only,
        # never phrased as an imminent trade.
        if holding:
            note = f"Already holding {holding.upper()} — a running position, not a new entry."
        elif armed:
            want_color = "green" if st["last_flag"] == "long" else "red"
            note = (f"Flag armed {st['last_flag'].upper()} — waiting ONLY on the Vegas line "
                    f"to turn {want_color} to enter.")
        elif lean:
            bias = f"Current bias {lean} (score {score:.0f}/100), Vegas {'agrees' if vegas_agrees else 'disagrees'}"
            note = (f"{bias} — but no flag is armed right now; one needs the MSB trend "
                    f"structure to flip {lean} first.")
        else:
            note = "No signal history yet — waiting for the next closed candle."

        rows.append({
            "symbol": base, "timeframe": params["timeframe"],
            "notional": params["margin"] * params["leverage"], "leverage": params["leverage"],
            "score": score, "vegas": vegas, "msb": st.get("last_msb"),
            "armed_flag": st.get("last_flag"), "holding": holding,
            "lean": lean, "dist_to_threshold": round(dist, 1) if dist is not None else None,
            "vegas_agrees": vegas_agrees, "note": note, "seen": st.get("last_seen"),
        })

    def rank_key(r):
        return (
            r["holding"] is not None,                       # holding sorts LAST (not a new opportunity)
            not (r["armed_flag"] and not r["holding"]),      # armed-and-unconsumed sorts first
            not r["vegas_agrees"],
            r["dist_to_threshold"] if r["dist_to_threshold"] is not None else 999,
        )
    rows.sort(key=rank_key)
    return {"best": rows[0] if rows else None, "all": rows}


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


# Single source of truth for the static-asset cache-bust query (?v=...). Bump
# this ONE value whenever app.css / i18n.js change and every template busts its
# cache — no more hunting down 10 hardcoded copies (which once shipped an
# unstyled page to users). Templates reference it as ?v={{ asset_ver }}.
ASSET_VER = "20260819"


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token, "asset_ver": ASSET_VER}


# ── gzip responses ──────────────────────────────────────────────────────────
# The dashboard renders ~500KB of HTML (signal tables + inline CSS/JS) and was
# being shipped raw over the Cloudflare tunnel on every load. Text compresses
# ~80-87%, so this is the single biggest latency win available and it costs a
# few ms of CPU. Implemented with the stdlib rather than flask-compress on
# purpose: requirements.txt is deliberately pinned ("a live-money bot must not
# absorb surprise upgrades") and this is ~30 lines we fully control.
#
# Fail-soft by design: any error compressing returns the original response, so
# a bad edge case degrades to today's behaviour instead of a 500.
_GZIP_MIN_BYTES = 1024          # below this, framing overhead outweighs the win
_GZIP_TYPES = (
    "text/html", "text/css", "text/plain", "text/xml", "text/javascript",
    "application/json", "application/javascript", "application/manifest+json",
    "image/svg+xml",
)


@app.after_request
def compress_response(response):
    try:
        if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
            return response
        # Never touch an already-encoded body, a streamed feed, or an empty one.
        if response.headers.get("Content-Encoding"):
            return response
        if response.mimetype == "text/event-stream" or response.status_code < 200 or response.status_code in (204, 304):
            return response
        if response.mimetype not in _GZIP_TYPES:
            return response
        # Static files come back in passthrough mode (a file wrapper); reading
        # them requires opting out of it first. The wrapper then has to be
        # closed by hand — get_data() drains it into memory but never releases
        # the descriptor, so without this every /static hit leaks an open file
        # until GC. CI's `-W error::Warning` surfaced it as an unraisable
        # ResourceWarning; on the live server it would have been a slow FD leak.
        source = response.response if response.direct_passthrough else None
        if source is not None:
            response.direct_passthrough = False
        data = response.get_data()          # materialises the body, drains source
        if source is not None:
            closer = getattr(source, "close", None)
            if callable(closer):
                closer()
        if len(data) < _GZIP_MIN_BYTES:
            return response
        packed = gzip.compress(data, 6)
        if len(packed) >= len(data):    # already-compressed payload (e.g. a png)
            return response
        response.set_data(packed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(packed))
        # Critical for any cache in front of us (the tunnel, a browser, a proxy):
        # without this a gzipped body can be replayed to a client that can't read
        # it. Flask already varies on Cookie, so add rather than overwrite.
        response.vary.add("Accept-Encoding")
    except Exception as e:  # noqa: BLE001 — compression must never break a page
        print(f"gzip skipped: {e}")
    return response


@app.before_request
def protect_post_requests():
    # /line/webhook is POSTed by LINE's servers (no session): it authenticates
    # with the channel-secret signature inside the route, not a CSRF token.
    if request.method == "POST" and request.endpoint != "line_webhook":
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
    """Resolve the cookie to a user — and reject it if the login was superseded.

    ONE ACCOUNT, ONE ACTIVE LOGIN. The cookie carries "<id>|<session_token>".
    A fresh login rotates the token on the row, so every cookie minted earlier
    now disagrees and resolves to None: those devices are signed out on their
    very next request. Sharing one account across people stops working, which
    is the whole point.

    NOT DONE BY IP, deliberately. "One address" is the obvious reading, but IP
    is both too strict and too loose here: a phone on mobile data changes IP
    constantly (the real owner would be logged out at random), while two people
    sharing a home or behind the same CGNAT would both pass. Binding to the
    LOGIN EVENT instead targets exactly the thing being prevented — the same
    credentials in use in two places at once — and never punishes one person
    moving between networks. last_login_ip is recorded for visibility only.

    Legacy cookies (issued before this column existed) have no "|" and are
    accepted once, so nobody is force-logged-out by the upgrade itself; the
    next login gives them a versioned one.
    """
    raw = str(user_id or "")
    uid, sep, token = raw.partition("|")
    try:
        user = db.session.get(User, int(uid))
    except (TypeError, ValueError):
        return None
    if user is None:
        return None
    if not sep:
        return user                      # pre-upgrade cookie — honoured once
    if not user.session_token:
        return user                      # never logged in since the upgrade
    # constant-time: this token is a credential like any other
    return user if secrets.compare_digest(token, user.session_token) else None

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
    cand = st.get("running") or st.get("saved") or "default"
    # The live engine may be a key that NO SignalRecord is tagged with (e.g.
    # 'strategy2_live' when S2 is the live engine and the S1 companion only scans),
    # which would silently scope the page to zero trades. Clamp to a real records
    # strategy so the page always shows the tracked track record.
    return cand if cand in BT.STRATEGIES else "all"


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
@admin_required
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
            else:
                # A closed record that is neither a clean win nor a loss (e.g. a
                # MANUAL_CLOSE that kept its booked PnL) ENDS the current streak
                # rather than being silently stepped over (which would let a run
                # of wins/losses incorrectly span across it).
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

        # Which engine is the live account actually trading? Frames the page +
        # the risk-model card around Strategy 2 when it's the armed live engine.
        import config as _config
        s2_live = (_config.read_env_var("STRATEGY2_LIVE", "false") or "false").strip().lower() \
            in ("1", "true", "yes", "on")
        if s2_live:
            try:
                _ms = int(_config.read_env_var("STRATEGY2_LIVE_MIN_SCORE", "85") or 85)
            except (TypeError, ValueError):
                _ms = 85
            live_engine = {
                "key": "strategy2_live",
                "name": "Strategy 2 — TV.pine Confluence (15m)",
                "tp": "Single TP 2R (100%)",
                "sl": "SL: ATR (≤4%)",
                "note": f"Live only on high conviction · long ≥{_ms} / short ≤{100 - _ms}",
            }
        else:
            live_engine = {
                "key": "default",
                "name": "Strategy 1 — Wolf Confluence (1h)",
                "tp": "TP1 1R · TP2 2R",
                "sl": "SL: ATR (≤4%)",
                "note": "5+ light confluence + BTC regime",
            }

        # Reconciliation against the exchanges' own records. Computed here
        # (not in the browser) so the page states the gap rather than leaving
        # the reader to compare two tabs. Fail-soft: an API blip must never
        # take the page down, and "cannot tell" is not "discrepancy".
        try:
            import executor as _ex
            import strategy3_exec as _s3
            reconcile = perf_reconcile(
                wins + losses, wins,
                _ex.realized_pnl_summary(), _s3.closed_pnl_summary(limit=400))
        except Exception as exc:  # noqa: BLE001
            print(f"[perf] reconcile unavailable: {exc}")
            reconcile = {"verdict": None, "note": "", "tracked_closed": wins + losses}

        return render_template(
            "performance.html",
            records=records,
            reconcile=reconcile,
            stats=stats,
            tier_stats=tier_stats,
            queued_signals=queued_signals,
            analytics=analytics,
            user=current_user,
            perf_strategy=_perf_strategy_context(strategy),
            live_engine=live_engine,
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
@admin_required
def get_performance_stats():
    """Return aggregate analytics data for charts (scoped to the selected strategy)."""
    records = _filter_records_by_strategy(
        SignalRecord.query.order_by(SignalRecord.timestamp.asc()).all(),
        _perf_selected_strategy())
    records = get_qualified_records(records)
    
    # Cumulative PnL over time — accumulate in EXIT-time order (the same axis the
    # points are labelled with). The query is ordered by ENTRY time, so with
    # overlapping/concurrent trades a late-exiting early entry would otherwise be
    # summed before an earlier-exiting later entry, making the curve zig-zag in time.
    cumulative_pnl = []
    running_pnl = 0
    closed_in_exit_order = sorted(
        (r for r in records if r.pnl_pct is not None),
        key=lambda r: r.exit_timestamp or r.timestamp,
    )
    for r in closed_in_exit_order:
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
@admin_required
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


# ── public landing page (the promo surface — NO login) ──────────────────────
# Everything else on this site is behind auth, so a shared link used to dump
# visitors on a bare login form. /welcome is the shareable front door: what
# the system is, LIVE honest numbers (the same outcome stats the Telegram
# scorecard publishes — evidence next to the claim), and the group-join CTA.
# PUBLIC-SAFE BY CONSTRUCTION: only aggregated signal stats ever appear here —
# never balances, positions or per-account P&L (enforced by test).
_public_stats_cache = {"ts": 0.0, "data": None}
PUBLIC_STATS_TTL = 300


def _invite_url() -> str:
    """The group's CURRENT invite link for every public join button.

    Was `config.TELEGRAM_INVITE_URL` read straight from .env. On 2026-08-11
    that value turned out to be a REVOKED link — the group's invite had been
    regenerated and .env never caught up, so every join button on /welcome,
    /tw and /us pointed at a dead invite. telegram_utils.group_invite_link()
    asks Telegram for the live one (cached 15 min) and falls back to the env
    value only when the API cannot answer.
    """
    try:
        import telegram_utils
        return telegram_utils.group_invite_link()
    except Exception:  # noqa: BLE001 — a join button must never 500 a page
        import config as _c
        return getattr(_c, "TELEGRAM_INVITE_URL", "") or ""


@app.route("/join")
def join_group():
    """Public 302 → the group's live Telegram invite.

    WHY THIS EXISTS. A private-group invite is `t.me/+HASH`, and the `+` does
    not survive being passed around. Threads' in-app browser strips it, and
    `t.me/HASH` without the plus is not an invite at all — t.me treats it as a
    username, finds nothing, and redirects to telegram.org's homepage. The
    reader gets "a new era of messaging" instead of a Join button and assumes
    the group is dead. Measured 2026-08-11:

        t.me/+__YKMoQF32czNDE1  → 200, Join Group Chat     ✅
        t.me/__YKMoQF32czNDE1   → 302 telegram.org         ❌  (same link, no +)

    A plain path with no reserved characters cannot be mangled by anything, so
    this is what gets published. It also means the invite can be rotated
    without touching a single post that is already live.
    """
    target = _invite_url()
    if not target:
        return redirect(url_for("welcome"))
    # 302, not 301: browsers cache a 301 forever, which would pin the redirect
    # to whichever invite happened to be live the first time someone tapped it.
    return redirect(target, code=302)


def _public_stats() -> dict:
    """Aggregated, account-free stats for the landing page. Cached; every
    branch fail-safe — a missing state file renders as absence, never a 500."""
    now = time.time()
    if _public_stats_cache["data"] is not None \
            and now - _public_stats_cache["ts"] < PUBLIC_STATS_TTL:
        return _public_stats_cache["data"]
    import morning_brief
    out = {"outcomes": {}, "signals": {}}
    try:
        out["outcomes"] = morning_brief._outcome_stat(now) or {}
    except Exception as e:  # noqa: BLE001 — stats are decoration on this page
        print(f"[welcome] outcome stat unavailable: {e}")
    try:
        out["signals"] = morning_brief._signal_tally(now) or {}
    except Exception as e:  # noqa: BLE001
        print(f"[welcome] signal tally unavailable: {e}")
    _public_stats_cache.update(ts=now, data=out)
    return out


@app.route("/welcome")
def welcome():
    return render_template("welcome.html", stats=_public_stats(),
                           invite_url=_invite_url(),
                           registration_enabled=ALLOW_PUBLIC_REGISTRATION)


@app.route("/login", methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or "unknown"
        if _login_blocked(ip):
            flash('Too many failed attempts. Try again in a few minutes.', 'danger')
            return render_template("login.html", registration_enabled=ALLOW_PUBLIC_REGISTRATION,
                                   invite_url=_invite_url()), 429
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            _login_fails.pop(ip, None)          # clear the counter on success
            session.permanent = True            # enrolls the idle-timeout above
            # ONE ACCOUNT, ONE ACTIVE LOGIN: rotating the token here is what
            # signs out every other device holding this account's cookie. It
            # must happen BEFORE login_user(), because login_user() reads
            # get_id() to build the new cookie — rotate afterwards and the
            # fresh cookie would carry the OLD token and lock the new session
            # out instead of the old one.
            user.new_session_token()
            user.last_login_at = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
            user.last_login_ip = ip
            db.session.commit()
            login_user(user)
            return redirect(url_for('index'))
        else:
            _record_login_fail(ip)
            flash('Login failed. Check your username and password.', 'danger')

    return render_template("login.html", registration_enabled=ALLOW_PUBLIC_REGISTRATION,
                           invite_url=_invite_url())

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

    # 🧩 Per-user row order. Rendered into the page (not fetched after load)
    # so the saved layout is correct in the first paint instead of shuffling.
    try:
        import dashboard_layout
        _lay = dashboard_layout.load(current_user.get_id())
        layout_css = dashboard_layout.style_block(_lay)
        layout_cards = dashboard_layout.cards_for_editor(_lay)
    except Exception as e:  # noqa: BLE001 — a layout must never break the page
        print(f"Dashboard layout error: {e}")
        layout_css, layout_cards = "", []

    return render_template(
        "index.html", 
        layout_css=layout_css,
        layout_cards=layout_cards,
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

@app.route("/api/dashboard_layout", methods=["GET", "POST", "DELETE"])
@admin_required
def api_dashboard_layout():
    """Read / save / reset the dashboard row order. ADMIN ONLY.

    Keyed on the session user, never on anything the client sends: a layout is
    trivial data, but accepting a user id from the body would let one account
    rewrite another's page.

    admin_required rather than login_required (2026-08-14, owner's request).
    The editor UI is hidden from non-admins in the template, but hiding a
    button is a suggestion, not a control — the route is what actually decides,
    and a hidden button plus an open endpoint is the shape of most access-
    control bugs. Non-admins keep READING their own layout because the
    dashboard renders it server-side and never calls this.
    """
    import dashboard_layout
    uid = current_user.get_id()
    # An admin's save also publishes the site default that every non-admin
    # reads (owner's decision, 2026-08-15). Taken from the SESSION, never from
    # the request body — the same rule as uid above.
    is_admin = bool(getattr(current_user, "is_admin", False))
    try:
        if request.method == "DELETE":
            lay = dashboard_layout.reset(uid, is_admin=is_admin)
        elif request.method == "POST":
            lay = dashboard_layout.save(uid, request.get_json(silent=True) or {},
                                        is_admin=is_admin)
        else:
            lay = dashboard_layout.load(uid)
        return jsonify({"ok": True, **lay,
                        "cards": dashboard_layout.cards_for_editor(lay)})
    except Exception as e:  # noqa: BLE001
        print(f"Dashboard layout API error: {e}")
        return jsonify({"ok": False, "error": str(e)[:150]}), 200


@app.route("/api/scan_data")
@login_required
def get_scan_data():
    data = load_data()
    data["age_sec"] = scan_age_seconds(data.get("last_update"))
    data["interval_min"] = CHECK_INTERVAL_MINUTES
    # Freshness poll: when the dashboard already holds this scan (?since= matches
    # last_update) it only needs status/age, so skip the DB sweep and drop the
    # ~300 KB signals payload. The client re-renders only on a NEW scan anyway,
    # so this turns the frequent poll into a ~1 KB no-op.
    since = request.args.get("since")
    if since and since == data.get("last_update"):
        data.pop("signals", None)
        return jsonify(data)
    records = get_qualified_records(SignalRecord.query.all())
    cleaned_signals = clean_signals(data.get("signals", []), records)
    data["signals"] = cleaned_signals
    return jsonify(data)


# BTC daily-ATR volatility regime, cached 30 min (daily bars barely move).
# DEAD vol is the confluence killer — the chip warns when signals fire into chop.
_vol_regime_cache = {"ts": 0.0, "data": None}


def _btc_vol_regime():
    now = time.time()
    if _vol_regime_cache["data"] is not None and now - _vol_regime_cache["ts"] < 1800:
        return _vol_regime_cache["data"]
    data = {"vol_regime": "unknown", "vol_atr_pct": None, "vol_ratio": None}
    try:
        ohlcv = rest_client.call("fetch_ohlcv", "BTC/USDT:USDT", "1d", None, 46)
        if ohlcv and len(ohlcv) >= 30:
            trs = []
            for i in range(1, len(ohlcv)):
                h, l = float(ohlcv[i][2]), float(ohlcv[i][3])
                pc = float(ohlcv[i - 1][4])
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            closes = [float(c[4]) for c in ohlcv[1:]]
            # ATR% series (14-bar simple rolling mean of TR, as % of close)
            atr_pct = [sum(trs[i - 13:i + 1]) / 14 / closes[i] * 100
                       for i in range(13, len(trs))]
            cur = atr_pct[-1]
            base = sum(atr_pct[:-1]) / len(atr_pct[:-1])
            ratio = cur / base if base > 0 else 1.0
            data = {
                "vol_regime": "high" if ratio > 1.25 else ("dead" if ratio < 0.75 else "normal"),
                "vol_atr_pct": round(cur, 2),
                "vol_ratio": round(ratio, 2),
            }
    except Exception as exc:  # noqa: BLE001 — the chip just shows unknown
        print(f"[web] vol regime note: {exc}")
    _vol_regime_cache["ts"] = now
    _vol_regime_cache["data"] = data
    return data


@app.route("/api/dashboard_summary")
@login_required
def get_dashboard_summary():
    summary = build_dashboard_summary(load_data())
    summary.update(_btc_vol_regime())
    return jsonify(summary)


@app.route("/api/pulse")
@login_required
def api_pulse():
    """Market Pulse — the dashboard's headline composite plus breadth, sector
    rotation, movers and the BTC trend window. pulse.compute() is disk-cached
    at the feed level, so this route is cheap enough to poll."""
    try:
        return jsonify(pulse.compute())
    except Exception as e:  # noqa: BLE001 — the board degrades, it never 500s
        return jsonify({"score": None, "label": "無法計算", "tone": "off",
                        "components": [], "confidence": {"pct": 0, "ok": 0, "total": 5,
                                                         "missing": []},
                        "breadth": {}, "sectors": [], "movers": {"up": [], "down": []},
                        "btc": {"ok": False, "series": []}, "errors": [str(e)]}), 200


# Main-coin hero tiles (BTC + ETH): price/24h + funding + OI Δ + meter score +
# a 24h sparkline in ONE payload, cached 60s so the tile refresh stays cheap.
_main_coins_cache = {"ts": 0.0, "data": None}


@app.route("/api/main_coins")
@login_required
def api_main_coins():
    import strategy2_meter
    now = time.time()
    if _main_coins_cache["data"] is not None and now - _main_coins_cache["ts"] < 60:
        return jsonify(_main_coins_cache["data"])
    coins = []
    for sym in ("BTC/USDT:USDT", "ETH/USDT:USDT"):
        try:
            # Derived too: this feeds compute_meter, so a literal here is the
            # same stale-threshold trap that disarmed the S2 scanner for three
            # days. Two symbols, so the extra bars cost nothing.
            ohlcv = rest_client.call("fetch_ohlcv", sym, "1h", None, chart_candles())
            t = rest_client.call("fetch_ticker", sym)
            meter = strategy2_meter.compute_meter(ohlcv)
            funding = None
            try:
                fr = rest_client.call("fetch_funding_rate", sym)
                funding = (fr or {}).get("fundingRate")
            except Exception:  # noqa: BLE001 — funding is decoration here
                pass
            coins.append({
                "base": sym.split("/")[0],
                "symbol": sym,
                "price": t.get("last"),
                "change_pct": t.get("percentage"),
                "high": t.get("high"),
                "low": t.get("low"),
                "volume_usdt": t.get("quoteVolume"),
                "funding": funding,
                "score": meter.get("score"),
                "bias": meter.get("bias"),
                "spark": [float(c[4]) for c in (ohlcv or [])[-25:]],   # last 24h of 1h closes
            })
        except Exception as exc:  # noqa: BLE001 — a dead tile beats a dead page
            print(f"[web] main_coins note {sym}: {exc}")
    try:
        oi = market_intel.oi_change(("BTC/USDT:USDT", "ETH/USDT:USDT"))
        for c in coins:
            c["oi_change_24h_pct"] = oi["rows"].get(c["symbol"])
    except Exception:  # noqa: BLE001
        pass
    payload = _json_safe({"generated_at": int(now), "coins": coins})
    _main_coins_cache["ts"] = now
    _main_coins_cache["data"] = payload
    return jsonify(payload)


@app.route("/funnel")
@login_required
def funnel():
    data = load_data()
    return render_template(
        "funnel.html", funnel=build_funnel(data), user=current_user,
        best_s1=build_best_s1_trade(data), best_s3=build_best_s3_trade(),
    )


@app.route("/api/funnel")
@login_required
def get_funnel():
    data = load_data()
    payload = build_funnel(data)
    payload["best_s1"] = build_best_s1_trade(data)
    payload["best_s3"] = build_best_s3_trade()
    return jsonify(_json_safe(payload))


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


def _s1_runtime() -> dict:
    """What the S1 bot process recorded about ITSELF at startup —
    {'exec': 'binance'|'bybit'|'scan_only', 'strategy': key}. Empty when the
    marker is missing (an old bot that predates the field, or never started)."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "bot_strategy.json")) as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001 — no marker = nothing claimed
        return {}


def _s1_mode() -> str:
    """'binance' | 'bybit' | 'scan_only' — how the running S1 bot executes.

    The bot records this at startup. A marker without the field (any bot
    started before it existed) falls back to the bot LOCK, which is the one
    unambiguous signal available: bot.py acquires it ONLY in Binance-trading
    mode — SCAN_ONLY and S1_EXEC=bybit both skip it deliberately, so that the
    S2 engine's "refuse to trade while S1 holds the lock" rule stays correct.
    Defaulting a missing field to 'binance' instead would keep printing the
    exact false claim this replaced."""
    mode = (_s1_runtime().get("exec") or "").strip()
    if mode in ("binance", "bybit", "scan_only"):
        return mode
    try:
        import strategy2_live as S2L
        if S2L.s1_bot_running():
            return "binance"          # holding the lock means Binance, always
    except Exception:  # noqa: BLE001
        pass
    # No lock → a companion. Which kind is whatever run_all.sh read at launch;
    # an .env edited since is itself flagged by the restart-needed check.
    import config as _config
    mirror = (_config.read_env_var("S1_BYBIT_MIRROR", "false") or "false") \
        .strip().lower() in ("1", "true", "yes", "on")
    return "bybit" if mirror else "scan_only"


# The set of things ACTUALLY placing real orders, across BOTH accounts.
#
# Deliberately NOT _live_strategy_state(). That function answers the admin
# SWITCHER's question — S1 or S2, on the Binance account — and answering "none"
# is correct there when neither is armed. /health was printing that answer under
# the heading "live engine", so with STRATEGY3_LIVE=true and the S1 Bybit mirror
# on, the page said "no live engine detected" directly above a process list that
# marked S1 LIVE and S3 "LIVE on BYBIT". Two contradictory claims, both wrong:
# the real answer is that S3 and S1-via-mirror are both trading, on Bybit, and
# Binance is not being traded at all.
def _live_engines(running: set = None) -> list:
    """[{key, name, venue}] — every engine placing real orders right now.
    `running` is the set of process keys known to be alive (from /health's own
    ps pass); a configured-but-dead engine trades nothing."""
    out = []
    running = running if running is not None else set()

    if "s2" in running and _scanner_live_engine():
        out.append({"key": "s2", "name": S2_ENGINE_NAME, "venue": "Binance"})

    if "bot" in running:
        import backtest as BT
        mode = _s1_mode()
        name = (BT.STRATEGIES.get(_s1_runtime().get("strategy")) or
                BT.STRATEGIES.get("default") or {}).get("name") or "Strategy 1"
        if mode == "binance":
            out.append({"key": "bot", "name": name, "venue": "Binance"})
        elif mode == "bybit":
            try:
                import s1_bybit_mirror
                armed = s1_bybit_mirror.enabled()
            except Exception:  # noqa: BLE001
                armed = False
            if armed:
                out.append({"key": "bot", "name": f"{name} 🪞", "venue": "Bybit"})

    if "s3" in running:
        try:
            if strategy3_scanner.mode_string() == "LIVE on BYBIT":
                out.append({"key": "s3", "name": "Strategy 3 — Vegas Flag Flip",
                            "venue": "Bybit"})
        except Exception:  # noqa: BLE001 — /health must never 500 on this
            pass
    return out


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
            # Newest record per (symbol, DIRECTION). Keying on symbol alone let an
            # old closed or opposite-direction trade on the same coin flag a
            # perfectly-healthy live position as 'diverged'; matching the side too
            # removes that whole class of false warnings on the live-money screen.
            latest = {}
            for rec in (SignalRecord.query
                        .filter(SignalRecord.symbol.in_(symbols))
                        .order_by(SignalRecord.timestamp.desc()).all()):
                latest.setdefault((rec.symbol, (rec.direction or "").upper()), rec)
            for p in positions:
                rec = latest.get((p.get("symbol"), (p.get("side") or "").upper()))
                status = (rec.status if rec else None)
                p["db_status"] = status
                # Bot thinks this is closed (or never tracked it) but it's live.
                p["db_diverged"] = bool(status and status not in ("PENDING", "TP1_PARTIAL"))
    except Exception as exc:  # noqa: BLE001 — enrichment must never break the snapshot
        print(f"[account] db-status enrichment note: {exc}")
    return jsonify(_json_safe(snap))


@app.route("/api/account/history")
@admin_required
def api_account_history():
    """Realized-P&L history (bot + manual trades) straight from Binance."""
    return jsonify(_json_safe(executor.realized_pnl_history(limit=80)))


@app.route("/api/performance/real")
@admin_required
def api_performance_real():
    """Real Binance account P&L (realized + fees + funding) for the performance
    page's 'Live Binance' panel — the ground truth the simulated stats reconcile
    against. Distinct from the DB-record analytics on the same page."""
    return jsonify(_json_safe(executor.realized_pnl_summary()))


@app.route("/api/performance/bybit")
@admin_required
def api_performance_bybit():
    """Real Bybit sub-account P&L (Strategy 3 flips only) for the performance
    page's Bybit panel — the exchange-side counterpart to /api/performance/real,
    same shape so both panels share frontend rendering code."""
    return jsonify(_json_safe(strategy3_exec.closed_pnl_summary()))


# MAE/MFE excursions are immutable once a trade closes, so each record is
# computed once and kept for the process lifetime. First load walks the klines
# for every uncached trade (~0.3s each); later loads are instant.
#
# "Instant" was only ever true for trades that SUCCEEDED. A record that could
# not be computed cached nothing, so every load re-walked the klines for it and
# failed again the same way: 3 unusable trades cost 6.6s on every single load,
# permanently, while the docstring promised a warm cache. A closed trade is
# immutable — a verdict of "cannot compute this" is exactly as final as a
# number, and must be remembered with the same confidence.
#
# Transient failures are the one thing that must NOT be remembered forever, so
# only decisions taken from data we actually hold become permanent skips;
# exceptions get a bounded number of retries instead of an infinite one.
_excursion_cache: dict = {}
_excursion_fail: dict = {}          # record id -> attempts (None = permanent)
_EXCURSION_MAX_TRIES = 3


def _permanent_symbol_errors():
    """ccxt errors that mean 'this market does not exist', not 'try later'."""
    try:
        import ccxt
        return (ccxt.BadSymbol,)
    except Exception:  # noqa: BLE001 — degrade to the bounded-retry path
        return ()


_PERMANENT_SYMBOL_ERRORS = _permanent_symbol_errors()


@app.route("/api/performance/excursions")
@admin_required
def api_performance_excursions():
    """Trade autopsy: for each closed bot-tracked trade, replay the 15m candles
    between entry and exit and measure the Max Adverse / Max Favorable Excursion
    (worst drawdown vs best unrealized profit, % from entry). Answers THE tuning
    question — do losers die instantly (bad entries) or nearly win first (stop
    and target placement)? DB-tracked trades, not the exchange income records."""
    tz8 = timezone(timedelta(hours=8))      # SignalRecord times are naive GMT+8
    rows = (SignalRecord.query
            .filter(SignalRecord.exit_timestamp.isnot(None),
                    SignalRecord.entry_price.isnot(None))
            .order_by(SignalRecord.exit_timestamp.desc())
            .limit(80).all())
    points, skipped = [], 0
    for r in rows:
        cached = _excursion_cache.get(r.id)
        if cached is not None:
            points.append(cached)
            continue
        if _excursion_fail.get(r.id, 0) is None or \
                _excursion_fail.get(r.id, 0) >= _EXCURSION_MAX_TRIES:
            skipped += 1                       # already settled — do not refetch
            continue
        try:
            t0 = int(r.timestamp.replace(tzinfo=tz8).timestamp() * 1000)
            t1 = int(r.exit_timestamp.replace(tzinfo=tz8).timestamp() * 1000)
            entry = float(r.entry_price)
            if t1 <= t0 or entry <= 0:
                _excursion_fail[r.id] = None   # a property of the row itself
                skipped += 1
                continue
            bars = min(int((t1 - t0) / 900_000) + 3, 500)
            ohlcv = rest_client.call("fetch_ohlcv", r.symbol, "15m", t0, bars)
            window = [c for c in (ohlcv or []) if t0 <= c[0] <= t1]
            if not window:
                # The fetch worked; the exchange simply has no candles covering
                # this closed trade. That will not change tomorrow.
                _excursion_fail[r.id] = None
                skipped += 1
                continue
            hi = max(float(c[2]) for c in window)
            lo = min(float(c[3]) for c in window)
            if (r.direction or "").upper() == "LONG":
                mae = max(0.0, (entry - lo) / entry * 100)
                mfe = max(0.0, (hi - entry) / entry * 100)
            else:
                mae = max(0.0, (hi - entry) / entry * 100)
                mfe = max(0.0, (entry - lo) / entry * 100)
            sl_pct = abs(entry - float(r.sl_price)) / entry * 100 if r.sl_price else None
            tp_ref = r.tp2_price or r.tp1_price
            tp_pct = abs(float(tp_ref) - entry) / entry * 100 if tp_ref else None
            p = {"symbol": r.symbol, "direction": r.direction, "status": r.status,
                 "pnl_pct": r.pnl_pct, "mae": round(mae, 2), "mfe": round(mfe, 2),
                 "sl_pct": round(sl_pct, 2) if sl_pct is not None else None,
                 "tp_pct": round(tp_pct, 2) if tp_pct is not None else None,
                 "held_min": int(round((t1 - t0) / 60_000))}
            _excursion_cache[r.id] = p
            points.append(p)
        except RateLimitCooldownError:
            # Return what we have — the page shows a partial set and the next
            # auto-refresh finishes the rest from cache.
            return jsonify(_json_safe({"ok": True, "partial": True,
                                       "points": points, "skipped": skipped}))
        except _PERMANENT_SYMBOL_ERRORS:
            # The symbol is gone from the exchange (AERGO was delisted, and its
            # two closed trades are still in the DB forever). No amount of
            # retrying brings a delisted market back.
            _excursion_fail[r.id] = None
            skipped += 1
        except Exception:  # noqa: BLE001 — one bad record must not kill the panel
            # Might be a network blip, so this one gets retried — but a bounded
            # number of times. An unbounded retry is what made the panel slow.
            _excursion_fail[r.id] = _excursion_fail.get(r.id, 0) + 1
            skipped += 1
    return jsonify(_json_safe({"ok": True, "partial": False,
                               "points": points, "skipped": skipped}))


def _json_safe(obj):
    """Recursively replace non-finite floats (inf/-inf/nan) with None. jsonify
    otherwise emits the bare tokens `Infinity`/`NaN`, which are invalid JSON and
    make the browser's response.json() throw — silently breaking whichever panel
    consumed the endpoint (e.g. profit_factor=inf after an all-green run)."""
    import math
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


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


@app.route("/bybit")
@admin_required
def bybit_page():
    """Live Bybit account page for Strategy 3 (Vegas Flag Flip) — balance,
    positions and per-symbol signal status, so you never need to open the
    Bybit app to see what the flip engine is doing with real money."""
    import config
    per_symbol = {}
    for base in config.STRATEGY3_SYMBOLS:
        p = config.strategy3_params(base)
        p["notional"] = p["margin"] * p["leverage"]
        per_symbol[base] = p
    return render_template(
        "bybit.html",
        user=current_user,
        status_line=strategy3_scanner.status_line(),
        cfg={
            "symbols": config.STRATEGY3_SYMBOLS,
            "per_symbol": per_symbol,
            "score_th": config.STRATEGY3_SCORE_TH,
            "adx_th": config.STRATEGY3_ADX_TH,
            "emergency_sl_pct": config.STRATEGY3_EMERGENCY_SL_PCT,
        },
    )


@app.route("/api/bybit")
@admin_required
def api_bybit():
    """Read-only JSON snapshot of the live Bybit account + each symbol's last
    known signal state (flag/vegas/msb/score), so the page can explain WHY a
    position is or isn't open without recomputing the strategy itself."""
    snap = strategy3_exec.account_snapshot()
    state = strategy3_scanner.load_state()
    status = []
    for sym in strategy3_scanner.symbols():
        st = state.get(sym, {})
        status.append({
            "symbol": sym,
            "base": sym.split("/")[0],
            "engine": st.get("engine") or "flagflip",
            "last_flag": st.get("last_flag"),
            "pos_dir": st.get("pos_dir"),
            "score": st.get("last_score"),
            "vegas": st.get("last_vegas"),
            "msb": st.get("last_msb"),
            "trend": st.get("last_trend"),
            "seen": st.get("last_seen"),
        })
    snap["status"] = status
    snap["blocked"] = strategy3_scanner.live_blocked()
    return jsonify(_json_safe(snap))


@app.route("/api/bybit/close", methods=["POST"])
@admin_required
def api_bybit_close():
    """MANUAL: close an open Bybit flip position at market (reduce-only). The
    scanner notices the position is gone on its next poll (~45s) and stands
    down for that symbol until the next flag — same as an SL hit or a close
    made directly on Bybit."""
    symbol = request.form.get("symbol")
    if symbol not in set(strategy3_scanner.symbols()):
        return jsonify({"ok": False, "error": "unknown symbol"}), 400
    return jsonify(strategy3_exec.close_flip(symbol))


# --- System health (/health ops page) ---------------------------------------
# Answers, at a glance, the three questions that otherwise need a terminal:
# is every process alive, does anything need a RESTART to pick up new code,
# and what did the logs last complain about. The page itself is READ-ONLY —
# one `ps` and some file reads. The single exception is the explicit 重啟 button
# (POST /api/restart), which hands the job to restart.sh; nothing here ever
# stops the stack without being asked.

# autoheal.log is here deliberately: the launchd auto-restart agent failed on
# EVERY run for 16 days (macOS blocks launchd-spawned bash from reading anything
# under ~/Desktop) and nobody saw it, because the one log that would have said so
# was the one log /health never opened. A watchdog nobody watches is not a
# watchdog.
_HEALTH_LOGS = ("app.log", "bot.log", "strategy2.log", "strategy3.log", "autoheal.log")

# (key, command regex, entry module). Which files make a process stale is
# DERIVED from the entry module's import closure (restart_ctl.sources) rather
# than hand-listed. The hand list this replaced had drifted both ways: it never
# mentioned strategy3_risk.py or strategy_ledger.py, so editing the S3 circuit
# breaker left /health saying S3 was up to date — and it DID list
# strategy2_meter.py and indicators.py, which strategy3_scanner never imports,
# so unrelated edits raised a restart flag that meant nothing.
_HEALTH_PROCS = tuple((key, pattern, entry)
                      for key, pattern, entry in restart_ctl.PROCS)


def _build_pipeline(base, now) -> dict:
    """LINE quota + site link + backup freshness — the family notification
    pipeline. Unlike a crashed process, these fail SILENTLY (a message just
    never arrives), so /health has to surface them explicitly."""
    import glob

    import backup_state
    import line_push
    import site_link

    q = line_push.quota_cached()
    line_info = {
        "enabled": line_push.enabled(),
        "used": q.get("used"),
        "limit": q.get("limit"),
        "checked_ago_sec": (int(now.timestamp() - q["checked_at"])
                            if q.get("checked_at") else None),
    }

    link_info = {"url": site_link.current_url()}

    zips = sorted(glob.glob(os.path.join(backup_state.BACKUP_DIR, "state-*.zip")))
    backup_info = {"exists": False, "age_hours": None, "size_bytes": None,
                   "offsite_configured": bool(backup_state.OFFSITE_DIR
                                              and os.path.isdir(os.path.dirname(
                                                  backup_state.OFFSITE_DIR.rstrip("/")))),
                   "offsite_ok": False}
    if zips:
        latest = zips[-1]
        st = os.stat(latest)
        backup_info.update(
            exists=True,
            age_hours=round((now.timestamp() - st.st_mtime) / 3600, 1),
            size_bytes=st.st_size,
            offsite_ok=os.path.exists(os.path.join(backup_state.OFFSITE_DIR,
                                                    os.path.basename(latest))),
        )
    return {"line": line_info, "site_link": link_info, "backup": backup_info}


def _ps_snapshot():
    """One `ps` pass → [{pid, started, rss_kb, cmd}] for every process."""
    import restart_ctl
    return restart_ctl.ps_snapshot()


_SECRET_RE = re.compile(
    r"(bot)\d{6,}:[A-Za-z0-9_\-]{20,}"            # Telegram bot token in a URL
    r"|(?i:(api[_-]?key|secret|token|password)=)[^\s&\"']+")


def _scrub(line: str) -> str:
    """Remove credentials from a log line before it is rendered in a browser.

    This page tails raw logs, and requests embeds the full request URL in its
    exception messages — so a Telegram error printed by any process put a LIVE
    bot token into last_error, 220 chars being ample for a whole one. The
    emitters redact at the source now, but this is the layer that also covers
    lines already on disk and any future module that forgets.
    """
    return _SECRET_RE.sub(lambda m: (m.group(1) + "***") if m.group(1) else "***", line)


def _log_health(base):
    """Size / last write / error lines per stack log (tail ~64 KB each).

    NOTE ON THE WINDOW: these lines carry no timestamps, so "errors" means
    "inside the last 64 KB", not "in the last hour". On a chatty log that is
    roughly the last few minutes; on a quiet one it can reach back weeks — the
    strategy3 rate-limit errors kept showing here for 13 days after they
    stopped. Read the count next to written_ago_sec, never on its own.

    The pattern matches whole-word `error` plus the shell/OS failures that a
    launchd agent produces, which the old regex missed entirely: 4550 copies of
    "Operation not permitted" scored zero. Verified against all five live logs —
    the added alternatives introduce no false positives on trading output such
    as "volume gate failed".
    """
    import re
    err_re = re.compile(
        r"traceback|exception|critical|fatal|"
        r"operation not permitted|permission denied|command not found|"
        r"no such file|cannot execute|\berror\b",
        re.IGNORECASE,
    )
    logs = []
    for name in _HEALTH_LOGS:
        path = os.path.join(base, "logs", name)
        entry = {"name": name, "exists": os.path.exists(path), "size": 0,
                 "written_ago_sec": None, "last_line": None,
                 "recent_errors": 0, "last_error": None}
        if entry["exists"]:
            try:
                st = os.stat(path)
                entry["size"] = st.st_size
                entry["written_ago_sec"] = max(0, int(time.time() - st.st_mtime))
                with open(path, "rb") as f:
                    f.seek(max(0, st.st_size - 65536))
                    tail = f.read().decode("utf-8", "replace")
                lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
                if lines:
                    entry["last_line"] = _scrub(lines[-1])[:220]
                errs = [ln for ln in lines if err_re.search(ln)]
                entry["recent_errors"] = len(errs)
                if errs:
                    entry["last_error"] = _scrub(errs[-1])[:220]
            except Exception:  # noqa: BLE001
                pass
        logs.append(entry)
    return logs


def perf_reconcile(tracked_closed: int, tracked_wins: int,
                   real: dict, bybit: dict) -> dict:
    """Compare the tracked signal record against the exchanges' own records.

    /performance shows both and says they "differ on purpose" — true, but it
    left the reader to spot the size of the gap. The tracked record contains
    PHANTOM entries: signals the bot recorded and then scored even though the
    order skipped, failed, or closed differently in reality. Stating the gap
    makes those visible instead of merely disclosed.

    Pure and total: takes already-fetched summaries, never raises, and returns
    verdict=None when it genuinely cannot tell (an API error is not evidence
    of a discrepancy).
    """
    real_ok = bool((real or {}).get("ok"))
    by_ok = bool((bybit or {}).get("ok"))
    real_n = int((real or {}).get("n_trades") or 0) if real_ok else 0
    by_n = int((bybit or {}).get("n_trades") or 0) if by_ok else 0
    exch_n = real_n + by_n
    out = {
        "tracked_closed": tracked_closed,
        "tracked_wins": tracked_wins,
        "exchange_trades": exch_n if (real_ok or by_ok) else None,
        "exchange_net": (round(float((real or {}).get("net") or 0.0)
                               + float((bybit or {}).get("net") or 0.0), 2)
                         if (real_ok or by_ok) else None),
        "verdict": None, "note": "",
    }
    if not (real_ok or by_ok):
        out["note"] = "無法讀取交易所紀錄，暫時無法對帳。"
        return out
    if tracked_closed and exch_n == 0:
        out["verdict"] = "phantom"
        out["note"] = (f"追蹤紀錄有 {tracked_closed} 筆已結束訊號，"
                       f"但交易所同期沒有任何成交 — 這些是<b>紙上紀錄</b>，"
                       f"不是真實損益。")
    elif tracked_closed > exch_n * 2 and exch_n:
        out["verdict"] = "diverged"
        out["note"] = (f"追蹤紀錄 {tracked_closed} 筆 vs 交易所 {exch_n} 筆成交 — "
                       f"多數訊號沒有真的下單（跳過或失敗），"
                       f"勝率請以交易所分頁為準。")
    else:
        out["verdict"] = "ok"
        out["note"] = (f"追蹤 {tracked_closed} 筆 · 交易所 {exch_n} 筆成交 — "
                       f"兩者本來就不同（追蹤的是策略，交易所是真錢）。")
    return out


def _daily_risk_status() -> dict:
    """Account-wide daily loss brake, for the /health panel. Never raises —
    this page must render even when the exchange is unreachable."""
    try:
        import daily_risk
        return daily_risk.status()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": False, "error": str(exc)[:150]}


def _auth_health() -> dict:
    """🔒 Guessable logins + the two settings that decide who can get in.

    The dashboard used to hide behind a Cloudflare quick-tunnel URL that
    rotated on every restart; it now has a permanent public address, so
    "nobody knows the link" stopped being a control. An audit on 2026-08-02
    found an ADMIN account whose username and password were both "1".
    Surfaced on /health so it cannot quietly go unnoticed again.
    """
    weak = []
    try:
        import set_password as _sp
        for u in User.query.all():
            why = _sp.weak_reason(u.username, u.password)
            if why:
                weak.append({"username": u.username, "is_admin": bool(u.is_admin),
                             "why": why})
    except Exception as e:  # noqa: BLE001 — never take the ops page down over this
        print(f"[health] credential audit failed: {e}")
    return {"weak_accounts": weak,
            "public_registration": ALLOW_PUBLIC_REGISTRATION,
            "cookie_secure": bool(app.config.get("SESSION_COOKIE_SECURE"))}


def build_health():
    """Full stack snapshot for /health — JSON-safe primitives only."""
    import re
    import shutil
    import config as _config

    base = os.path.dirname(__file__)
    now = datetime.now()
    s2_is_engine = (_config.read_env_var("STRATEGY2_LIVE", "false") or "false") \
        .strip().lower() in ("1", "true", "yes", "on")

    # The S1 role comes from what the bot process recorded about itself, not
    # from STRATEGY2_LIVE. Branching on S2 alone had exactly two answers for
    # three modes, so whenever S3 was the armed engine the page described the
    # S1 mirror as "LIVE engine — places real orders", which reads as Binance —
    # the one exchange that mode deliberately never touches.
    s1_mode = _s1_mode()
    s1_role = {
        "scan_only": "scan-only companion — refreshes the dashboard, places NO orders",
        "bybit": "LIVE on BYBIT via the 🪞 mirror — Binance untouched",
        "binance": "LIVE engine on Binance — scans hourly and places real orders",
    }.get(s1_mode, "LIVE engine on Binance — scans hourly and places real orders")

    labels = {
        "web": ("Web dashboard", f"serves this site on :{os.getenv('FLASK_PORT', '4000')}"),
        "bot": ("S1 bot", s1_role),
        "s2": ("S2 scanner",
               "LIVE engine — trades TV.pine confluence on 15m"
               if s2_is_engine else
               "alert-only companion — feeds /strategy2, places NO orders"),
        "s3": ("S3 flip", f"Vegas Flag Flip on Bybit — {strategy3_scanner.mode_string()}"),
    }

    ps = _ps_snapshot()
    issues, processes = [], []
    for key, pattern, entry in _HEALTH_PROCS:
        matches = [p for p in ps if re.search(pattern, p["cmd"])]
        label, role = labels[key]
        proc = {"key": key, "label": label, "role": role, "live": False,
                "running": bool(matches), "pid": None, "uptime_sec": None,
                "rss_mb": None, "instances": len(matches),
                "restart_needed": False, "changed_files": []}
        if matches:
            m = matches[0]
            proc["pid"] = m["pid"]
            proc["rss_mb"] = round(m["rss_kb"] / 1024, 1)
            if m["started"]:
                proc["uptime_sec"] = max(0, int((now - m["started"]).total_seconds()))
                changed = restart_ctl.changed_since(
                    entry, m["started"].timestamp(), base)
                proc["changed_files"] = changed
                proc["restart_needed"] = bool(changed)
        processes.append(proc)

        if not matches:
            issues.append({"sev": "down",
                           "text": f"{label} is NOT running ({role}).",
                           "fix": "./run_all.sh bg"})
        elif len(matches) > 1:
            issues.append({"sev": "down",
                           "text": f"{label}: {len(matches)} copies are running — "
                                   "duplicates can double-trade.",
                           "fix": "./run_all.sh stop && ./run_all.sh bg"})
        elif proc["restart_needed"]:
            issues.append({"sev": "warn",
                           "text": f"{label} is running OLD code — "
                                   f"{', '.join(proc['changed_files'])} changed after it started.",
                           "fix": "./run_all.sh bg"})

    # Which of them is actually trading. The template used to derive its LIVE
    # badge by regex-matching the role SENTENCE (/LIVE engine/), so rewording a
    # human-readable description silently changed what the page claimed about
    # real money. It is a flag now.
    live_engines = _live_engines({p["key"] for p in processes if p["running"]})
    live_keys = {e["key"] for e in live_engines}
    for p in processes:
        p["live"] = p["key"] in live_keys

    # Scan freshness — S1 writes scan_results.json each sweep, S2 rewrites
    # strategy2_signals.json every ~5 min.
    data = load_data()
    age = scan_age_seconds(data.get("last_update"))
    scan_limit = CHECK_INTERVAL_MINUTES * 60 + 900   # one interval + 15 min grace
    scan = {"last_update": data.get("last_update"), "age_sec": age,
            "interval_min": CHECK_INTERVAL_MINUTES,
            "stale": age is None or age > scan_limit}
    if scan["stale"] and any(p["key"] == "bot" and p["running"] for p in processes):
        issues.append({"sev": "warn",
                       "text": "S1 scan data is stale — the bot is up but hasn't finished "
                               f"a scan in over {CHECK_INTERVAL_MINUTES + 15} minutes.",
                       "fix": "tail -50 app/logs/bot.log"})

    s2 = {"age_sec": None, "live": None, "stale": True}
    try:
        with open(os.path.join(base, "strategy2_signals.json")) as f:
            s2_data = json.load(f) or {}
        s2["age_sec"] = max(0, int(time.time() - float(s2_data.get("generated_at", 0))))
        s2["live"] = bool(s2_data.get("live"))
        s2["stale"] = s2["age_sec"] > 1800
    except Exception:  # noqa: BLE001
        pass
    if s2["stale"] and any(p["key"] == "s2" and p["running"] for p in processes):
        issues.append({"sev": "warn",
                       "text": "Strategy-2 signals are stale — the scanner is up but "
                               "hasn't written a sweep in 30+ minutes.",
                       "fix": "tail -50 app/logs/strategy2.log"})
    scan["s2"] = s2

    # Storage / data files.
    db_path = os.path.join(base, "instance", "signals.db")
    try:
        from sqlalchemy import func
        by_status = {(s or "?"): int(c) for s, c in
                     db.session.query(SignalRecord.status, func.count())
                     .group_by(SignalRecord.status).all()}
    except Exception:  # noqa: BLE001
        by_status = {}
    storage = {
        "db_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
        "records_total": sum(by_status.values()),
        "records_by_status": by_status,
        "scan_file_bytes": os.path.getsize(DATA_FILE) if os.path.exists(DATA_FILE) else 0,
        "disk_free_gb": round(shutil.disk_usage(base).free / 1e9, 1),
        "bot_lock": os.path.exists(os.path.join(base, "bot.lock")),
        "tunnel_running": any("cloudflared" in p["cmd"] for p in ps),
    }
    if storage["disk_free_gb"] < 5:
        issues.append({"sev": "warn",
                       "text": f"Low disk space — {storage['disk_free_gb']} GB free.",
                       "fix": None})

    # 📡 Pipeline — LINE quota, the public site link, backup freshness. These
    # fail silently (a message never arrives, a backup silently stops), so
    # flag them here rather than trusting nothing-looks-wrong.
    auth_info = _auth_health()

    pipeline = _build_pipeline(base, now)
    s2_running = any(p["key"] == "s2" and p["running"] for p in processes)
    pl = pipeline["line"]
    if pl["enabled"] and pl["limit"] and pl["used"] is not None \
            and pl["used"] >= pl["limit"] * 0.8:
        issues.append({"sev": "warn",
                       "text": f"LINE push quota at {pl['used']}/{pl['limit']} this "
                               "month — messages go silent once it runs out.",
                       "fix": None})
    pb = pipeline["backup"]
    if s2_running:
        if not pb["exists"]:
            issues.append({"sev": "warn", "text": "No state backup has been written yet.",
                           "fix": "tail -50 app/logs/strategy2.log"})
        elif pb["age_hours"] > 30:
            issues.append({"sev": "warn",
                           "text": f"Last state backup was {pb['age_hours']:.0f}h ago "
                                   "— expected a fresh one every ~24h.",
                           "fix": "tail -50 app/logs/strategy2.log"})

    # Which engine is trading vs which one .env selects (divergence = pending
    # restart), reusing the admin switcher's ground truth.
    try:
        st = _live_strategy_state()
        engine = {"saved_name": st["saved_name"], "running_name": st["running_name"],
                  "diverged": st["diverged"], "live": live_engines}
        if st["diverged"]:
            issues.append({"sev": "warn",
                           "text": f"Engine divergence — .env selects \"{st['saved_name']}\" "
                                   f"but \"{st['running_name']}\" is the one trading.",
                           "fix": "./run_all.sh bg"})
    except Exception:  # noqa: BLE001
        engine = {"saved_name": None, "running_name": None, "diverged": False,
                  "live": live_engines}

    overall = "ok"
    for i in issues:
        if i["sev"] == "down":
            overall = "down"
            break
        overall = "warn"

    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "overall": overall,
        "issues": issues,
        "engine": engine,
        "processes": processes,
        "scan": scan,
        "logs": _log_health(base),
        "storage": storage,
        "pipeline": pipeline,
        "auth": auth_info,
        "daily_risk": _daily_risk_status(),
    }


@app.route("/health")
@admin_required
def health_page():
    return render_template("health.html", health=build_health(), user=current_user)


# ── 🧵 Meta Threads OAuth ───────────────────────────────────────────────────
# Admin-only on both legs: the callback exchanges whatever `code` it is handed
# for a token that then posts under our name, so an open endpoint would let a
# stranger bind their own Threads account to this bot.
@app.route("/threads/connect")
@admin_required
def threads_connect():
    import threads_post
    if not threads_post.configured():
        return ("THREADS_APP_ID / THREADS_APP_SECRET 還沒設定在 .env。", 400)
    return redirect(threads_post.auth_url())


@app.route("/threads/callback")
@admin_required
def threads_callback():
    import threads_post
    err = request.args.get("error_description") or request.args.get("error")
    if err:
        return (f"❌ Threads 授權被拒絕：{err}", 400)
    code = (request.args.get("code") or "").strip()
    if not code:
        return ("❌ 回呼沒有帶 code。", 400)
    res = threads_post.exchange_code(code)
    if not res.get("ok"):
        return (f"❌ 交換 token 失敗：{res.get('error')}", 400)
    who = res.get("username") or "(帳號名稱讀取失敗，但 token 已存)"
    return (f"✅ Threads 已連結：@{who}<br>接著在 .env 設 "
            f"<code>THREADS_ENABLED=true</code> 並重啟 S2 掃描器。", 200)


@app.route("/api/health")
@admin_required
def api_health():
    return jsonify(build_health())


@app.route("/api/restart", methods=["POST"])
@admin_required
def api_restart():
    """The one write on this page. /health has always been able to SEE that a
    process is running old code while the only cure was a terminal on the Mac;
    this closes that gap. The work is handed to restart.sh, which outlives the
    kill — this request cannot report the outcome because the process serving
    it is one of the things being restarted. Telegram gets the result.

    CSRF is handled by the global protect_post_requests() hook, not here."""
    ok, note = restart_ctl.request("web /health button")
    return jsonify({"ok": ok, "note": note})


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
    strategy2_meter.py. 750 1h candles cover the outer tunnel EMA676 + Vegas SMA5."""
    import strategy2_meter
    try:
        ohlcv = rest_client.call("fetch_ohlcv", symbol, "1h", None, chart_candles())
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
    uses the SAME 1h/750 window the meter does — the on-chart EMAs line up exactly with
    the confidence factors. Distinct path so it never collides with the <path:symbol>
    meter route. Volume is dropped to keep the payload small."""
    try:
        ohlcv = rest_client.call("fetch_ohlcv", symbol, "1h", None, chart_candles())
    except RateLimitCooldownError as exc:
        return jsonify({"symbol": symbol, "error": str(exc), "candles": []}), 200
    except Exception as exc:  # noqa: BLE001 — never 500 the dashboard
        return jsonify({"symbol": symbol, "error": f"fetch failed: {exc}", "candles": []}), 200
    candles = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])]
               for c in (ohlcv or [])]
    return jsonify({"symbol": symbol, "timeframe": "1h", "candles": candles,
                    "ema_defs": chart_ema_defs()})


# Candles behind the chart panes. 1000 is what Binance actually serves for 1h
# (asking 1500 returns 1000), and the meter needs MIN_CANDLES before it can
# score a single bar — so at the old 750 the score line was a 65-point stub,
# 2.7 days, most of it off the left of a 31-day chart. At 1000 it is 315
# points / 13 days, and the 676 tunnel gets 324 bars instead of 74.
# Costs 0.7s to recompute, behind a 5-minute cache. Derived, not a literal:
# a rise in the meter's requirement must not silently shrink the line again.
def chart_candles():
    import strategy2_meter as M
    return max(1000, M.MIN_CANDLES + 314)


def chart_ema_defs():
    """The lines the chart draws, with the tunnel periods taken FROM THE METER.

    They were hardcoded in the page as 288/338 and stayed there when the outer
    tunnel moved to 576/676 on 2026-08-11, so for five days the chart drew a
    tunnel the score was not using while the legend promised "the on-chart EMAs
    line up exactly with the confidence factors". Nothing failed; the picture
    was just answering a faster question than the number beside it.

    strategy2_meter's periods are themselves pinned to the .pine by
    test_tunnel_periods_match_the_pine_indicator, so serving them from here
    chains chart → meter → chart indicator with no hand-copied number left.
    """
    import strategy2_meter as M
    return [
        {"key": "e20",  "len": 20,  "color": "#FFEB3B", "width": 1, "label": "EMA 20"},
        {"key": "e50",  "len": 50,  "color": "#FF9800", "width": 1, "label": "EMA 50"},
        {"key": "e100", "len": 100, "color": "#E040FB", "width": 1, "label": "EMA 100"},
        {"key": "e200", "len": 200, "color": "#2962FF", "width": 2, "label": "EMA 200"},
        {"key": "tia", "len": M.TUNNEL_INNER_A, "color": "#FFB74D", "width": 1,
         "label": f"Tunnel {M.TUNNEL_INNER_A}"},
        {"key": "tib", "len": M.TUNNEL_INNER_B, "color": "#FB8C00", "width": 1,
         "label": f"Tunnel {M.TUNNEL_INNER_B}"},
        {"key": "toa", "len": M.TUNNEL_OUTER_A, "color": "#9CCC65", "width": 1,
         "label": f"Tunnel {M.TUNNEL_OUTER_A}"},
        {"key": "tob", "len": M.TUNNEL_OUTER_B, "color": "#66BB6A", "width": 1,
         "label": f"Tunnel {M.TUNNEL_OUTER_B}"},
    ]


# Historical meter scores are pure recompute over the same candles → cache the
# ~100-point series per symbol for 5 min (one 1h bar can't close faster anyway).
_score_hist_cache: dict = {}


@app.route("/api/strategy2_score_history")
@app.route("/api/strategy2_score_history/<path:symbol>")
@login_required
def api_strategy2_score_history(symbol="BTC/USDT:USDT"):
    """Confidence-meter score per closed 1h bar — the same math as the live
    gauge, replayed over history so the chart can show whether high scores
    actually preceded moves (a visual backtest of the meter)."""
    import strategy2_meter
    now = time.time()
    hit = _score_hist_cache.get(symbol)
    if hit and now - hit[0] < 300:
        return jsonify(hit[1])
    try:
        ohlcv = rest_client.call("fetch_ohlcv", symbol, "1h", None, chart_candles())
    except RateLimitCooldownError as exc:
        return jsonify({"symbol": symbol, "error": str(exc), "points": []}), 200
    except Exception as exc:  # noqa: BLE001 — never 500 the dashboard
        return jsonify({"symbol": symbol, "error": f"fetch failed: {exc}", "points": []}), 200
    points = []
    min_n = strategy2_meter.MIN_CANDLES
    for i in range(min_n, len(ohlcv or []) + 1):
        try:
            m = strategy2_meter.compute_meter(ohlcv[:i])
            points.append([int(ohlcv[i - 1][0]), m["score"]])
        except Exception:  # noqa: BLE001 — skip a bad bar, keep the series
            continue
    payload = {"symbol": symbol, "points": points}
    _score_hist_cache[symbol] = (now, payload)
    return jsonify(payload)


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


# ── Strategies hub (📊 one page, three tabs: S1/S2/S3 rules + live状況) ────────
def _strategies_params() -> dict:
    """Live rule NUMBERS pulled from config so the /strategies rules stay honest
    when a threshold is retuned (the prose lives in the template, the digits here)."""
    import config
    import strategy2_meter
    s3syms = []
    for base in config.STRATEGY3_SYMBOLS:
        p = config.strategy3_params(base)
        s3syms.append({"base": base, "engine": p.get("engine"),
                       "tf": p.get("timeframe"), "leverage": p.get("leverage")})
    return {
        "s1": {
            "tf": ", ".join(config.TIMEFRAMES),
            "lights_long": config.MIN_LIGHTS_FOR_ENTRY,
            "lights_short": config.MIN_LIGHTS_SHORT,
            "leverage": config.LEVERAGE,
            "margin": config.FIXED_MARGIN_USDT,
            "notional": round(config.FIXED_MARGIN_USDT * config.LEVERAGE, 1),
            "sl_mult": config.ATR_SL_MULTIPLIER,
            "max_sl_pct": round(config.MAX_SL_PCT * 100, 1),
            "tp1_r": config.ATR_TP1_MULTIPLIER,
            "tp2_r": config.ATR_TP_MULTIPLIER,
            "rsi_hi": int(config.RSI_UPPER_THRESHOLD),
            "rsi_lo": int(config.RSI_LOWER_THRESHOLD),
            "max_concurrent": config.MAX_CONCURRENT_POSITIONS,
        },
        "s2": {
            "tf": os.getenv("STRATEGY2_TIMEFRAME", "15m"),
            "long_th": strategy2_meter.LONG_THRESHOLD,
            "short_th": strategy2_meter.SHORT_THRESHOLD,
            "premium_score": config.STRATEGY2_PREMIUM_MIN_SCORE,
            "premium_adx": int(config.STRATEGY2_PREMIUM_MIN_ADX),
            "sl_mult": config.STRATEGY2_PREMIUM_SL_MULT,
            "tp1_r": config.STRATEGY2_PREMIUM_TP1_R,
            "tp2_r": config.STRATEGY2_PREMIUM_TP2_R,
        },
        "s3": {
            "adx_th": config.STRATEGY3_ADX_TH,
            "emergency_sl": round(config.STRATEGY3_EMERGENCY_SL_PCT * 100, 1),
            "be_trigger": round(config.STRATEGY3_BE_TRIGGER_PCT * 100, 2),
            "symbols": s3syms,
        },
    }


def build_strategies_status() -> dict:
    """Consolidated LIVE snapshot of all three strategies for the hub. Every
    branch is failure-safe — one strategy's data source being down must not blank
    the others or 500 the page."""
    import config
    out = {"s1": {}, "s2": {}, "s3": {}}

    # ── S1 — latest scan funnel + best armed/queued setup ──
    try:
        data = load_data()
        funnel = build_funnel(data)
        out["s1"] = {
            "regime": funnel.get("btc_regime", "unknown"),
            "scanned": funnel.get("total", 0),
            "tradeable": funnel.get("tradeable", 0),
            "queued": funnel.get("queued", 0),
            "best": build_best_s1_trade(data),
            "last_update": funnel.get("last_update"),
        }
    except Exception as exc:  # noqa: BLE001
        out["s1"] = {"error": str(exc)}

    # ── S2 — the 15m scanner's most recent sweep ──
    try:
        path = os.path.join(os.path.dirname(__file__), "strategy2_signals.json")
        with open(path, "r", encoding="utf-8") as f:
            sd = json.load(f) or {}
        sigs = sd.get("signals", []) or []

        def _conv(s):
            sc = s.get("score") or 0
            return sc if s.get("direction") == "long" else 100 - sc

        prem = sorted((s for s in sigs if s.get("premium")), key=_conv, reverse=True)
        out["s2"] = {
            "live": bool(sd.get("live")),
            "fresh": (time.time() - float(sd.get("generated_at", 0) or 0)) < 1800,
            "last_scan": sd.get("last_scan_human"),
            "count": len(sigs),
            "premium_count": len(prem),
            "premium": [{
                "base": s.get("base"), "direction": s.get("direction"),
                "score": s.get("score"), "conv": _conv(s), "adx": s.get("adx"),
                "aligned": s.get("aligned"), "entry": s.get("entry"),
                "sl": s.get("sl"), "tp1": s.get("tp1"), "tp2": s.get("tp2"),
            } for s in prem[:5]],
        }
    except Exception as exc:  # noqa: BLE001
        out["s2"] = {"error": str(exc), "count": 0, "premium": []}

    # ── S3 — flag-flip / OCC per-symbol position state ──
    try:
        st = strategy3_scanner.load_state()
        syms = []
        for base in config.STRATEGY3_SYMBOLS:
            sym = f"{base}/{config.QUOTE_ASSET}:{config.QUOTE_ASSET}"
            stt = st.get(sym, {}) or {}
            p = config.strategy3_params(base)
            holding = (stt.get("pos_dir") or "").lower() or None
            armed = bool(stt.get("last_flag")) and not stt.get("consumed") and not holding
            # "armed: —, holding: —, score 97.5" is exactly the display that
            # made the owner ask why nothing had opened. Same explanation the
            # Telegram card gives, so the two can never disagree.
            try:
                import strategy3_status
                why = strategy3_status.explain(base, stt, p)
            except Exception:  # noqa: BLE001 — the pane must still render
                why = {}
            syms.append({
                "base": base, "engine": p.get("engine"), "tf": p.get("timeframe"),
                "leverage": p.get("leverage"),
                "holding": holding.upper() if holding else None,
                "armed": (stt.get("last_flag") or "").upper() if armed else None,
                "score": stt.get("last_score"),
                "why": why.get("headline"), "next": why.get("next_step"),
            })
        out["s3"] = {"symbols": syms}
    except Exception as exc:  # noqa: BLE001
        out["s3"] = {"error": str(exc), "symbols": []}

    return out


@app.route("/strategies")
@login_required
def strategies():
    """One hub for all three strategies — tab buttons switch between S1/S2/S3,
    each showing its RULES (prose in-template, live numbers from config) and its
    CURRENT SITUATION (build_strategies_status). Concludes the separate strategy
    pages into a single overview; each tab links out to its detailed page."""
    return render_template("strategies.html", user=current_user,
                           params=_strategies_params(),
                           status=_json_safe(build_strategies_status()))


@app.route("/api/strategies")
@login_required
def api_strategies():
    """Live status for the hub's auto-refresh (rules are static, only状況 moves)."""
    return jsonify(_json_safe(build_strategies_status()))


@app.route("/api/price_alerts")
@login_required
def api_price_alerts():
    """The dashboard's 🔔 Price Alerts card — user-set levels the S2 scanner
    watches. Active alerts are enriched with a live price so the card can show
    distance-to-target without another endpoint."""
    alerts = sorted(price_alerts.load_alerts(),
                    key=lambda a: a.get("created") or 0, reverse=True)
    active_syms = [a["symbol"] for a in alerts if not a.get("triggered")]
    tickers = fetch_live_tickers(active_syms) if active_syms else {}
    for a in alerts:
        t = tickers.get(a.get("symbol")) or {}
        last = t.get("last") or t.get("close")
        a["last"] = float(last) if last else None
    return jsonify({"alerts": alerts})


@app.route("/api/price_alerts", methods=["POST"])
@login_required
def api_price_alerts_add():
    """Create one alert. Body: {symbol: "ETH" (base or full), price: 3500}.
    Direction (above/below) is fixed from the live price at creation."""
    body = request.get_json(silent=True) or {}
    base = str(body.get("symbol") or "").strip().upper().split("/")[0].split(":")[0]
    if not base or not base.isalnum():
        return jsonify({"ok": False, "error": "bad symbol"}), 400
    try:
        target = float(body.get("price"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad price"}), 400
    symbol = f"{base}/{QUOTE_ASSET}:{QUOTE_ASSET}"
    t = fetch_live_tickers([symbol]).get(symbol) or {}
    last = t.get("last") or t.get("close")
    if not last:
        return jsonify({"ok": False, "error": f"no live price for {base} — "
                        "is it a Binance USDT perp?"}), 400
    try:
        alert = price_alerts.add_alert(symbol, target, float(last))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "alert": alert})


@app.route("/api/price_alerts/delete", methods=["POST"])
@login_required
def api_price_alerts_delete():
    body = request.get_json(silent=True) or {}
    return jsonify({"ok": price_alerts.remove_alert(str(body.get("id") or ""))})


# ── Funding-cost tool (/tools) ───────────────────────────────────────────────
# Binance publishes the CURRENT rate but not "what holding this costs me", so
# the tool needs the interval too. ccxt leaves `interval` None on Binance, so
# it is derived from the gaps in the funding-rate HISTORY (real data, not an
# assumed 8h) and the same history gives a recent average — one funding print
# is noisy, the average is what a multi-day hold actually pays.
_funding_cache: dict = {}
FUNDING_TTL = 120


@app.route("/api/funding")
@login_required
def api_funding():
    base = (request.args.get("symbol") or "").strip().upper().split("/")[0].split(":")[0]
    if not base or not base.isalnum():
        return jsonify({"ok": False, "error": "bad symbol"}), 400
    symbol = f"{base}/{QUOTE_ASSET}:{QUOTE_ASSET}"
    now = time.time()
    hit = _funding_cache.get(symbol)
    if hit and now - hit[0] < FUNDING_TTL:
        return jsonify(hit[1])

    try:
        cur = rest_client.call("fetch_funding_rate", symbol) or {}
    except Exception as e:  # noqa: BLE001 — unknown symbol / cooldown
        return jsonify({"ok": False, "error": f"no funding data for {base} — "
                        "is it a Binance USDT perp?", "detail": str(e)[:120]}), 400

    rate = cur.get("fundingRate")
    if rate is None:
        return jsonify({"ok": False, "error": f"no funding rate for {base}"}), 400

    # Interval + recent average from history; both are best-effort — the tool
    # stays usable on the current rate alone if this call fails.
    interval_h, avg_rate, samples = None, None, 0
    try:
        hist = rest_client.call("fetch_funding_rate_history", symbol, None, 21) or []
        stamps = [h["timestamp"] for h in hist if h.get("timestamp")]
        gaps = sorted((stamps[i + 1] - stamps[i]) / 3600000.0
                      for i in range(len(stamps) - 1))
        if gaps:  # median gap — robust to a single missed/backfilled print
            interval_h = round(gaps[len(gaps) // 2], 2)
        rates = [h["fundingRate"] for h in hist if h.get("fundingRate") is not None]
        if rates:
            avg_rate, samples = sum(rates) / len(rates), len(rates)
    except Exception as e:  # noqa: BLE001
        print(f"Funding history for {symbol} failed: {e}")

    payload = {
        "ok": True,
        "symbol": symbol,
        "base": base,
        "funding_rate": float(rate),
        "avg_rate": avg_rate,
        "avg_samples": samples,
        "interval_hours": interval_h,
        "mark_price": cur.get("markPrice"),
        "next_funding_ts": cur.get("fundingTimestamp"),
    }
    _funding_cache[symbol] = (now, payload)
    return jsonify(payload)


@app.route("/api/events_feed")
@login_required
def api_events_feed():
    """Recent big-event alerts the 🌍 event radar sent to Telegram — mirrored
    on the /market page. Read-only view over the radar's own state file."""
    import event_radar
    recent = (event_radar._load_state().get("recent") or [])[:30]
    return jsonify({"events": recent})


@app.route("/api/balance_history")
@admin_required
def api_balance_history():
    """Daily real-balance snapshots (both venues) — the equity curve the
    /performance overview draws. Written by daily_report.record_balance.
    ADMIN-ONLY: this is the owner's real money, not member content."""
    import daily_report
    try:
        with open(daily_report.BALANCE_FILE, "r", encoding="utf-8") as f:
            hist = json.load(f) or []
    except Exception:  # noqa: BLE001 — no snapshots yet
        hist = []
    return jsonify({"history": hist})


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
    # 🐋 Crowd radar reads a file the scanner writes; it never fetches here, so
    # a slow or down Binance cannot delay this page.
    try:
        import crowd_radar
        crowd = crowd_radar.web_view()
    except Exception as e:  # noqa: BLE001 — a strip must never break the page
        print(f"Crowd radar view error: {e}")
        crowd = {"recent": [], "ran_ts": 0}
    return render_template("market.html", intel=intel, circuit=circuit, crowd=crowd)


@app.route("/coin")
@app.route("/coin/<base>")
@login_required
def coin_page(base=None):
    """🔍 One-coin analysis. Server-renders nothing but the shell — the search
    box drives /api/coin, so a slow exchange cannot block the page.

    ?embed=1 drops the nav and the search bar for the dashboard's pop-out. The
    pop-out shows THIS page in an iframe rather than a second implementation:
    the analysis is ~200 lines of JS plus its own card CSS, and a copy of that
    on the dashboard is the fourth duplication this repo would be maintaining.
    An iframe cannot drift from the page it embeds.
    """
    return render_template("coin.html", user=current_user, initial=(base or ""),
                           embed=request.args.get("embed") == "1")


@app.route("/api/coin_search")
@login_required
def api_coin_search():
    """Ticker autocomplete over the live perp universe, cached 10 min."""
    import coin_analysis
    q = (request.args.get("q") or "").strip()
    try:
        return jsonify({"matches": coin_analysis.search(q, coin_analysis.universe())})
    except Exception as e:  # noqa: BLE001
        print(f"[coin] search failed: {e}")
        return jsonify({"matches": [], "error": str(e)[:120]}), 200


@app.route("/api/top_picks")
@login_required
def api_top_picks():
    """🏆 Top 3 buy / sell across every engine, with reasons.

    Reads the state files the scanners already write — no exchange calls, so
    this cannot slow the dashboard or add to the rate budget.
    """
    import top_picks
    try:
        return jsonify({"ok": True, **top_picks.rank()})
    except Exception as e:  # noqa: BLE001 — a ranking must never 500 the page
        print(f"Top picks error: {e}")
        return jsonify({"ok": False, "buy": [], "sell": [], "error": str(e)[:150]}), 200


@app.route("/api/coin_overview")
@login_required
def api_coin_overview():
    """The default /coin view — OI-flagged coins plus BTC/ETH. Cheap by design
    (one bulk ticker + the radar's stored numbers), so the page opens fast and
    the full eight-factor analysis stays lazy."""
    import coin_analysis
    try:
        return jsonify(_json_safe(coin_analysis.overview()))
    except Exception as e:  # noqa: BLE001
        print(f"[coin] overview failed: {e}")
        return jsonify({"ok": False, "rows": [], "error": str(e)[:150]}), 200


@app.route("/api/coin")
@login_required
def api_coin():
    import coin_analysis
    try:
        out = coin_analysis.analyse(request.args.get("q") or "")
        out.setdefault("disclaimer", coin_analysis.DISCLAIMER)
        return jsonify(_json_safe(out))
    except Exception as e:  # noqa: BLE001
        print(f"[coin] analyse failed: {e}")
        return jsonify({"ok": False, "error": str(e)[:150]}), 200


@app.route("/api/flips")
@login_required
def api_flips():
    """壓力翻支撐 — live flips plus the forward track record. Reads the
    scanner's state file, so polling costs no exchange call."""
    try:
        import flip_outcomes
        return jsonify(_json_safe(flip_outcomes.web_view()))
    except Exception as e:  # noqa: BLE001
        print(f"[flips] view failed: {e}")
        return jsonify({"recent": [], "open": [], "closed": [], "stats": {},
                        "error": str(e)[:150]}), 200


@app.route("/api/crowd_radar")
@login_required
def api_crowd_radar():
    """Latest extreme open-interest builds. Reads the scanner's state file —
    no exchange call — so polling this is free."""
    try:
        import crowd_radar
        return jsonify(crowd_radar.web_view())
    except Exception as e:  # noqa: BLE001
        return jsonify({"recent": [], "ran_ts": 0, "error": str(e)[:150]}), 200


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


@app.route("/api/liquidations")
@login_required
def api_liquidations():
    """Live futures-liquidation aggregates (Binance + Bybit + OKX WebSockets).
    The collector starts lazily on the first call and accumulates from there —
    there is no free historical source, so the window fills up over time
    (collecting_since tells the UI how much is really behind the numbers)."""
    liquidations.start()
    try:
        window = min(int(request.args.get("window", 86400)), 86400)
    except (TypeError, ValueError):
        window = 86400
    return jsonify(_json_safe(liquidations.snapshot(window)))


@app.route("/stocks")
@login_required
def stocks_page():
    """Stock watch — Taiwan top-50 + US top-100 with Binance-perp comparison.
    Read-only public data via stocks_data.py; isolated from the scanner/bot."""
    return render_template("stocks.html", stocks=_stocks_cached(), user=current_user)


_stocks_cache = {"ts": 0.0, "data": None}
STOCKS_TTL = 60


def _stocks_cached():
    """build_stocks() hits TWSE + Yahoo. /stocks, /markets and the public
    movers endpoint all want it, so cache it briefly rather than fanning out
    three sets of upstream calls per visitor."""
    import stocks_data
    now = time.time()
    if _stocks_cache["data"] is not None and now - _stocks_cache["ts"] < STOCKS_TTL:
        return _stocks_cache["data"]
    try:
        data = stocks_data.build_stocks()
    except Exception as e:  # noqa: BLE001
        if _stocks_cache["data"] is not None:
            return _stocks_cache["data"]          # stale beats nothing
        return stocks_data.empty_payload(str(e))
    _stocks_cache.update(ts=now, data=data)
    return data


@app.route("/api/stocks")
@login_required
def api_stocks():
    return jsonify(_stocks_cached())


@app.route("/api/market_movers")
def api_market_movers():
    """PUBLIC — feeds the /markets board's movers + session clocks.

    Deliberately trimmed: only ticker, name, price and % move, plus each
    market's open/closed state. The full /api/stocks payload also carries the
    Binance tokenised-stock perp columns, which nobody needs here; a public
    endpoint should hand out the minimum that answers the question.
    """
    data = _stocks_cached()
    out = {"generated_at": data.get("generated_at"), "tw": {}, "us": {}}
    for side, key, label in (("tw", "code", "name"), ("us", "ticker", "name")):
        block = data.get(side) or {}
        rows = []
        for r in block.get("rows") or []:
            pct = r.get("change_pct")
            if pct is None:
                continue
            rows.append({"id": r.get(key), "name": r.get(label),
                         "price": r.get("price"), "pct": pct})
        rows.sort(key=lambda r: r["pct"], reverse=True)
        out[side] = {
            "market": block.get("market") or {},
            "up": rows[:5],
            "down": rows[-5:][::-1],
            "advancers": sum(1 for r in rows if r["pct"] > 0),
            "decliners": sum(1 for r in rows if r["pct"] < 0),
            "total": len(rows),
            "error": block.get("error"),
        }
    return jsonify(out)


# ── 3D Market Universe (/universe) ──────────────────────────────────────────
# The scan table is 246 rows deep; the same data plotted in space makes the
# clusters and the outliers obvious. Everything here is already on disk from
# the last sweep — this endpoint reshapes it, it never triggers a scan or a
# network call.
_CONVICTION_RANK = (("MAX", 3), ("HIGH", 2), ("WATCH", 1))

# Only axes with FULL coverage are offered by default. entry/SL/TP are None
# for every signal the engine did not arm (215 of 246 on a typical bear
# sweep), so metrics derived from them are marked optional and the viewer
# drops the points that lack one rather than plotting a fake zero.
UNIVERSE_AXES = [
    {"key": "rsi",    "label": "RSI",                  "unit": "",  "optional": False},
    {"key": "score",  "label": "Confluence score",     "unit": "",  "optional": False},
    {"key": "lights", "label": "Lights passed",        "unit": "",  "optional": False},
    {"key": "dhigh",  "label": "Distance to swing high", "unit": "%", "optional": False},
    {"key": "dlow",   "label": "Distance to swing low",  "unit": "%", "optional": False},
    {"key": "rr",     "label": "Risk : reward",        "unit": "",  "optional": True},
    {"key": "dent",   "label": "Distance to entry",    "unit": "%", "optional": True},
]


def _fnum(v):
    """float() that yields None instead of raising — scan rows carry None for
    every level when a setup was not armed."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


@app.route("/universe")
@login_required
def universe_page():
    """3D market map — every scanned symbol as a point in space."""
    return render_template("universe.html", user=current_user)


_liq_px_cache: dict = {}
LIQ_PX_TTL = 300


def _minute_closes(base: str) -> dict:
    """{minute_ts_ms: close} for the last day, from 1m candles.

    Bybit and OKX report the BANKRUPTCY price, which sits past where the tape
    actually printed, so liquidations.py deliberately stores px=0 for them
    (verified 2026-07-14). That is correct — but it leaves ~99% of the feed
    unusable for a PRICE map: four hours of collection produced 1,151
    Bybit/OKX events against 3 Binance ones.

    Those events still carry a trustworthy TIMESTAMP. Pricing them at the
    market close of their own minute uses no bankruptcy figure at all, is
    accurate to one candle — ample for bucketing a price level — and is
    reported separately so the map never claims they were fills.
    """
    now = time.time()
    hit = _liq_px_cache.get(base)
    if hit and now - hit[0] < LIQ_PX_TTL:
        return hit[1]
    out = {}
    try:
        import backtest as _bt
        oh = _bt.fetch_ohlcv(f"{base}/{QUOTE_ASSET}:{QUOTE_ASSET}", "1m", 2)
        out = {int(c[0]): float(c[4]) for c in oh}
    except Exception as e:  # noqa: BLE001 — degrade to Binance-only prints
        print(f"[liq_levels] 1m closes for {base} failed: {e}")
    _liq_px_cache[base] = (now, out)
    return out


# ── 🔥 Projected liquidation heatmap ────────────────────────────────────────
# THIS IS A MODEL, NOT DATA. /api/liq_levels shows liquidations that actually
# happened; this estimates where leverage would GET liquidated if price went
# there. Public heatmaps (Coinglass et al.) do the same thing with a
# proprietary model — this one states its assumptions so the output can be
# argued with:
#
#   1. New positions open in proportion to each bar's OPEN-INTEREST INCREASE.
#      OI falling means positions closed, so no new levels are created.
#      Volume is only a fallback when OI history is unavailable — volume also
#      counts closes and would invent levels that never existed.
#   2. Longs and shorts split 50/50 each bar. The real split is not public.
#      This is the single largest assumption in the model.
#   3. Leverage mix is fixed: 10x/25x/50x/100x weighted .35/.30/.25/.10.
#   4. Liquidation price = entry x (1 -/+ 1/L). Ignores maintenance margin
#      tiers, fees and funding, so real liquidations happen slightly EARLIER
#      than modelled.
#   5. A level is CLEARED once price trades through it — those positions are
#      already gone. Without this the map accumulates ghosts.
#
# Total intensity is scaled to the current OI notional, so the numbers are in
# plausible USD rather than arbitrary units. They remain an estimate.
_LEV_MIX = ((10, 0.35), (25, 0.30), (50, 0.25), (100, 0.10))
_heat_cache: dict = {}
HEAT_TTL = 180


@app.route("/api/liq_heatmap")
@login_required
def api_liq_heatmap():
    base = (request.args.get("coin") or "BTC").strip().upper()
    if not base.isalnum():
        return jsonify({"ok": False, "error": "bad coin"}), 400
    tf = request.args.get("tf", "15m")
    if tf not in ("5m", "15m", "1h"):
        tf = "15m"
    sym = f"{base}/{QUOTE_ASSET}:{QUOTE_ASSET}"
    key = (base, tf)
    now = time.time()
    hit = _heat_cache.get(key)
    if hit and now - hit[0] < HEAT_TTL:
        return jsonify(hit[1])

    try:
        import backtest as _bt
        days = 2 if tf != "1h" else 4
        oh = _bt.fetch_ohlcv(sym, tf, days)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"no candles for {base}: {str(e)[:90]}"}), 200
    if len(oh) < 20:
        return jsonify({"ok": False, "error": f"only {len(oh)} candles for {base}"}), 200

    NBARS = 96
    oh = oh[-NBARS:]

    # open-interest history, aligned to the same bars where possible
    oi_by_ts, oi_now = {}, None
    try:
        hist = rest_client.call("fetch_open_interest_history", sym, tf, None, NBARS) or []
        for r in hist:
            v = r.get("openInterestValue")
            if r.get("timestamp") and v:
                oi_by_ts[int(r["timestamp"])] = float(v)
        cur = rest_client.call("fetch_open_interest", sym) or {}
        oi_now = _fnum(cur.get("openInterestValue"))
        if oi_now is None and oi_by_ts:
            oi_now = list(oi_by_ts.values())[-1]
    except Exception as e:  # noqa: BLE001 — fall back to volume weighting
        print(f"[heatmap] OI for {base} failed: {e}")

    # weight per bar = positive OI change (new positions), else volume
    weights, used_oi = [], bool(oi_by_ts)
    prev_oi = None
    for c in oh:
        w = 0.0
        if used_oi:
            v = oi_by_ts.get(int(c[0]))
            if v is not None:
                w = max(0.0, v - prev_oi) if prev_oi is not None else 0.0
                prev_oi = v
        if not used_oi or (w == 0.0 and prev_oi is None):
            w = float(c[5]) * float(c[4])
        weights.append(w)
    if sum(weights) <= 0:                       # OI never rose in the window
        weights = [float(c[5]) * float(c[4]) for c in oh]
        used_oi = False

    # Vertical window. Deriving it from the candle range ALONE renders an empty
    # map on any quiet day (fixed 2026-08-09): BTC ranged 1% that session, so a
    # range-derived window spanned ±0.5% of spot while the tightest leverage
    # band — 100x — liquidates at ±1.0%. Every one of the four bands fell
    # outside, every cell came back 0, and the canvas drew nothing at all with
    # no explanation. So the window is now the WIDER of the price range and a
    # band that is guaranteed to contain the two highest-leverage tiers
    # (100x = ±1%, 50x = ±2%), which are also the ones close enough to matter.
    HEAT_MIN_HALF_SPAN = 0.025          # ±2.5% of spot, floor
    lo_p = min(c[3] for c in oh)
    hi_p = max(c[2] for c in oh)
    spot_now = float(oh[-1][4])
    pad = (hi_p - lo_p) * 0.35 or hi_p * 0.02   # room for levels beyond the range
    lo_p = min(lo_p - pad, spot_now * (1 - HEAT_MIN_HALF_SPAN))
    hi_p = max(hi_p + pad, spot_now * (1 + HEAT_MIN_HALF_SPAN))
    NB = 64
    step = (hi_p - lo_p) / NB

    def _bin(px):
        return None if not (lo_p <= px < hi_p) else int((px - lo_p) / step)

    live = [[0.0, 0.0] for _ in range(NB)]      # [long-liq usd, short-liq usd]
    grid = []
    for i, c in enumerate(oh):
        entry, high, low = float(c[4]), float(c[2]), float(c[3])
        w = weights[i]
        for lev, share in _LEV_MIX:
            amt = w * share * 0.5               # assumption 2: 50/50 split
            for side, price in ((0, entry * (1 - 1 / lev)), (1, entry * (1 + 1 / lev))):
                j = _bin(price)
                if j is not None:
                    live[j][side] += amt
        # assumption 5: anything this bar traded through is already liquidated
        jl, jh = _bin(low), _bin(high)
        if jl is not None and jh is not None:
            for j in range(jl, jh + 1):
                live[j][0] = live[j][1] = 0.0
        grid.append([round(a + b, 2) for a, b in live])

    tot = sum(a + b for a, b in live) or 1.0
    scale = (oi_now / tot) if oi_now else 1.0
    grid = [[v * scale for v in row] for row in grid]
    live_usd = [[a * scale, b * scale] for a, b in live]

    levels = [{"lo": lo_p + j * step, "hi": lo_p + (j + 1) * step,
               "long": live_usd[j][0], "short": live_usd[j][1]} for j in range(NB)]
    spot = float(oh[-1][4])
    hot = sorted([lv for lv in levels if (lv["long"] + lv["short"]) > 0],
                 key=lambda lv: -(lv["long"] + lv["short"]))[:6]

    payload = {
        "ok": True, "coin": base, "tf": tf,
        "candles": [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])] for c in oh],
        "grid": grid, "nb": NB, "lo": lo_p, "hi": hi_p, "spot": spot,
        "levels": levels,
        "max_usd": max((max(r) if r else 0) for r in grid) or 0.0,
        "oi_usd": oi_now, "weighted_by": "open-interest growth" if used_oi else "volume",
        "clusters": [{"price": (h["lo"] + h["hi"]) / 2, "usd": h["long"] + h["short"],
                      "side": "long" if h["long"] >= h["short"] else "short",
                      "dist_pct": ((h["lo"] + h["hi"]) / 2 - spot) / spot * 100} for h in hot],
    }
    _heat_cache[key] = (now, payload)
    return jsonify(payload)


@app.route("/api/liq_levels")
@login_required
def api_liq_levels():
    """2D liquidation map for ONE coin: liquidated USD stacked by PRICE level.

    The 3D terrain answers "when did it happen"; this answers the question you
    actually trade off — "which prices have been eating stops, and where is
    price now relative to them". Same Binance-only rule: Bybit and OKX report
    the BANKRUPTCY price, so including them would draw bars at prices that
    never traded.
    """
    liquidations.start()
    base = (request.args.get("coin") or "BTC").strip().upper()
    if not base.isalnum():
        return jsonify({"ok": False, "error": "bad coin"}), 400
    try:
        window = max(600, min(int(request.args.get("window", 86400)), 86400))
    except (TypeError, ValueError):
        window = 86400

    cutoff = time.time() * 1000 - window * 1000
    mine = [e for e in liquidations.events_copy()
            if e.get("sym") == base and (e.get("ts") or 0) >= cutoff]
    newest = max((e.get("ts") or 0 for e in mine), default=0)

    closes = _minute_closes(base) if any(not e.get("px") for e in mine) else {}
    priced, skipped, n_fill, n_est = [], 0, 0, 0
    for e in mine:
        if e.get("px"):
            priced.append(e); n_fill += 1
            continue
        m = int(e.get("ts", 0)) // 60000 * 60000
        px = closes.get(m) or closes.get(m - 60000)
        if px:
            priced.append({**e, "px": px}); n_est += 1
        else:
            skipped += 1

    snap = liquidations.snapshot(60)
    out = {"ok": True, "coin": base, "window_sec": window, "levels": [],
           "n_priced": len(priced), "n_unpriced": skipped, "last_ts": newest or None,
           "n_fill": 0, "n_est": 0,
           "collecting_since": snap.get("collecting_since"),
           "long_usd": 0.0, "short_usd": 0.0, "max_usd": 0.0, "price": None,
           "lo": None, "hi": None}

    try:
        t = fetch_live_tickers([f"{base}/{QUOTE_ASSET}:{QUOTE_ASSET}"]) or {}
        first = next(iter(t.values()), {}) or {}
        out["price"] = _fnum(first.get("last") or first.get("close"))
    except Exception as e:  # noqa: BLE001 — the map still reads without it
        print(f"[liq_levels] price for {base} failed: {e}")

    out["n_fill"], out["n_est"] = n_fill, n_est
    if not priced:
        return jsonify(out)

    pxs = sorted(e["px"] for e in priced)
    # Anchor the ladder on SPOT, not on the event min/max. Percentiles do not
    # help at these sample sizes (at n=32 the 2nd percentile is still index 0),
    # and one stale print 5% away stretched the scale until 25 of 28 rows
    # rendered empty. A band around spot is also the actionable zone — levels
    # far from price are not what you trade off. Outliers clamp into the end
    # bins and are counted, never dropped silently.
    spot0 = out["price"] or pxs[len(pxs) // 2]
    band = 0.03
    lo, hi = spot0 * (1 - band), spot0 * (1 + band)
    inside = [x for x in pxs if lo <= x <= hi]
    # widen until the band holds most of the activity, so a genuinely volatile
    # window is not squeezed into the two end bins
    while len(inside) < len(pxs) * 0.8 and band < 0.25:
        band *= 1.6
        lo, hi = spot0 * (1 - band), spot0 * (1 + band)
        inside = [x for x in pxs if lo <= x <= hi]
    out["band_pct"] = round(band * 100, 2)
    out["n_outside"] = len(pxs) - len(inside)

    NB = 28
    bins = [[0.0, 0.0] for _ in range(NB)]
    for e in priced:
        # clamp: an outlier belongs in the end bin, not off the chart
        j = min(NB - 1, max(0, int((e["px"] - lo) / (hi - lo) * NB)))
        usd = float(e.get("usd") or 0)
        if str(e.get("side", "")).lower().startswith("l"):
            bins[j][0] += usd
            out["long_usd"] += usd
        else:
            bins[j][1] += usd
            out["short_usd"] += usd

    step = (hi - lo) / NB
    out["levels"] = [{"lo": lo + j * step, "hi": lo + (j + 1) * step,
                      "long": b[0], "short": b[1]} for j, b in enumerate(bins)]
    out["max_usd"] = max((b[0] + b[1]) for b in bins)
    out["lo"], out["hi"] = lo, hi

    # ── the "magnets": the heaviest cluster ABOVE and BELOW spot ─────────────
    # Price tends to reach for where leverage is dying, so the nearest big
    # cluster on each side is the honest read of "what might get run next".
    # Stated as a LEVEL, never as a prediction — it is where stops already
    # died, not where price is going.
    spot = out["price"]
    if spot:
        above = [lv for lv in out["levels"] if lv["lo"] > spot and (lv["long"] + lv["short"]) > 0]
        below = [lv for lv in out["levels"] if lv["hi"] < spot and (lv["long"] + lv["short"]) > 0]
        def _mag(rows):
            if not rows:
                return None
            b = max(rows, key=lambda lv: lv["long"] + lv["short"])
            mid = (b["lo"] + b["hi"]) / 2
            tot = b["long"] + b["short"]
            return {"price": mid, "usd": tot,
                    # which side died there: longs liquidate on the way DOWN,
                    # shorts on the way UP
                    "side": "long" if b["long"] >= b["short"] else "short",
                    "dist_pct": (mid - spot) / spot * 100}
        out["magnet_up"] = _mag(above)
        out["magnet_down"] = _mag(below)
        # net pressure: which side has been bleeding more in this window
        tot = out["long_usd"] + out["short_usd"]
        out["bias"] = None if not tot else {
            "long_share": out["long_usd"] / tot,
            # more LONGS liquidated = downside pressure has been winning
            "label": "downside" if out["long_usd"] > out["short_usd"] else "upside",
        }
    return jsonify(out)


@app.route("/api/whale_3d")
@login_required
def api_whale_3d():
    """🐳 Tracked whales' current positioning in ONE coin.

    Reads whale_tracker's own state file — the snapshot its alert loop
    already maintains — so this endpoint costs no Hyperliquid calls.

    `szi` is in COIN units, which makes whales incomparable: 29 BTC and
    50,000 ETH look like a rounding error next to each other until both are
    priced. Everything is converted to USD notional with one live ticker
    lookup, and a whale whose size cannot be priced is returned with usd=None
    rather than 0 — zero would render as "no position".
    """
    import whale_tracker
    coin = (request.args.get("coin") or "ETH").strip().upper()
    if not coin.isalnum():
        return jsonify({"ok": False, "error": "bad coin"}), 400

    try:
        labels = {a["address"]: (a.get("label") or "") for a in whale_tracker.load_addresses()}
        state = whale_tracker._load_state() or {}
    except Exception as e:  # noqa: BLE001 — never take the dashboard down
        return jsonify({"ok": False, "error": str(e)[:160], "whales": []}), 200

    px = None
    try:
        t = fetch_live_tickers([f"{coin}/{QUOTE_ASSET}:{QUOTE_ASSET}"]) or {}
        first = next(iter(t.values()), {}) or {}
        px = _fnum(first.get("last") or first.get("close"))
    except Exception as e:  # noqa: BLE001 — priced view is better, not required
        print(f"[whale3d] price for {coin} failed: {e}")

    whales, coins = [], set()
    for addr, pos in state.items():
        if not isinstance(pos, dict):
            continue
        coins.update(k for k, v in pos.items() if isinstance(v, dict))
        d = pos.get(coin)
        if not isinstance(d, dict):
            continue
        szi = _fnum(d.get("szi"))
        if szi is None or szi == 0:
            continue
        entry, lev, liq = _fnum(d.get("entry")), _fnum(d.get("lev")), _fnum(d.get("liq"))
        # P&L is COMPUTED from cost basis against the live price, not read from
        # the stored snapshot: the tracker polls every 5 minutes, so a stored
        # figure would be up to that stale. szi carries the sign, so
        # szi*(px-entry) is already correct for a short.
        # Caveat kept honest in the UI: entry comes from Hyperliquid while px is
        # this exchange's perp last — a few bps of cross-venue basis rides along.
        upnl = roi = None
        if entry and px:
            upnl = szi * (px - entry)
            margin = abs(szi) * entry / lev if lev else abs(szi) * entry
            if margin:
                roi = upnl / margin
        whales.append({
            "label": labels.get(addr) or addr[:8],
            "addr": addr[:10] + "…",
            "side": "long" if szi > 0 else "short",
            "szi": szi,
            "usd": None if px is None else abs(szi) * px,
            "entry": entry,
            "lev": lev,
            "liq": liq,
            "upnl": upnl,
            "roi": roi,
            "url": f"https://hypurrscan.io/address/{addr}",
        })
    whales.sort(key=lambda w: -(w["usd"] or abs(w["szi"])))

    longs = [w for w in whales if w["side"] == "long"]
    shorts = [w for w in whales if w["side"] == "short"]
    lu = sum(w["usd"] or 0 for w in longs)
    su = sum(w["usd"] or 0 for w in shorts)
    return jsonify({
        "ok": True, "coin": coin, "price": px, "whales": whales,
        "n_long": len(longs), "n_short": len(shorts),
        "long_usd": lu, "short_usd": su, "net_usd": lu - su,
        "tracked": len(labels),
        "coins": sorted(coins),
    })


@app.route("/api/universe")
@login_required
def api_universe():
    data = load_data()
    pts = []
    for s in data.get("signals", []):
        if not isinstance(s, dict):
            continue
        rsi, score = _fnum(s.get("rsi")), _fnum(s.get("score"))
        if rsi is None or score is None:
            continue                      # can't place it without the base axes
        smc = s.get("smc") or {}
        entry, sl = _fnum(s.get("entry")), _fnum(s.get("sl"))
        tp, price = _fnum(s.get("tp")), _fnum(s.get("current_price"))
        rr = dent = None
        if None not in (entry, sl, tp) and abs(entry - sl) > 0:
            rr = abs(tp - entry) / abs(entry - sl)
        if None not in (entry, price) and entry:
            dent = (price - entry) / entry * 100.0

        conv = 0
        label = str(s.get("conviction") or "")
        for needle, rank in _CONVICTION_RANK:
            if needle in label:
                conv = rank
                break

        sym = str(s.get("symbol") or "")
        pts.append({
            "b": sym.split("/")[0],                     # base, for the label
            "sym": sym,
            "d": 1 if s.get("direction") == "LONG" else -1,
            "rsi": round(rsi, 1),
            "score": score,
            "lights": _fnum(s.get("effective_lights")) or 0,
            "dhigh": _fnum(smc.get("dist_to_high_pct")),
            "dlow": _fnum(smc.get("dist_to_low_pct")),
            "rr": None if rr is None else round(rr, 2),
            "dent": None if dent is None else round(dent, 2),
            "conv": conv,
            "zone": smc.get("zone_label") or "",
            "px": price,
            "st": s.get("trade_status") or "",
            "tv": s.get("tv_url") or "",
        })

    return jsonify({
        "ok": True,
        "points": pts,
        "axes": UNIVERSE_AXES,
        "last_update": data.get("last_update"),
        "btc_regime": data.get("btc_regime", "neutral"),
        "scanned": data.get("scanned_symbols", 0),
    })


@app.route("/api/liq_terrain")
@login_required
def api_liq_terrain():
    """Liquidation terrain — a time × price grid of liquidated USD.

    ONLY Binance events carry a real traded price: Bybit and OKX report the
    BANKRUPTCY price, which sits beyond where the tape actually printed, so
    plotting them would put mountains at prices that never traded. They are
    excluded from the grid and reported separately, so an empty terrain reads
    as "no Binance prints yet", not as "no liquidations".
    """
    liquidations.start()
    base = (request.args.get("symbol") or "BTC").strip().upper()
    try:
        window = max(600, min(int(request.args.get("window", 21600)), 86400))
    except (TypeError, ValueError):
        window = 21600

    now_ms = time.time() * 1000
    cutoff = now_ms - window * 1000
    priced, skipped = [], 0
    for e in liquidations.events_copy():
        if e.get("sym") != base or (e.get("ts") or 0) < cutoff:
            continue
        if e.get("px"):
            priced.append(e)
        else:
            skipped += 1

    NX, NZ = 40, 26                      # time buckets × price buckets
    out = {
        "ok": True, "symbol": base, "window_sec": window,
        "nx": NX, "nz": NZ, "n_priced": len(priced), "n_unpriced": skipped,
        "collecting_since": liquidations.snapshot(60).get("collecting_since"),
        "grid": [], "price_domain": None, "time_domain": [cutoff, now_ms],
        "max_usd": 0.0, "long_usd": 0.0, "short_usd": 0.0,
    }
    if not priced:
        return jsonify(out)

    prices = sorted(e["px"] for e in priced)
    plo, phi = prices[0], prices[-1]
    if phi - plo < phi * 1e-6:           # single price level — give it air
        pad = max(phi * 0.001, 1e-9)
        plo, phi = plo - pad, phi + pad

    # grid[j][i] = {l: long usd, s: short usd}; j = price bucket, i = time
    grid = [[[0.0, 0.0] for _ in range(NX)] for _ in range(NZ)]
    for e in priced:
        i = int((e["ts"] - cutoff) / (window * 1000) * NX)
        j = int((e["px"] - plo) / (phi - plo) * NZ)
        i = max(0, min(NX - 1, i))
        j = max(0, min(NZ - 1, j))
        usd = float(e.get("usd") or 0)
        # "long" = long positions liquidated (forced sells into the bid)
        if str(e.get("side", "")).lower().startswith("l"):
            grid[j][i][0] += usd
            out["long_usd"] += usd
        else:
            grid[j][i][1] += usd
            out["short_usd"] += usd

    out["grid"] = grid
    out["price_domain"] = [plo, phi]
    out["max_usd"] = max((c[0] + c[1]) for row in grid for c in row)
    return jsonify(out)


@app.route("/api/risk_bodies")
@admin_required
def api_risk_bodies():
    """Live positions with their stop / liquidation geometry.

    Admin-only: this is the owner's real money (see the admin gating tests).
    Distances are signed toward loss, so 0 means "touching it now".
    """
    bodies = []
    try:
        raw = (strategy3_exec.account_snapshot() or {}).get("positions") or []
    except Exception as e:  # noqa: BLE001 — never 500 a viewer
        return jsonify({"ok": False, "error": str(e)[:160], "bodies": []}), 200

    for p in raw:
        mark = _fnum(p.get("mark")) or _fnum(p.get("entry"))
        entry, liq, sl = _fnum(p.get("entry")), _fnum(p.get("liq")), _fnum(p.get("sl"))
        if not mark or not entry:
            continue
        short = str(p.get("side", "")).upper() == "SHORT"
        bodies.append({
            "sym": (p.get("symbol") or p.get("sym") or "?").split("/")[0],
            "side": "SHORT" if short else "LONG",
            "engine": p.get("engine") or "manual",
            "notional": _fnum(p.get("notional")) or 0.0,
            "lev": _fnum(p.get("leverage")),
            "entry": entry, "mark": mark, "sl": sl, "liq": liq,
            "to_sl": None if sl is None else abs((sl - mark) / mark * 100.0),
            "to_liq": None if liq is None else abs((liq - mark) / mark * 100.0),
            "pnl_pct": None if not entry else
                       ((entry - mark) / entry * 100.0 if short else (mark - entry) / entry * 100.0),
        })
    return jsonify({"ok": True, "bodies": bodies})


@app.route("/reality")
@login_required
def reality_page():
    """What the measured record says — the page that is allowed to say "no".

    Replaced the seven-calculator Toolkit on 2026-08-10. Those calculators were
    generic (expectancy, DCA, drawdown, risk-of-ruin exist on any site) and none
    of them knew a single thing about this account. Meanwhile signal_outcomes
    had quietly scored ~19,000 real outcomes against six exit rules and the only
    way to read them was a Telegram text table.

    Price Alerts moved here intact — it was the one widget on the old page doing
    something an exchange does not.
    """
    return render_template("reality.html", user=current_user)


@app.route("/tools")
@login_required
def tools_page():
    """Permanent redirect — /tools is bookmarked, is in old Telegram messages,
    and was the nav target for a week. Kept as a redirect rather than deleted so
    none of those turn into a 404."""
    return redirect(url_for("reality_page"), code=301)


@app.route("/api/reality")
@login_required
def api_reality():
    """The scoreboard behind /reality. Reads the tally off disk — no exchange
    calls, no evaluation — so it is cheap enough to poll."""
    import reality
    cohort = (request.args.get("cohort") or "all").strip()
    rule = (request.args.get("rule") or "hold").strip()
    if cohort not in reality.COHORT_LABEL:
        cohort = "all"
    if rule not in reality.RULE_LABEL:
        rule = "hold"
    try:
        return jsonify(_json_safe(reality.board(cohort=cohort, rule=rule)))
    except Exception as exc:  # noqa: BLE001 — the page degrades, never 500s
        print(f"[reality] board failed: {exc}")
        return jsonify({"ok": False, "error": str(exc)[:150]}), 200


@app.route("/tw")
def tw_page():
    """台股．好進場點 — dad's plain-Chinese view of the daily TW pullback scan.
    PUBLIC (no login) so it opens straight from the LINE link; shows only the
    same setups already broadcast to the family LINE group, nothing account-
    related. Read-only and fully fail-soft — never 500s."""
    import tw_stocks
    try:
        data = tw_stocks.web_view()
    except Exception as e:  # noqa: BLE001 — never let dad's page break
        print(f"TW page error: {e}")
        data = {"setups": [], "regime": {}, "regime_ok": False,
                "as_of": None, "error": str(e)}
    # 🎯 立即可進場 Top 5. Ranks the setups already in `data` — no second scan
    # and no extra quote fetch.
    try:
        import stock_picks
        picks = stock_picks.picks()["tw"]
    except Exception as e:  # noqa: BLE001 — the ranking never breaks dad's page
        print(f"TW picks error: {e}")
        picks = None
    return render_template("tw.html", tw=data, picks=picks,
                           invite_url=_invite_url())


@app.route("/markets")
def markets_page():
    """股市總覽 — 台股 + 美股 on ONE page.

    /tw and /us stay as they are (dad opens them straight from LINE links);
    this is the combined view, because the two markets are not independent —
    the US close is what sets the tone for the next TW open, and reading them
    on separate pages hides that. PUBLIC like its two halves: market data
    only, nothing account-related. Fail-soft on each side independently, so a
    broken TW scan still leaves the US half readable.
    """
    import tw_stocks
    import us_market
    try:
        tw = tw_stocks.web_view()
    except Exception as e:  # noqa: BLE001
        print(f"Markets page (TW) error: {e}")
        tw = {"setups": [], "regime": {}, "regime_ok": False, "as_of": None}
    try:
        us = us_market.web_view()
    except Exception as e:  # noqa: BLE001
        print(f"Markets page (US) error: {e}")
        us = {"indices": [], "vix": {}, "tnx": {}, "adr": {}, "breadth": {},
              "read": "", "session_label": None, "live": False}
    return render_template("markets.html", tw=tw, us=us,
                           invite_url=_invite_url())


@app.route("/s4")
@login_required
def s4_page():
    """S4 — Bybit TradFi perp scanner. Reads the last scan's state file; it
    never scans on a page load, or every visitor would fire 60 exchange calls.

    login_required, unlike /tw and /us: those carry public market data, this
    carries entry/stop/target levels the owner is acting on. Not admin_required
    — there is no account information on it, and members of the signal group
    are the intended audience."""
    import strategy4
    try:
        view = strategy4.web_view()
    except Exception as exc:  # noqa: BLE001 — a broken scan must not 500 the page
        view = {"signals": [], "error": str(exc)[:200],
                "disclaimer": strategy4.DISCLAIMER}
    return render_template("s4.html", s4=view, user=current_user)


@app.route("/api/s4")
@login_required
def api_s4():
    """The last S4 scan as JSON — same payload the /s4 page renders, so the
    dashboard can carry the live setups instead of making the owner remember
    to open a second page. Login-gated for the same reason /s4 is: these are
    entry/stop/target levels, not public market data."""
    import strategy4
    try:
        return jsonify(strategy4.web_view())
    except Exception as exc:  # noqa: BLE001 — a broken scan must not 500 a card
        return jsonify({"signals": [], "rejected": {}, "error": str(exc)[:200],
                        "disclaimer": strategy4.DISCLAIMER}), 200


@app.route("/us")
def us_page():
    """美股．開盤前看盤 — the companion to /tw at the other end of the day.
    PUBLIC for the same reason /tw is: it opens straight from a LINE link and
    carries only public market data, nothing account-related. Read-only and
    fail-soft — web_view() serves its last good snapshot rather than raising."""
    import us_market
    import us_stocks
    try:
        setups = us_stocks.web_view()
    except Exception as e:  # noqa: BLE001 — the close digest must still render
        print(f"[us] setup view failed: {e}")
        setups = None
    try:
        import stock_picks
        us_picks = stock_picks.picks()["us"]
    except Exception as e:  # noqa: BLE001
        print(f"US picks error: {e}")
        us_picks = None
    return render_template("us.html", us=us_market.web_view(), setups=setups,
                           picks=us_picks,
                           invite_url=_invite_url())


@app.route("/api/us")
def api_us():
    import us_market
    return jsonify(us_market.web_view())


@app.route("/api/tw")
def api_tw():
    import tw_stocks
    try:
        return jsonify(tw_stocks.web_view())
    except Exception as e:  # noqa: BLE001
        return jsonify({"setups": [], "regime": {}, "error": str(e)}), 200


# ── Copy trading — followers mirror the live Strategy-3 (Bybit) engine ────────
def _copy_context() -> dict:
    """Assemble the /copy page state: the member's own enrollment + (for admins)
    every follower with runtime status. NEVER includes any secret/ciphertext."""
    import config as _config
    import copy_engine
    import copy_store
    import copy_vault
    me = copy_store.get_public(current_user.id)
    ctx = {
        "vault_ok": copy_vault.available(),
        "master_live": bool(getattr(_config, "COPY_TRADING_LIVE", False)),
        "me": me,
        "my_status": copy_engine.status_for(current_user.id) if me else {},
        "min_margin": copy_store.MIN_MARGIN,
        "max_margin": copy_store.MAX_MARGIN,
        "s3_symbols": _config.STRATEGY3_SYMBOLS,
        "is_admin": bool(current_user.is_admin),
        "followers": None,
    }
    if current_user.is_admin:
        rows = copy_store.list_public()
        for r in rows:
            r["status"] = copy_engine.status_for(r["user_id"])
        ctx["followers"] = rows
    return ctx


@app.route("/copy")
@login_required
def copy_page():
    """Copy-trading console: a member enrolls their own Bybit keys to mirror the
    live S3 engine; admins approve/pause/remove followers. Login-gated; the
    member view only ever touches the current user's own record."""
    try:
        ctx = _copy_context()
    except Exception as e:  # noqa: BLE001 — never 500 this page
        print(f"Copy page error: {e}")
        ctx = {"vault_ok": False, "master_live": False, "me": None,
               "my_status": {}, "min_margin": 5, "max_margin": 5000,
               "s3_symbols": [], "is_admin": bool(current_user.is_admin),
               "followers": None, "error": str(e)}
    return render_template("copy.html", user=current_user, ctx=ctx)


@app.route("/api/copy/enroll", methods=["POST"])
@login_required
def api_copy_enroll():
    """Member submits/updates their OWN Bybit keys. Validated read-only before
    storing; stored encrypted; ALWAYS lands disabled (admin must approve)."""
    import copy_engine
    import copy_store
    import copy_vault
    if not copy_vault.available():
        return jsonify({"ok": False, "error": "伺服器加密尚未設定，暫時無法接收金鑰"}), 503
    api_key = (request.form.get("api_key") or "").strip()
    api_secret = (request.form.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        return jsonify({"ok": False, "error": "請填入 API Key 與 Secret"}), 400
    v = copy_engine.validate(api_key, api_secret)
    if not v.get("ok"):
        return jsonify({"ok": False, "error": v.get("error") or "金鑰驗證失敗"}), 400
    rec = copy_store.upsert_keys(current_user.id, current_user.username,
                                 api_key, api_secret, request.form.get("margin"))
    return jsonify({"ok": True, "record": rec, "equity": v.get("equity")})


@app.route("/api/copy/margin", methods=["POST"])
@login_required
def api_copy_margin():
    import copy_store
    rec = copy_store.set_margin(current_user.id, request.form.get("margin"))
    if not rec:
        return jsonify({"ok": False, "error": "尚未提交金鑰"}), 404
    return jsonify({"ok": True, "record": rec})


@app.route("/api/copy/revoke", methods=["POST"])
@login_required
def api_copy_revoke():
    """A member removes their own keys — the only self-service deletion path."""
    import copy_store
    copy_store.remove(current_user.id)
    return jsonify({"ok": True})


def _copy_admin_uid():
    try:
        return int(request.form.get("user_id")), None
    except (TypeError, ValueError):
        return None, (jsonify({"ok": False, "error": "bad user_id"}), 400)


@app.route("/api/copy/admin/toggle", methods=["POST"])
@admin_required
def api_copy_admin_toggle():
    """Admin approve (enable) or pause (disable) a follower."""
    import copy_store
    uid, err = _copy_admin_uid()
    if err:
        return err
    rec = copy_store.set_enabled(uid, request.form.get("enabled") == "true")
    if not rec:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "record": rec})


@app.route("/api/copy/admin/remove", methods=["POST"])
@admin_required
def api_copy_admin_remove():
    import copy_store
    uid, err = _copy_admin_uid()
    if err:
        return err
    copy_store.remove(uid)
    return jsonify({"ok": True})


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

@app.route("/line/webhook", methods=["POST"])
def line_webhook():
    # Called by LINE's servers, not browsers — auth is the channel-secret
    # signature (no session/CSRF). Inviting the Official Account into a LINE
    # group lands a join event here and the group auto-subscribes to the
    # 台股 messages; leaving unsubscribes. See line_push.handle_webhook.
    import line_push
    if not line_push.sig_ok(request.get_data(),
                            request.headers.get("X-Line-Signature", "")):
        return "bad signature", 403
    line_push.handle_webhook(request.get_json(silent=True) or {})
    return "OK"


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
    # Timestamp every line. These logs had none, which made
    # "is this error current?" unanswerable — see log_stamp.
    import log_stamp
    log_stamp.install()

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

        # One account, one active login. Left NULL on existing rows on purpose:
        # load_user() treats "no token yet" as still-valid, so nobody is kicked
        # out merely by deploying this. The first login after the upgrade mints
        # a token and enforcement begins for that account.
        for col, ddl in (("session_token", "VARCHAR(64)"),
                         ("last_login_at", "DATETIME"),
                         ("last_login_ip", "VARCHAR(64)")):
            if col not in user_columns:
                with db.engine.connect() as conn:
                    conn.execute(text(f"ALTER TABLE user ADD COLUMN {col} {ddl}"))
                    conn.commit()
                    print(f"Added {col} to user table")

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
    if app_debug or app_reload:
        # dev conveniences (interactive debugger / .py auto-reload) need werkzeug
        app.run(host=app_host, port=app_port, debug=app_debug, use_reloader=app_reload)
    else:
        # Production path: the cloudflare tunnel exposes this app publicly and
        # the single-threaded dev server warns for a reason. Template editing
        # still hot-reloads (TEMPLATES_AUTO_RELOAD is set at app creation
        # above); only .py changes need a restart. Waitress writes no
        # per-request access logs.
        try:
            from waitress import serve
        except ImportError:
            print("waitress not installed — falling back to the Flask dev server")
            app.run(host=app_host, port=app_port)
        else:
            print(f"Serving with waitress on {app_host}:{app_port} (12 threads)")
            serve(app, host=app_host, port=app_port, threads=12)
