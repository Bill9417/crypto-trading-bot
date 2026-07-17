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

# ── Second bot: "the rest of auto messages" (2026-07-10) ──────────────────
# The ORIGINAL bot (BOT_TOKEN/CHAT_ID above) is now the "signals" channel —
# RSI-extreme alerts + every good-entry-chance card (S1 queued/filled, S2
# high-conviction + digest). This SECOND bot carries everything else the bot
# sends automatically: trade fills/closes, S3 (XAUT) live trade alerts,
# naked-position safety, halts, startup/status notices. Leave the two vars
# below empty and nothing changes — those messages keep following the old
# TELEGRAM_QUIET/force rules on the signals bot. Fill them in (new bot from
# @BotFather, chat id from api.telegram.org/bot<token>/getUpdates after you
# message it once) and the split takes effect immediately, no restart logic
# needed beyond the normal bot restart.
ALERTS_BOT_TOKEN = os.getenv("TELEGRAM_ALERTS_BOT_TOKEN", "")
ALERTS_CHAT_ID = os.getenv("TELEGRAM_ALERTS_CHAT_ID", "")

# ── Topics group (2026-07-10) — supersedes the two-bot split above ────────
# One forum-enabled Telegram group ("Wolfman_Group"), one bot (the original
# signals bot), two topic threads inside it: "📊 Signals" and "🔔 Alerts".
# When GROUP_CHAT_ID is set, send_message() posts here instead of the
# two-bot setup — same category logic (signals vs alerts), just delivered as
# threads in one group instead of two separate chats. Leave GROUP_CHAT_ID
# empty and the two-bot (or single-bot) behaviour above still applies.
TELEGRAM_GROUP_CHAT_ID = os.getenv("TELEGRAM_GROUP_CHAT_ID", "")
TELEGRAM_SIGNALS_THREAD_ID = os.getenv("TELEGRAM_SIGNALS_THREAD_ID", "")
TELEGRAM_ALERTS_THREAD_ID = os.getenv("TELEGRAM_ALERTS_THREAD_ID", "")
# Third topic thread: "🌍 Events" — big market-moving events (Fed/FOMC, war,
# regulation, hacks, whale flows, BTC/ETH shock moves) from event_radar.py.
# Empty = events fall back into the Alerts thread.
TELEGRAM_EVENTS_THREAD_ID = os.getenv("TELEGRAM_EVENTS_THREAD_ID", "")
# Fourth topic thread: "💻 Tech" — 6-hourly tech/AI news digest (HN,
# TechCrunch, The Verge, Ars, Simon Willison) from tech_news.py.
# Empty = tech digests fall back into the Alerts thread.
TELEGRAM_TECH_THREAD_ID = os.getenv("TELEGRAM_TECH_THREAD_ID", "")
# Fifth topic thread: "📈 Daily Report" — one morning message per day with
# both live accounts (Binance + Bybit), realized P&L, and the day's market
# context, from daily_report.py. Empty = reports fall back into Alerts.
TELEGRAM_REPORT_THREAD_ID = os.getenv("TELEGRAM_REPORT_THREAD_ID", "")
# Sixth topic thread: "🇹🇼 台股" — one message per TWSE trading day after the
# 13:30 close: TAIEX regime (enter / stand aside) + TW50 pullback setups with
# entry/SL/TP, from tw_stocks.py. Empty = falls back into Alerts.
TELEGRAM_TWSTOCKS_THREAD_ID = os.getenv("TELEGRAM_TWSTOCKS_THREAD_ID", "")
# Seventh topic thread: "💥 清算" — BTC/ETH liquidation-cascade (stop-run)
# alerts from liq_alerts.py. Empty = falls back into Alerts.
TELEGRAM_LIQ_THREAD_ID = os.getenv("TELEGRAM_LIQ_THREAD_ID", "")
# Eighth topic thread: "📈 S1 交易訊號" — the Strategy-1 copy-trade feed. The
# full live trade lifecycle (掛單 → 進場成交 → TP1/停損/止盈/保本 → 取消) in
# 中文 with Bybit prices/links, so followers can mirror the trades. Empty =
# falls back into the Signals thread. Provision it with create_s1_topic.py.
TELEGRAM_S1SIGNALS_THREAD_ID = os.getenv("TELEGRAM_S1SIGNALS_THREAD_ID", "")

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
# Binance's TradFi stock/ETF perps (AAPL, NVDA, QQQ, SPY, …) require a separately
# signed agreement; without it EVERY order is rejected with -4411, so by default
# they are dropped from both scanners' universes and refused by the executor.
# Sign the agreement on Binance first, then set EXCLUDE_TRADFI_PERPS=false to
# let them scan/trade like any other perp.
EXCLUDE_TRADFI_PERPS = _env_bool("EXCLUDE_TRADFI_PERPS", True)

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

# ── Strategy 2 ⭐ PREMIUM signal gate (2026-07-16) ───────────────────────────
# The Signals topic's real win rate was finally MEASURED (research_s2_winrate:
# 120 real fired signals from the state backups + a 60-day replay of the
# exact production signal code over the top-60 perps). Findings:
#   • the raw feed (score 70/30 bar) reached TP1 before SL only ~49% of the
#     time and 65% eventually stopped out — the user's "win rate is low" was
#     real, not felt;
#   • the score alone did NOT help (conviction ≥80 was no better than the
#     firehose on real signals);
#   • BTC-regime ALIGNMENT was the big honest lever (aligned 57% vs
#     counter-BTC 41% TP1-first on real fires), consistent with S1's
#     "don't fight Bitcoin" backtests.
# So public alerts now carry a ⭐ PREMIUM tier: conviction ≥ MIN_SCORE AND
# BTC-aligned AND ADX ≥ MIN_ADX (trend, not chop). Only ⭐ signals send the
# immediate bilingual alert; the digest keeps a wider bar (below) so the
# topic stays alive without drowning readers in coin-flip signals.
#
# 2026-07-17 re-measure (same 60d/6,545-fire replay): of all combined gates,
# conv≥85 + aligned + ADX≥20 was the BEST on both axes — 51.6% TP1-first (vs
# 50.0% at conv≥80) and the highest managed expectancy of the grid (+0.113R vs
# +0.076R). At the ⭐ plan geometry (SL 2×ATR, TP1 0.75R) that gate reaches its
# first target 58.7% of the time. So MIN_SCORE 80→85: fewer signals (~13/day
# over 60 symbols), higher win rate, better expectancy. (Alert-only tier —
# STRATEGY2_LIVE is false — so this changes which alerts fire, not live orders.)
STRATEGY2_PREMIUM_MIN_SCORE = int(os.getenv("STRATEGY2_PREMIUM_MIN_SCORE", "85"))
STRATEGY2_PREMIUM_REQUIRE_ALIGNED = _env_bool("STRATEGY2_PREMIUM_REQUIRE_ALIGNED", True)
STRATEGY2_PREMIUM_MIN_ADX = float(os.getenv("STRATEGY2_PREMIUM_MIN_ADX", "20"))
# Cap immediate ⭐ alerts per sweep so a market-wide pump (many alts aligning
# with BTC at once) can't fire a 20-message burst into the promo channel. Any
# premium signal beyond the cap still lands in the 30-min digest and on the
# /strategy2 page — it just doesn't get its own instant push. 0 = unlimited.
STRATEGY2_PREMIUM_ALERTS_PER_SWEEP = int(os.getenv("STRATEGY2_PREMIUM_ALERTS_PER_SWEEP", "5"))
# Digest bar: minimum conviction (score distance from neutral: long score ≥ X,
# short score ≤ 100−X) AND not fighting the BTC regime. Signals below the bar
# still show on the /strategy2 page — they just don't spam the topic.
STRATEGY2_DIGEST_MIN_CONV = int(os.getenv("STRATEGY2_DIGEST_MIN_CONV", "75"))
STRATEGY2_DIGEST_SKIP_COUNTER_BTC = _env_bool("STRATEGY2_DIGEST_SKIP_COUNTER_BTC", True)
# Minimum stop distance as a fraction of price. The replay caught USDC firing
# 82 signals in 60 days with a 0.002%-of-price stop — fees alone were ~55R on
# that plan. A stop tighter than this floor is a fee-burn, not a trade: the
# signal is dropped entirely (not alerted, not listed, not recorded).
STRATEGY2_MIN_STOP_PCT = float(os.getenv("STRATEGY2_MIN_STOP_PCT", "0.003"))
# ⭐ premium plan geometry — measured on the 60d replay (6,545 fires, fees in):
# SL 2×ATR + TP1 at 0.75R was the best win-rate/expectancy balance for the
# gate above (58.7% of premium signals reached TP1 before the stop; the old
# 1R TP1 on a 1.5×ATR stop was ~50%). TP2 stays a 2R runner. These knobs
# shape ONLY the published ⭐ plan; live execution keeps its own levels.
#
# TP1_R is the win-rate LEVER. Measured on the conv≥85+aligned gate (SL 2×ATR):
#   TP1 0.50R → 67.7% hit    TP1 0.75R → 58.7%    TP1 1.0R → 50.4%
# BUT tighter = smaller wins: after fees EVERY setting is slightly negative, so
# 0.5R buys a prettier win rate at the cost of expectancy (the classic high-WR
# trap). 0.75R is the honest default; drop to 0.50R only if you knowingly want
# the higher hit rate for the promo optics, not because it makes more money.
STRATEGY2_PREMIUM_SL_MULT = float(os.getenv("STRATEGY2_PREMIUM_SL_MULT", "2.0"))
STRATEGY2_PREMIUM_TP1_R = float(os.getenv("STRATEGY2_PREMIUM_TP1_R", "0.75"))
STRATEGY2_PREMIUM_TP2_R = float(os.getenv("STRATEGY2_PREMIUM_TP2_R", "2.0"))

# ── Strategy 3 — "Vegas Flag Flip" (opt-in, OFF by default) ──────────────────
# BTC + SOL + HYPE + XAUT by default (STRATEGY3_SYMBOLS). Signals come from
# BINANCE charts (same candles as the TradingView chart); orders go to the
# user's BYBIT account (BYBIT_API_KEY / BYBIT_API_SECRET in .env) — a SEPARATE
# account from the Binance one S1/S2 drive, so the bot.lock / one-Binance-
# engine rules do not apply to it.
# Flag-flip engine mirrors pine/TV_strategy_XAUT_30min.pine: a TV.pine
# confluence flag sets the direction, the
# Vegas line (SMA5 of EMA200) must agree before entry, and the position is held
# until the OPPOSITE flag appears, then flipped. Manual closes on Bybit are
# respected — the scanner stands down for that symbol until the NEXT flag.
# By default the scanner is ALERT-ONLY. Real orders need STRATEGY3_LIVE=true
# AND Bybit keys AND LIVE_TRADING=true (the master gate: false ⇒ dry-run logs).
STRATEGY3_LIVE = _env_bool("STRATEGY3_LIVE", False)
# Comma-separated base assets it may trade (must exist on BOTH Binance futures
# — chart source — and Bybit linear perps — execution venue).
STRATEGY3_SYMBOLS = [s.strip().upper() for s in
                     os.getenv("STRATEGY3_SYMBOLS", "BTC,SOL,HYPE,XAUT").split(",") if s.strip()]
# Flag threshold — same semantics as the TV.pine meter (long ≥ TH, short ≤ 100−TH).
# 70 matches the indicator's default triangles, NOT S2's stricter 85 gate.
STRATEGY3_SCORE_TH = int(os.getenv("STRATEGY3_SCORE_TH", "70"))
# ADX regime filter for the flag (0 disables). 20 matches the indicator default.
STRATEGY3_ADX_TH = int(os.getenv("STRATEGY3_ADX_TH", "20"))
# Emergency (disaster) stop for flip positions — NOT part of the strategy rules.
# The exit is the opposite flag; this stop only caps a crash while waiting for
# it. It is attached to the Bybit order and re-armed by the guardian if missing.
STRATEGY3_EMERGENCY_SL_PCT = float(os.getenv("STRATEGY3_EMERGENCY_SL_PCT", "0.04"))
# Shared defaults — timeframe + position size (margin × leverage = notional)
# for any symbol WITHOUT an override in STRATEGY3_OVERRIDES below.
STRATEGY3_TIMEFRAME = os.getenv("STRATEGY3_TIMEFRAME", "15m")
STRATEGY3_MARGIN_USDT = float(os.getenv("STRATEGY3_MARGIN_USDT", "3"))
STRATEGY3_LEVERAGE = int(os.getenv("STRATEGY3_LEVERAGE", str(LEVERAGE)))
# Candle feed for the signal computation: "binance" (the original split-
# exchange spec) or "bybit" (the venue the orders fill on). 2026-07-10 lesson:
# XAUT is a THIN market — Binance and Bybit print visibly different candles,
# and a near-threshold gate split on the same 30m bar (ADX 19.7 on Binance vs
# 22.5 on Bybit): the short fired on the Bybit feed only. For thin symbols,
# reading candles from the execution venue keeps chart, signal and fills on
# one tape. Shared default + per-symbol override below.
STRATEGY3_FEED = os.getenv("STRATEGY3_FEED", "binance").strip().lower()
# Per-symbol overrides. Gold (XAUT) trends far slower than the cryptos here, so
# it trades a 30m chart (vs the others' 15m) at a bigger size — the same
# emergency-SL %, score/ADX thresholds and leverage still apply to it unless
# also overridden here. Any key omitted falls back to the shared defaults above.
STRATEGY3_OVERRIDES = {
    "XAUT": {
        "timeframe": os.getenv("STRATEGY3_XAUT_TIMEFRAME", "30m"),
        "margin": float(os.getenv("STRATEGY3_XAUT_MARGIN_USDT", "3")),
        "feed": os.getenv("STRATEGY3_XAUT_FEED", "").strip().lower() or None,
    },
}
# V2 anti-chop break-even (backtested 2026-07 on HYPE 15m; its pine test
# script was retired in the 2026-07-09 pine/ cleanup): once a
# position is BE_TRIGGER into profit, the resting Bybit stop jumps from the
# wide emergency level to entry ± BE_OFFSET (≈ fees), so sideways chop that
# pokes into profit and reverses scratches at ~0 instead of losing the full
# emergency-SL distance. The exit is STILL the opposite flag — winners are not
# capped. Applied only to the symbols listed here (HYPE by default; gold's
# slow trends retrace to entry early, so break-even would trim its winners).
STRATEGY3_BE_SYMBOLS = [s.strip().upper() for s in
                        os.getenv("STRATEGY3_BE_SYMBOLS", "HYPE").split(",") if s.strip()]
STRATEGY3_BE_TRIGGER_PCT = float(os.getenv("STRATEGY3_BE_TRIGGER_PCT", "0.0075"))
STRATEGY3_BE_OFFSET_PCT = float(os.getenv("STRATEGY3_BE_OFFSET_PCT", "0.0015"))

# ── Strategy 3 second engine: "OCC" (Open Close Cross) ──────────────────────
# Port of JustUncleL's "Open Close Cross (its pine was deleted 2026-07-09 —
# the TV backtest repaints; engine kept for reference, no symbol uses it)
# Strategy R5.1", default settings): an SMMA(MA_LEN) of the OPEN series and of
# the CLOSE series is computed on an alternate resolution = chart timeframe ×
# RES_MULT (30m × 3 = 90m bars, resampled from Binance 30m candles); when the
# close-MA crosses OVER the open-MA → flip long, crosses UNDER → flip short.
# Stop-and-reverse: always in the market after the first cross, one entry per
# cross (a stop-out or manual close stands down until the NEXT cross).
# ⚠ The TradingView original REPAINTS with these defaults (security() with
# lookahead) — this port acts only on CLOSED 90m data, so live entries lag the
# too-good default-settings backtest. The pine has NO stop; live adds the
# emergency SL below as pure disaster protection (not part of the strategy).
# Symbols listed here are traded with the OCC engine INSTEAD of the flag-flip
# rules (they must also be in STRATEGY3_SYMBOLS to be scanned at all).
STRATEGY3_OCC_SYMBOLS = [s.strip().upper() for s in
                         os.getenv("STRATEGY3_OCC_SYMBOLS", "").split(",") if s.strip()]
STRATEGY3_OCC_TIMEFRAME = os.getenv("STRATEGY3_OCC_TIMEFRAME", "30m")
STRATEGY3_OCC_RES_MULT = int(os.getenv("STRATEGY3_OCC_RES_MULT", "3"))
STRATEGY3_OCC_MA_LEN = int(os.getenv("STRATEGY3_OCC_MA_LEN", "8"))
STRATEGY3_OCC_MARGIN_USDT = float(os.getenv("STRATEGY3_OCC_MARGIN_USDT", "50"))
STRATEGY3_OCC_LEVERAGE = int(os.getenv("STRATEGY3_OCC_LEVERAGE", "10"))
STRATEGY3_OCC_SL_PCT = float(os.getenv("STRATEGY3_OCC_SL_PCT", "0.04"))


def strategy3_params(base: str) -> dict:
    """Effective per-symbol settings for one Strategy 3 symbol. OCC symbols get
    the whole OCC block; everything else runs the flag-flip engine with
    STRATEGY3_OVERRIDES winning per key over the shared defaults above.
    'sl_pct' is the emergency stop for THIS symbol (the OCC pair trades at
    lower leverage with a wider disaster stop than the flag-flip symbols)."""
    b = base.upper()
    if b in STRATEGY3_OCC_SYMBOLS:
        return {
            "engine": "occ",
            "timeframe": STRATEGY3_OCC_TIMEFRAME,
            "margin": STRATEGY3_OCC_MARGIN_USDT,
            "leverage": STRATEGY3_OCC_LEVERAGE,
            "sl_pct": STRATEGY3_OCC_SL_PCT,
            "res_mult": STRATEGY3_OCC_RES_MULT,
            "ma_len": STRATEGY3_OCC_MA_LEN,
            "feed": STRATEGY3_FEED,
        }
    ov = STRATEGY3_OVERRIDES.get(b, {})
    return {
        "engine": "flagflip",
        "timeframe": ov.get("timeframe", STRATEGY3_TIMEFRAME),
        "margin": ov.get("margin", STRATEGY3_MARGIN_USDT),
        "leverage": ov.get("leverage", STRATEGY3_LEVERAGE),
        "sl_pct": ov.get("sl_pct", STRATEGY3_EMERGENCY_SL_PCT),
        # `or` (not a get-default): an unset env override stores None and must
        # still fall back to the shared feed
        "feed": ov.get("feed") or STRATEGY3_FEED,
    }
