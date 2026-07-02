import os

# Load environment variables from .env file if it exists
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                # Let real shell exports override the local .env file.
                os.environ.setdefault(key, val)

# Path to the .env this config was loaded from (may not exist yet). The admin UI
# uses the helpers below to persist a setting (e.g. LIVE_STRATEGY); the bot reads
# .env only at startup, so a bot restart is required for a change to take effect.
ENV_PATH = env_path


def read_env_var(name, default=None):
    """Read a single key from the .env file FRESH (not the import-time cache)."""
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return default


def set_env_var(name, value):
    """Replace-or-append KEY=value in the .env file (atomic write). Also updates the
    in-process environment so the running web app reflects it immediately. The bot
    is a separate process that reads .env at startup → it needs a restart to apply."""
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    out, found = [], False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == name:
            out.append(f"{name}={value}\n")
            found = True
        else:
            out.append(line)
    if not found:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append(f"{name}={value}\n")
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, ENV_PATH)
    os.environ[name] = str(value)


BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Quiet mode: mute ALL Telegram messages EXCEPT ones explicitly forced (the RSI
# extreme alert + the naked-position safety alarm). Default ON — the user only
# wants the RSI ≥90 / ≤10 alert; signal digests, order fills, startup notices,
# etc. are all silenced. Set TELEGRAM_QUIET=false in .env to get everything back.
# (Parsed inline — _env_bool is defined further down this file.)
TELEGRAM_QUIET = os.getenv("TELEGRAM_QUIET", "true").strip().lower() in ("1", "true", "yes", "on")

# ── Live trading (Binance Futures USD-M) ──────────────────────────────────
# SAFETY: LIVE_TRADING defaults to False. While False the bot is in DRY-RUN —
# it logs the exact order it WOULD place (and sends a Telegram note) but sends
# NOTHING to Binance. Flip to True (env LIVE_TRADING=true) only after you have
# added API keys and tested. When you first go live, keep USE_TESTNET=true.
def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")

LIVE_TRADING = _env_bool("LIVE_TRADING", False)   # master switch — False = dry-run
USE_TESTNET = _env_bool("USE_TESTNET", True)       # route to Binance testnet when live
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Which strategy the LIVE bot trades. Keys are the backtest.STRATEGIES registry:
#   default        = Strategy 1 (Wolf Confluence) — the original live strategy
#   trend_breakout = Strategy 2 (volatility-squeeze breakout, 1R/2R bracket — legacy key name)
#   trend_trailing = Strategy 3 (breakout + Chandelier trailing stop)
# (Strategies 4 and 5 were removed 2026-06-23 — they lost the cross-strategy bake-off.)
# DEFAULT IS "default" (S1): with this value the live bot behaves EXACTLY as it did
# before strategy selection existed. The S2/S3 paths are only reached after a
# deliberate admin change + bot restart. bot.py validates this against the registry
# at startup and falls back to "default" (with a loud log) if it is unknown. S3
# manages the position with a trailing stop the bot ratchets each closed 1h candle.
LIVE_STRATEGY = os.getenv("LIVE_STRATEGY", "default")

# ── Sizing tuned for a ~25 USDT account (Plan A: wide, single-target TP) ──────
# With 10 concurrent positions each can use ~2.5 USDT margin (25/10), and Binance
# enforces a ~5 USDT minimum order size. At 4x leverage a 1.5 USDT margin trade is
# 6 USDT notional (clears the floor); a 6-light 1.5x trade is 9 USDT. Worst case
# fully deployed = 10 × 2.25 ≈ 22.5 USDT, leaving a small free-margin buffer.
# Because the partial take-profit's 50% slice would fall under the 5 USDT floor at
# this size, LIVE_PARTIAL_TP is OFF — each position closes 100% at one target.
LEVERAGE = int(os.getenv("LEVERAGE", "4"))         # 4x — clears min order size on a tiny account
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated") # isolated | cross
# Fixed USDT margin committed per trade (before conviction scaling below).
# Notional position value = FIXED_MARGIN_USDT × LEVERAGE.
FIXED_MARGIN_USDT = float(os.getenv("FIXED_MARGIN_USDT", "1.5"))
# Hard ceiling so a bug can never size an absurd position. Set just above the
# real 1.5x (6-light) case of 2.25 USDT so 10 concurrent ≤ 23 USDT, always
# leaving a free-margin buffer for fees on a 25 USDT account.
MAX_MARGIN_USDT = float(os.getenv("MAX_MARGIN_USDT", "2.3"))
# Hard ceiling on how many positions can be committed at once (resting brackets
# already on the exchange + filled positions still being managed). On a strong
# 1h close many alts qualify together and — because the BTC-regime filter aligns
# them to the SAME side — they are highly correlated, so one BTC reversal hits
# them all at once. This caps total simultaneous exposure regardless of how many
# setups qualify. 0 or negative = unlimited (NOT recommended for live).
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "10"))
# Place the SL + TP bracket orders on Binance so they fire even if the bot is
# offline. The dashboard simulation loop still tracks status either way.
PLACE_BRACKET_ORDERS = _env_bool("PLACE_BRACKET_ORDERS", True)

# Resting-limit model: the instant a setup is QUEUED, place a resting LIMIT
# entry + SL/TP bracket on Binance. The exchange then fills the entry the moment
# price touches it (no polling delay, no missed entries) and the SL/TP protect
# the position even if the bot is offline. If False, the bot instead market-fills
# when it detects the entry touch (legacy behaviour, has polling lag).
USE_RESTING_ORDERS = _env_bool("USE_RESTING_ORDERS", True)
# Post-Only (maker-only) entry. The resting LIMIT entry is always placed on the
# passive side of the book (LONG below price, SHORT above), so it already rests
# as a maker order — this just GUARANTEES the lower maker fee and refuses to ever
# fill as a taker at a worse price. Binance calls this GTX ("Good-Till-Crossing").
# Worth it on a small account where fees matter proportionally more.
USE_POST_ONLY_ENTRY = _env_bool("USE_POST_ONLY_ENTRY", True)
# Partial take-profit on the live account, matching the dashboard simulation:
# close 50% of the position at TP1 and the remaining 50% at TP2. The stop-loss
# protects the whole position the entire time. If False, the live bracket closes
# 100% at a single target (LIVE_TP_TARGET) instead.
# OFF for the ~25 USDT account: the 50% slice would be below Binance's ~5 USDT
# minimum order size, so the bot closes 100% at the single target (2R) and moves
# the live stop to breakeven once price reaches 1R (the dashboard still simulates
# the partial, so live P&L will differ slightly from the dashboard by design).
LIVE_PARTIAL_TP = _env_bool("LIVE_PARTIAL_TP", False)
# Used only when LIVE_PARTIAL_TP is False: "tp2" = 2R runner, "tp1" = 1R de-risk.
LIVE_TP_TARGET = os.getenv("LIVE_TP_TARGET", "tp2").lower()
# After TP1's 50% closes, move the live stop to breakeven (entry) on the
# remaining 50% — exactly what the dashboard simulation does, so the live
# testnet result matches the dry-run. Set False to keep the original stop.
LIVE_TRAIL_TO_BREAKEVEN = _env_bool("LIVE_TRAIL_TO_BREAKEVEN", True)
# Telegram: send a message for each live order fill/failure? Default OFF — the
# routine "order filled" / "entry failed" notifications are noise (e.g. -4411
# TradFi-Perps rejects on stock perps the account can't trade). Safety alarms
# (a position left with NO stop-loss → manual action) ALWAYS send, regardless.
LIVE_ORDER_NOTIFY = _env_bool("LIVE_ORDER_NOTIFY", False)
# Binance deprecated ccxt's set_sandbox_mode for futures, so we point the USD-M
# endpoints at the demo/testnet host directly. Options seen in the wild:
#   demo-fapi.binance.com        → new "Demo Trading" (keys from demo.binance.com)
#   testnet.binancefuture.com    → classic Futures Testnet (keys from testnet.binancefuture.com)
BINANCE_TESTNET_HOST = os.getenv("BINANCE_TESTNET_HOST", "demo-fapi.binance.com")

# Scanner settings
# Full-history backtests (post pagination-fix) showed the edge lives on 1h, not 15m:
# 1h over 120/90/60d held at ~63-66% win, +0.28 to +0.31R expectancy, both LONG and
# SHORT positive, equity-curve R^2 ~0.87-0.92 (steady rise). 15m was ~breakeven.
TIMEFRAMES = ["1h"]
RSI_PERIOD = 7
RSI_UPPER_THRESHOLD = 90.0
RSI_LOWER_THRESHOLD = 10.0
RSI_TARGET_HIGH = 98.0
RSI_TARGET_LOW = 2.0

# ── Hourly RSI-extreme Telegram digest (alert-only; NEVER affects trading) ────
# Each scan the bot collects coins whose RSI on RSI_ALERT_TIMEFRAME is at a
# blow-off extreme (≥ HIGH overbought / ≤ LOW oversold) and sends ONE Telegram
# digest. Dedicated knobs so tuning the alert can't change the strategy's own
# RSI thresholds. Silent when nothing is extreme unless RSI_ALERT_ALWAYS is on.
RSI_ALERT_ENABLED = _env_bool("RSI_ALERT_ENABLED", True)
RSI_ALERT_HIGH = float(os.getenv("RSI_ALERT_HIGH", "90"))
RSI_ALERT_LOW = float(os.getenv("RSI_ALERT_LOW", "10"))
RSI_ALERT_TIMEFRAME = os.getenv("RSI_ALERT_TIMEFRAME", "1h")
RSI_ALERT_ALWAYS = _env_bool("RSI_ALERT_ALWAYS", False)

CHECK_INTERVAL_MINUTES = 60   # match the 1h entry timeframe (scan once per closed candle)
TIMEZONE = "Asia/Taiwan"  # GMT+8

# Market data safety settings
REST_API_MIN_INTERVAL_SEC = 0.25
REST_API_MAX_RETRIES = 4
WS_STALE_AFTER_SECONDS = 120
WS_READY_TIMEOUT_SECONDS = 8
OHLCV_CACHE_LIMIT = 400

# ── Trade plan settings ──
ATR_SL_MULTIPLIER = 1.5  # ATR-based SL beyond structure
# TP is set as a multiple of the ACTUAL risk (R = |entry - SL|) so the
# risk-reward ratio is real on every coin regardless of its volatility.
ATR_TP1_MULTIPLIER = 1.0  # TP1 (partial 50% close) at 1R — de-risks fast
ATR_TP_MULTIPLIER = 2.0   # TP2 (runner) at 2R — used as a floor target / when trailing is off

# ── Runner management ───────────────────────────────────────────────────────
# The remaining 50% after TP1 is managed EXACTLY like the live Binance bracket:
# a hard TP2 at 2R (reduce-only order) + a breakeven stop (moved to entry after
# TP1). This guarantees the dashboard simulation == the live exchange result.
# A trailing-runner experiment was tested in the backtester and DROPPED — on 15m
# almost nothing reaches 2R, so it captured no extra edge and only created a
# sim≠live divergence. (Constants below are legacy/unused, kept for compatibility.)
RUNNER_TRAIL_ENABLED = False
RUNNER_TRAIL_R_MULT = 0.5   # legacy/unused
# After TP1, where does the runner's stop sit BEFORE price reaches the 2R trail?
# 0.0 = breakeven (old). Backtest showed 55% of winners reverse to breakeven for
# only +0.5R total; locking a small profit here turns those into bigger wins.
# entry + RUNNER_LOCK_R*R for longs (mirror for shorts).
# TESTED head-to-head: +0.5R lock was slightly WORSE than breakeven (it cut some
# winners short before they reached 2R). Kept at 0.0 (breakeven) — the ~1:1
# reward:risk is inherent to 15m crypto and isn't fixed by a stop tweak.
RUNNER_LOCK_R = 0.0
MAX_SL_PCT = 0.04  # Cap SL at 4% max
ATR_OFFSET_MULTIPLIER_MIN = 0.3  # 0.3x ATR offset min
ATR_OFFSET_MULTIPLIER_MAX = 0.5  # 0.5x ATR offset max
ENTRY_OFFSET_PCT = 0.015  # Fallback fixed offset if ATR not available
FIXED_SL_PCT = 0.03  # Fallback fixed SL if ATR not available
FIXED_TP_PCT = 0.03  # Fallback fixed TP if ATR not available

# ── Signal conviction thresholds ──
# Backtest (90-trade pooled sample) was decisive: 4-light setups had a NEGATIVE
# edge (34.6% win, -0.272R) and 4-light SHORTs were catastrophic (-0.463R), while
# 5+ lights were clearly profitable (+0.237R). Raising the floor to 5 ~2.6x'd the
# strategy's expectancy (+0.090R -> +0.237R) and removed the inconsistency.
MIN_LIGHTS_FOR_ENTRY = 6   # Only qualify 6+ light setups for trade plans
MIN_LIGHTS_FOR_RECORD = 6  # Only save 6+ light setups to performance tracking
MIN_LIGHTS_FOR_ALERT = 6   # Minimum lights to send Telegram alert
MIN_BASE_LIGHTS = 2        # Require ≥2 real strategy lights — SMC bonus alone can't qualify a trade
MAX_SMC_BONUS_LIGHTS = 3   # Cap SMC bonus so effective lights stay honest (max 5 base + 3 = 8)
STRATEGY_SCORE_LIGHT_THRESHOLD = 8  # L2 light fires when bull/bear score reaches this (max score is 10)
BREAKOUT_VOLUME_MULTIPLIER = 1.5    # A breakout only scores if volume ≥ this × 20-bar average

# ── Data quality ──
MIN_CANDLES_FOR_SIGNAL = 60  # Skip a symbol if it has fewer closed candles than this

# ── Queue hygiene ──
# A queued (pending-entry) setup is only useful while price is between the entry
# and the target. If price reaches TP1 before the entry fills, the move already
# happened — chasing it is a losing trade, so the setup is cancelled. Likewise a
# fresh signal is not queued at all if price is already at/past the target zone.
CANCEL_QUEUE_IF_TARGET_HIT = True

# ── Symmetric directional filters (LONG and SHORT treated EQUALLY) ──────────
# Previously SHORTs were heavily handicapped (needed 5 lights, RSI≥65, and had
# to be at a resistance zone) so almost only LONGs ever fired — fatal when the
# market turned down. These rules are now mirror images so the bot trades both
# sides with equal opportunity.
MIN_LIGHTS_SHORT = 5              # short conviction bar (reverted 6→5 2026-06-24 to resume trading in the bear)
# BOTH directions ON — and this is the real edge. Direction performance is fully
# REGIME-DEPENDENT: in bullish windows LONGs win and SHORTs lose; in bearish
# windows (e.g. the recent 90-day test) SHORTs made +0.85R while LONGs lost -0.27R.
# The BTC-regime filter switches the bot to the side that's working, so keeping
# BOTH directions is profitable in bull AND bear (recent test: +0.315R, +11.3%),
# whereas longs-only LOST -0.27R in the bear regime. Set False only to A/B test.
ENABLE_SHORTS = True
# Avoid entering at an exhausted extreme — symmetric: don't buy the blow-off
# top, don't short the capitulation bottom.
RSI_BLOCK_LONG_ABOVE = 80.0      # block LONG when RSI ≥ this (overbought top)
RSI_BLOCK_SHORT_BELOW = 20.0     # block SHORT when RSI ≤ this (oversold bottom)
# SMC zones are enforced symmetrically by smc_blocked (LONG never at a premium/
# resistance zone, SHORT never at a discount/support zone). The IDEAL location
# (LONG at discount, SHORT at premium) is rewarded with a bonus light rather than
# required, so trade count stays healthy while edge-quality is encouraged.
SMC_IDEAL_ZONE_BONUS = 1
# Premium/discount zone split as a fraction of the swing range (ICT-style).
# Previously the zones were only the top/bottom 5%, so ~90% of price action was
# labelled "equilibrium" and the discount/premium edge never applied. Now the
# lower 40% of the range = discount (ideal LONG), upper 40% = premium (ideal
# SHORT), middle 20% = equilibrium (no edge).
SMC_DISCOUNT_MAX_PCT = 0.40
SMC_PREMIUM_MIN_PCT = 0.60
# Bear-trend short override: normally a SHORT is blocked in a discount/support zone
# (don't short into support). But in a confirmed BTC BEAR regime a downtrend keeps
# BREAKING support, so that rule blocks exactly the trend-shorts we want. When True,
# the discount-zone block is lifted for SHORTs *only while BTC is bear* — chop and
# bull regimes stay fully protected. Set False to restore the strict symmetric gate.
ALLOW_BEAR_TREND_SHORTS_IN_DISCOUNT = True

# ── BTC regime filter — "don't fight Bitcoin" ───────────────────────────────
# Altcoins amplify BTC's direction (BTC -5% → alts -10/15%). Longing an alt while
# BTC is dumping, or shorting while BTC is pumping, is a low-probability fight.
# This blocks counter-BTC trades and is the single biggest win-rate lever for an
# alt futures bot. BTC regime is computed once per scan on a higher timeframe.
ENABLE_BTC_REGIME_FILTER = True
BTC_REGIME_SYMBOL = "BTC/USDT:USDT"
BTC_REGIME_TIMEFRAME = "1h"      # higher TF = stable regime, not noise
BTC_REGIME_EMA = 50
BTC_REGIME_SLOPE_LOOKBACK = 5    # bars used to measure whether the EMA is rising/falling

# ── VWAP confirmation ───────────────────────────────────────────────────────
# Built and TESTED head-to-head — it did NOT help (it cut an already-sparse trade
# count without improving expectancy). Left wired but OFF; flip True to re-test.
# Price ABOVE the rolling VWAP = buyers in control (long) / BELOW = sellers (short).
ENABLE_VWAP_FILTER = False
VWAP_PERIOD = 24                 # rolling VWAP window in bars (~24h on 1h)

# Deprecated/legacy names kept for backward-compatibility (no longer used in the
# qualification logic):
MIN_LIGHTS_FOR_RECORD_SHORT = MIN_LIGHTS_SHORT
BLOCK_SHORT_IF_RSI_BELOW = 0.0
BLOCK_LONG_IF_RSI_MIN = RSI_BLOCK_LONG_ABOVE
REQUIRE_SHORT_IN_RESISTANCE = False

# ── L1: Stochastic RSI ──
STOCH_RSI_RSI_PERIOD = 14      # RSI period fed into the stochastic formula
STOCH_RSI_STOCH_PERIOD = 14    # Lookback for stochastic min/max of RSI
STOCH_RSI_K_SMOOTH = 3         # Smoothing periods for K line
STOCH_RSI_D_SMOOTH = 3         # Smoothing periods for D (signal) line
STOCH_RSI_OVERSOLD = 20        # K below this → oversold (bullish)
STOCH_RSI_OVERBOUGHT = 80      # K above this → overbought (bearish)

# ── New Filters from Checklist ──
# 4H Trend bias check
ENABLE_4H_TREND_FILTER = True  # Only take LONGs when price > 4H EMA50; SHORTs when <
VOLUME_GATE_MULTIPLIER = 1.3  # Signal candle needs ≥1.3x average volume
REQUIRE_RSI_50_CROSS = False    # Require RSI to cross back above/below 50 after extreme

# ── Optional research-backed filters (ALL DEFAULT OFF) ──────────────────────
# Added to improve expectancy on a small, leveraged account WITHOUT changing
# current behaviour: every flag defaults OFF, so the live bot trades exactly as
# before until you deliberately enable one (ideally backtest it first).

# ADX trend-strength gate — an EMA-cross system bleeds in chop; ADX filters it.
# When ON, a setup must show a real trend: ADX(period) ≥ threshold (and rising,
# if required), computed on the 1h scan candles. Backtests repeatedly show
# ADX > ~20-25 cuts the weak/ranging trades the EMA votes alone would take.
ENABLE_ADX_FILTER = _env_bool("ENABLE_ADX_FILTER", False)
ADX_PERIOD = int(os.getenv("ADX_PERIOD", "14"))
ADX_MIN_THRESHOLD = float(os.getenv("ADX_MIN_THRESHOLD", "20"))
# Stricter opt-in: also require ADX to be rising (an ascending trend phase).
# OFF by default because a strong but plateaued trend has a flat/high ADX and
# would otherwise be rejected — the threshold alone is the core filter.
ADX_REQUIRE_RISING = _env_bool("ADX_REQUIRE_RISING", False)

# Funding-rate sentiment filter — extreme positive funding = crowded longs
# (elevated reversal risk + a holding cost); extreme negative = crowded shorts.
# When ON, skip NEW longs when funding > max and NEW shorts when funding < min.
# Funding is a per-interval fraction (Binance 8h): 0.001 = 0.10%/8h ≈ "extreme".
# Only fetched for an otherwise-qualified setup, so it adds ~0 API load.
ENABLE_FUNDING_FILTER = _env_bool("ENABLE_FUNDING_FILTER", False)
FUNDING_MAX_LONG = float(os.getenv("FUNDING_MAX_LONG", "0.001"))     # block longs above this
FUNDING_MIN_SHORT = float(os.getenv("FUNDING_MIN_SHORT", "-0.001"))  # block shorts below this

# Correlation / same-direction cap — 10 alt LONGs that all move with BTC is one
# leveraged BTC bet, not 10 independent ones. When ON, cap how many live+queued
# positions may share a direction (0 = unlimited). Complements
# MAX_CONCURRENT_POSITIONS (which caps the total regardless of side).
ENABLE_DIRECTION_CAP = _env_bool("ENABLE_DIRECTION_CAP", False)
MAX_SAME_DIRECTION = int(os.getenv("MAX_SAME_DIRECTION", "0"))       # 0 = unlimited

# Risk-based position sizing — size each entry so the loss AT the stop equals a
# fixed % of account equity (size = equity*risk% / stop-distance / leverage),
# instead of a fixed margin per trade. NOTE: on a tiny (~25 USDT) account the
# ~5 USDT Binance min-order floor usually dominates, so this mostly matters as
# the account grows. Clamped to MAX_MARGIN_USDT and available balance; a
# risk-sized order below the min floor is simply skipped (no order placed).
ENABLE_RISK_SIZING = _env_bool("ENABLE_RISK_SIZING", False)
RISK_PCT_PER_TRADE = float(os.getenv("RISK_PCT_PER_TRADE", "0.02"))  # 2% of equity at risk

# ── Position Scaling ──
POSITION_SIZE_4_LIGHTS = 0.5  # 0.5x for 4 lights
POSITION_SIZE_5_LIGHTS = 1.0  # 1.0x for 5 lights
POSITION_SIZE_6_LIGHTS = 1.5  # 1.5x for 6 lights
POSITION_SIZE_COUNTER_TREND = 0.25  # 0.25x if counter 4H trend

# ── Daily Circuit Breaker ──
# NOTE: these count TOTAL losses/drawdown for the day (Taiwan time, reset at
# midnight) — NOT consecutive. The bot halts NEW trades once either limit is hit.
DAILY_MAX_LOSSES = 8  # Stop for the day after this many losing (SL) trades
DAILY_MAX_DRAWDOWN_PCT = -11.5  # Stop for the day after losing trades sum to this %

# Binance universe settings
SYMBOLS = []
QUOTE_ASSET = "USDT"
# Two-tier universe — scan wide, trade narrow:
#   • SCAN_SYMBOL_LIMIT  — how many pairs we SCAN + display for market breadth.
#     These are shown on the dashboard so the platform feels diverse.
#   • TOP_SYMBOL_LIMIT   — how many of those (the most liquid, by volume rank) are
#     actually TRADEABLE. Pairs ranked outside this are scanned/scored but never
#     queue, record, or place an order — they're flagged "Watch only" in the UI.
# Research: mean-reversion needs ~$100M+ daily volume or spread/slippage eats the
# edge; the thin tail produces bad fills. Validated backtests used the top ~75-120
# names, so trading stays in that liquid range while scanning shows more.
SCAN_SYMBOL_LIMIT = 300   # universe we scan + display
TOP_SYMBOL_LIMIT = 150    # of those, only the top-N (by volume) are tradeable

# ── Strategy 2 LIVE execution (opt-in, OFF by default) ───────────────────────
# Strategy 2 is the stand-alone 15m TV.pine confluence scanner. By DEFAULT it is
# alert-only (sends a Telegram note + shows the signal on /strategy2, never an
# order). Flip STRATEGY2_LIVE=true to let a NEW high-conviction signal place a
# REAL bracketed market order through the shared executor.
#
# SAFETY MODEL for the ~25 USDT account — run ONE engine at a time:
#   • S2 live REFUSES to place orders while the S1 bot (bot.lock) is running, so
#     the tiny account is only ever driven by one strategy. Stop S1 to run S2.
#   • It shares the executor's MAX_CONCURRENT_POSITIONS + MAX_MARGIN ceilings.
#   • Only the most liquid pairs (STRATEGY2_LIVE_TOP_N by volume) are eligible.
#   • The global LIVE_TRADING gate still applies — with LIVE_TRADING=false the
#     S2 order is logged as a dry-run, exactly like S1.
STRATEGY2_LIVE = _env_bool("STRATEGY2_LIVE", False)
# "Best plan" conviction gate — only the strongest meter reads fire a live order.
# A LONG needs score ≥ this; a SHORT needs score ≤ (100 − this). 85 ⇒ long ≥85 /
# short ≤15 (stricter than the meter's own 70/30 signal threshold). No BTC-regime
# filter is applied here by design — a strong signal trades even against Bitcoin.
STRATEGY2_LIVE_MIN_SCORE = int(os.getenv("STRATEGY2_LIVE_MIN_SCORE", "85"))
# Conviction → size: (lights, aligned) handed to executor.position_scale. 5 ⇒ 1.0×
# the FIXED_MARGIN_USDT base (1.5 USDT margin → 6 USDT notional at 4× — clears the
# ~5 USDT Binance min-order floor). 6 ⇒ 1.5× (2.25 USDT margin, the MAX ceiling).
STRATEGY2_LIVE_LIGHTS = int(os.getenv("STRATEGY2_LIVE_LIGHTS", "5"))
# Only the top-N most-liquid USDT perps (by 24h volume rank) may place a live S2
# order. Defaults to the same tradeable tier S1 uses; pairs outside it stay
# alert-only no matter how strong the signal.
STRATEGY2_LIVE_TOP_N = int(os.getenv("STRATEGY2_LIVE_TOP_N", str(TOP_SYMBOL_LIMIT)))
