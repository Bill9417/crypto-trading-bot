# PROJECT_MEMORY.md — Wolf Scanner

> Handoff / context doc for a fresh Claude Code session. Read this first.
> Last updated: **2026-06-22**.
> For day-to-day "how do I run it", see [USAGE.md](USAGE.md).

> ## 🔴 LIVE — REAL MONEY (since 2026-06-20)
> The bot is **trading the user's real Binance USDⓈ-M account (~25 USDT)**.
> `LIVE_TRADING=true`, `USE_TESTNET=false` in `app/.env`. Real keys (withdrawals
> OFF, futures ON, IP-restricted). One-Way position mode. Treat any change to
> `executor.py` / `bot.py` / sizing config as money-affecting — test before launch.
> Full details in **section 8**.

---

## 1. What this is

A **personal** crypto trading bot + Flask web dashboard. Scans Binance USDⓈ-M
perpetuals, finds setups, manages trades, and shows everything in a local web UI.

- **Local / single-user only — NOT public.** (It used to be served at
  wolfman.asia via Caddy; that infra is now in `_archive/` and unused.)
- Python interpreter is hard-coded in the scripts:
  **`/Users/wolfman/miniforge3/bin/python`**.
- There is also an auto-loaded Claude memory at
  `~/.claude/projects/-Users-wolfman-Desktop---/memory/` (`MEMORY.md` index) —
  it overlaps with this file; keep both roughly in sync.

---

## 2. Folder structure (reorganized 2026-06-19)

```
交易/
├── crypto/                  ← the application
│   ├── USAGE.md             plain-language run guide
│   ├── PROJECT_MEMORY.md    this file
│   ├── run_all.sh           start web+bot (start|stop|status), runs tests first
│   ├── run_web.sh           web only
│   ├── run_bot.sh           bot only
│   ├── dev.sh               web with hot-reload (dev loop, no test gate)
│   ├── app/                 ALL code + data (import root, pytest rootdir)
│   └── _archive/            old infra, .bak files; _archive/research/ = dead scripts
├── diary/
│   └── knowledge/           reference material (NOT code)
│       ├── notes/           MACD/RSI, SMC/TSI .pages
│       └── tradingview/     Pine scripts incl. strategy4_adaptive_trend.pine
└── .claude/                 Claude settings (settings.local.json)
```

All Python uses `__file__`-relative paths, so the app is location-independent.
Launchers use `$(dirname)/app`, so moving `crypto/` won't break them.

### Key files (all in `crypto/app/`)
- `app.py` — Flask web server, all routes, auth, CSRF, the `/paper` background thread.
- `bot.py` — the **live** trading bot loop (runs **Strategy 1**).
- `backtest.py` — backtester, **Strategy 4** decision (`evaluate_adaptive_trend`),
  trade simulators (`simulate_trade`, `simulate_trade_trailing`), `apply_costs`,
  `summarize`, the `STRATEGIES` registry.
- `paper_s4.py` — Strategy 4 **forward paper-trade** engine (the `/paper` page).
- `indicators.py`, `smc.py` — TA + Smart Money Concepts.
- `config.py` — all thresholds; loads `app/.env`.
- `executor.py` — **live Binance order layer** (USDⓈ-M). Resting LIMIT (Post-Only)
  entry + **`reduceOnly`** SL/TP bracket placed **on fill** (`_place_stop`,
  `_place_take_profits`, `on_resting_filled`); safety gates (`verify_exchange_access`,
  `verify_position_mode`, `halt_live_trading`); concurrency cap; `below_min_order_size`;
  read-only `account_snapshot` (merges strategy-book SL/TP); manual `close_position_market`,
  `set_protection`, `cancel_protection`, `realized_pnl_history`. Strategy-book helpers
  `_fetch_protective_orders` / `_cancel_protective_orders` (cancel needs `params={"stop":True}`).
- `market_data.py`, `market_intel.py`, `telegram_utils.py` — used by bot/web.
- `tests/` — pytest suite; `conftest.py`, `pytest.ini`, `run_tests.sh`, `preflight.sh`.
- Data: `scan_results.json`, `ohlcv_cache.json` (~18MB, rebuildable), `paper_s4_state.json`,
  `instance/users.db` + `instance/signals.db` (SQLite), `logs/`.

---

## 3. Strategies

- **Live bot = Strategy 1** ("Wolf Confluence"): 5-light StochRSI/score/EMA/TSI/MACD
  + SMC bonus + BTC-regime filter. 1h timeframe. ⚠ **FAILED walk-forward validation
  (2026-06-22): net NEGATIVE over 360d, positive only the last ~4 months → overfit /
  recent-regime. The live edge is UNPROVEN — do NOT scale on the backtest. See §4d.**
- **Strategy 4 ("Adaptive Trend")** = trend-following: Donchian(20) breakout + 0.5×ATR
  buffer + price/EMA200 + 4H EMA50 agreement + ADX≥25 & rising + volume gate +
  BTC regime must confirm; **long-only**; Chandelier ATR×3 trailing exit.
  **S4 is NOT live** — it runs only in the backtester and the `/paper` dry-run.
- **Strategy 2 = "Volatility-Squeeze Breakout" (TTM Squeeze)** since 2026-06-22 — replaced
  the old Donchian breakout (the worst strategy; see §4d). Registry KEY stays `trend_breakout`
  (only `fn`/name/desc changed). **S3 (`trend_trailing`) still uses the ORIGINAL Donchian
  breakout** (`evaluate_trend_breakout`) + Chandelier trailing exit.

Web pages: `/` dashboard, `/performance`, `/funnel`, `/account` (admin), `/backtester`,
`/paper`, `/market`, `/login`, `/register`, `/admin/users`. Port **4000**. First
registered user = admin. Shared front-end: `static/app.css` + `templates/_nav.html`
(one nav bar on every authed page, active via `request.endpoint`).

---

## 4. What was done in the last session (2026-06-19)

1. **TradingView Pine v6 of S4** → `diary/knowledge/tradingview/strategy4_adaptive_trend.pine`
   (signal indicator, faithful port of `evaluate_adaptive_trend`).
2. **`/paper` fixed-notional account model** so the dry-run mirrors a real Binance
   account: `NOTIONAL = MARGIN(3) × LEVERAGE(3) = 9 USDT`, `START_BALANCE=100`,
   `MAX_CONCURRENT=33`. Trades store `pnl_usd`; summary shows `net_pnl_usd`,
   `balance`, best/worst; `paper.html` has an account row + Net P&L / Balance tiles +
   `PnL $` column. Env: `PAPER_S4_START_BALANCE`, `PAPER_S4_MARGIN`, `PAPER_S4_LEVERAGE`.
3. **Backtester realism + honesty**: `apply_costs(pnl, bars_held, tf_hours, realism)`
   = `FEE_PCT` always + `2×SLIPPAGE_PCT` + `FUNDING_PCT_PER_8H`. UI "Realistic costs"
   toggle, **survivorship-bias warning banner**, and outlier metrics in `summarize`
   (`top3_r`, `top3_share_pct`, `total_r_ex_top3`). Env: `BT_SLIPPAGE_PCT`, `BT_FUNDING_PCT_8H`.
4. **Security hardening**: removed leftover `debug_report` telemetry (was POSTing to
   localhost:7777); `_resolve_secret_key()` persists a strong key to `app/.secret_key`
   (no more forgeable default); login brute-force throttle (8 fails / 15 min).
5. **pytest suite (39 tests)** in `app/tests/` + gate: `preflight.sh` runs the suite
   before any launch (`SKIP_TESTS=1` to bypass).
6. **Folder reorg** (section 2) + **dev hot-reload** (`dev.sh`, `TEMPLATES_AUTO_RELOAD`,
   `FLASK_RELOAD`). Bot is intentionally NOT hot-reloaded.

---

## 4b. Last session (2026-06-21) — SL bug fix, strategy-book orders, UI overhaul

**Critical bug fixed: filled positions ran with a TP but NO stop-loss.**
- Root cause: the resting path placed SL/TP as `closePosition` triggers at *queue
  time, before the entry filled* → Binance rejects those with **-4509** ("TIF GTE
  can only be used with open positions"); the error was swallowed. `on_resting_filled`
  then retried only the TP, never the SL. The price-monitor then recorded a false SL
  loss and `on_trade_closed` cancelled the TP → fully naked position (seen on EVAA/ZEC).
- Fix (`executor.py`): **the resting path now uses `reduceOnly` for ALL bracket legs**
  (same as the market path + manual setter — reduceOnly avoids both -4509 and -4130).
  New `_place_stop` helper + `plan['sl_placed']`; `on_resting_filled` re-places **both**
  SL and TP after the fill and sends a **loud Telegram alert** if a filled position has
  no stop. `move_stop_to_breakeven` switched to reduceOnly. No order-placement path uses
  `closePosition` anymore.

**Strategy-book cancellation (the SL/TP-stacking fix).** Verified empirically: conditional
orders are invisible to `fetch_open_orders`/`cancel_all_orders`. They MUST be listed/cancelled
with `params={"stop": True}`. Added `_fetch_protective_orders` / `_cancel_protective_orders`
helpers; `account_snapshot` now merges conditional orders (dashboard shows real SL/TP);
`set_protection` reads + cancels priors with the stop flag (no more duplicate conditional
records); new **`cancel_protection()`** + `/api/account/cancel_protection` + a **Reset**
button per coin. `close_position_market`, `on_trade_closed`, `cancel_resting_order`,
`move_stop_to_breakeven` all cancel conditional orders with the stop flag now.

**Web/UI overhaul.**
- **Account control endpoints are admin-only** (`/account` + all `/api/account*`); nav
  link hidden from non-admins.
- **Shared front-end:** new `static/app.css` (theme vars + nav + protection badges) and
  `templates/_nav.html` (one nav bar, active-page highlight via `request.endpoint`),
  included on every authed page → consistent navigation everywhere. `performance.html`
  and `funnel.html` keep their own `:root` on purpose (distinct values / unprefixed names).
- **Account page:** per-position protection badge (🛡 SL+TP / ⚠ NO STOP / ⚠ NAKED),
  top banner "X of N have no stop-loss", risk context (distance to SL/TP/liq, $ at risk,
  R:R), and a divergence flag when the bot's DB thinks a live position is closed.
- **Dashboard:** admin-only protection banner (warns of unprotected/diverged positions),
  scan-freshness pill that turns red when the bot goes quiet, divergence via `db_status`.
- Small bugs fixed earlier in the session: `remove_queued_signal` / `_save_bt_runs` now use
  the numpy JSON encoder; `ensure_db_columns` uses the modern `db.engines[...]` API.
- All 39 tests pass; every page renders 200; JS validated. Web restarted; bot restarted
  (for the executor fix). The 3 then-open positions were left for the user to manage.

---

## 4c. Last session (2026-06-21 PM) — selectable strategy (dry-run + live)

Both the **dry-run page** and the **live bot** can now run ANY of the four
`backtest.STRATEGIES` (S1 default / S2 trend_breakout / S3 trend_trailing /
S4 adaptive_trend), as **two independent settings**. One registry now drives both.

- **Dry-run (`/paper`):** strategy dropdown (`POST /api/paper/strategy`). Each
  strategy keeps its OWN forward record in `paper_state_<key>.json` (the old
  `paper_s4_state.json` auto-migrates to `paper_state_adaptive_trend.json` on first
  load). `paper_s4._advance` now does BOTH trailing (S3/S4) and the 1R/2R bracket
  (S1/S2, faithful port of `simulate_trade`). Active key in `paper_active.json`.
- **Live (`config.LIVE_STRATEGY`, default `default`=S1):** with the default value the
  live bot is **byte-for-byte unchanged** (the S2/S3/S4 path is guarded by
  `if LIVE_STRATEGY != "default"`). Non-default keys use `bot.scan_symbol_alt_strategy`
  (registry dispatch → queue → `place_resting_order(..., manage=...)`). Change it from the
  **gold "⚙ Live strategy" control at the TOP of the Account page** OR the **admin-only
  switcher on the Dashboard** (both `POST /api/account/live_strategy` → writes `.env`).
  NOT on the ⚙ Admin/User-Management page. **Bot restart required** (not hot-reloaded);
  the Dashboard bar shows "Trading now: …" + a "saved ≠ running, restart" warning. `bot.resolve_live_strategy`
  validates + falls back to S1 on a bad key; the running key is written to
  `bot_strategy.json` so the Account page flags a saved≠running divergence.
- **Live trailing for S3/S4 (NEW money-affecting code):** `executor.move_stop()`
  (generalised from `move_stop_to_breakeven`, now **place-then-cancel** so a rejected
  new stop never leaves the position naked); trailing plans place **SL only, no TP**.
  `bot._manage_trailing_signal` ratchets the exchange stop each closed 1h candle
  (mirrors `simulate_trade_trailing`); on a stop hit it **force-closes at market**
  (`close_position_market`) when live, so a stale stop after bot downtime can't
  diverge into a naked position. New `SignalRecord` cols (migrated): `strategy`,
  `manage`, `trail_dist`, `peak`, `trough`, `cur_stop`, `last_bar_ts`.
- **Performance page is strategy-scoped:** `/performance` (+ `/api/performance_stats`,
  `/api/performance_live`) filter `SignalRecord`s by a **strategy** (new column, NULL→
  `default`). A selector (All + 4 strategies) defaults to the strategy the bot is
  RUNNING (`bot_strategy.json` → the "dashboard strategy"); the polling fetches carry
  `?strategy=`. `record_signal(strategy=…)` tags each trade (defaults to the live key).
- **Tests:** +19 (now 58 total, all green) — `tests/test_paper_strategies.py`,
  `tests/test_live_strategy.py`. Verified render/routes/migration on DB copies.
- **⚠ NOT DEPLOYED YET:** the live bot+web are still running the OLD S1 code (this
  session only edited files on disk). `LIVE_STRATEGY` defaults to S1, so a restart is
  safe and keeps trading S1. **Before switching mainnet to S2/S3/S4, validate the new
  live paths on testnet** (`USE_TESTNET=true`) — they have never run with real money.

---

## 4d. Last session (2026-06-22) — strategy bake-off + S1 walk-forward (RESEARCH ONLY)

All research/paper only — **the live bot was NOT touched** (still S1, `LIVE_STRATEGY=default`).

**1. S2 replaced: Donchian breakout → Volatility-Squeeze Breakout (TTM Squeeze).**
The old S2 was the worst strategy by far (a capital incinerator: **−201R, −204R DD over
90d**, 760 trades). Web-researched 3 evidence-based replacements and ran a head-to-head
bake-off (same universe, realism ON): Bollinger mean-reversion, TTM squeeze, trend-aligned
pullback. **Squeeze won** (−0.209R/90d — beats the old S2 on every metric, robust 130-trade
sample) and is now `evaluate_squeeze_breakout` in `backtest.py`. Registry **KEY stays
`trend_breakout`** (persisted in the DB `strategy` col, `LIVE_STRATEGY` env, web dropdown —
renaming breaks them); only `fn`/name/desc changed. **S3 still uses the original
`evaluate_trend_breakout`.** 58 tests green. ⚠ Honest caveat: the squeeze is still
**net-negative**, only LESS bad — in the bake-off **only S1 (+0.219R) and S4 (+0.048R)
beat costs over 90d**; fading + raw breakouts all bled.

**2. S1 FAILED walk-forward validation (the big finding).** `app/walk_forward.py` ran 6
non-overlapping out-of-sample folds over 360d (S1's current locked params, no look-ahead):
**positive in only 2/6 folds — and they're the two MOST RECENT.** Full year: 96 trades,
49% win, **−0.086R/trade, −8.3R total (NET NEGATIVE)**. The strong 30d/90d backtests were
just the favourable recent window (folds 5–6). Classic "tuned to recent regime" overfit.
**Implication: the LIVE S1 edge is UNPROVEN — do NOT scale size based on the backtest.**
Recent live profit (if any) coincides with the one regime where S1 currently works.

**3. Survivorship-corrected re-validation — DONE, conclusion CONFIRMED.**
`app/walk_forward_pit.py` rebuilt a **point-in-time universe** (each fold = top-30 by
trailing volume AS OF that fold's date, from a 70-coin listed-perp pool) to remove the
selection-by-current-popularity bias. **The gap is tiny:** full-year exp −0.086R (biased)
→ **−0.099R (corrected)**, Δ−0.013R; still 2/6 folds positive, same recent-only pattern,
119 trades. So survivorship was NOT what inflated S1 — the negative verdict is REAL and now
more robust (two independent universe methods agree). Why so small: the PIT universe is
mega-cap-dominated (BTC/ETH/SOL/XRP/DOGE/BNB/ADA/SUI/HYPE/1000PEPE/LINK in every fold), so
selection bias is modest. Residual (uncorrected): fully-delisted coins excluded (needs an
external historical symbol list) → true edge a touch worse still, same conclusion: **S1 has
no durable edge.**

**New research tools (read-only, live-safe):** `app/rank_strategies.py` (rank all 4),
`app/walk_forward.py` (rolling OOS), `app/walk_forward_pit.py` (survivorship-corrected).
E.g. `python rank_strategies.py 90 30 1h` · `python walk_forward.py 360 6 30 1h`.

## 8. LIVE TRADING — went live 2026-06-20 (REAL MONEY, ~25 USDT)

The live bot (Strategy 1) was connected to the user's **real Binance mainnet**
futures account and armed. Account ≈ **25 USDT**.

### Sizing — "Plan A" (chosen for a tiny account), in `config.py` + `app/.env`
- `LEVERAGE=4` isolated, `FIXED_MARGIN_USDT=1.5`, `MAX_MARGIN_USDT=2.3`
- `MAX_CONCURRENT_POSITIONS=10`, `USE_POST_ONLY_ENTRY=true`
- `LIVE_PARTIAL_TP=false`, `LIVE_TP_TARGET=tp2` (single 100% close at 2R; breakeven
  still moves at 1R). **Why partial is OFF:** the 50% TP1 slice would be under
  Binance's ~5 USDT min order size at this account size.
- Per-trade notional 6–9 USDT; worst case 10 × 2.25 ≈ 22.5 USDT (fee buffer left).
- **BTC/ETH and pricey coins are auto-skipped** (`below_min_order_size`) — the
  rounded qty is below the min lot; the bot trades mid/low-priced alts.
- Live ≠ dashboard sim by design (dashboard still simulates the 50/50 partial).

### Safety hardening added this session (all in `executor.py` + wired in `bot.py`)
1. **Concurrency cap** — gates only *new* commitments; resting setups still refresh.
2. **One-Way position-mode gate** at startup — halts to dry-run if account is Hedge.
3. **Exchange-access/auth check** at startup — authenticates against the configured
   host; on testnet catches key/host mismatch; halts to dry-run on failure.
4. ~~Offline-fill TP guardian~~ — **superseded 2026-06-21.** The old idea (place
   `closePosition` brackets pre-fill) does NOT work on this account: Binance rejects
   pre-fill triggers with -4509. Brackets are now `reduceOnly` and placed **on fill**
   by `on_resting_filled` (SL + TP), with a loud alert if a stop can't be placed.
   There is NO offline protection — if the bot is down when an entry fills, the
   position is unprotected until the bot is back. See section 4b.
5. **Min-order-size skip** — clean skip instead of exchange rejections.
6. **Post-Only (GTX) entry** — guarantees maker fee; entry is always passive-side.
- Any failed startup gate flips `_safety_halt` → `is_live()` returns False → bot
  keeps scanning but logs orders as dry-run (never sends broken orders).

### Account page (web) — added so the user never opens the Binance app
- **All account routes are ADMIN-ONLY** (2026-06-21): `/account`, `/api/account`,
  `/api/account/history`, `/api/account/close` (POST), `/api/account/protect` (POST),
  `/api/account/cancel_protection` (POST — the Reset button). Template `account.html`.
- Shows balance / positions / live P&L / realized-P&L history; per position:
  **Set** SL/TP, **Reset** (cancel all SL/TP for that coin), **Close** (reduceOnly market).
- Per-position **protection badge** (🛡 SL+TP / ⚠ NO STOP / ⚠ NAKED), **risk context**
  (distance to SL/TP/liq, $ at risk, R:R), and a **divergence flag** when the bot's DB
  thinks a live position is closed (`/api/account` is enriched with `db_status`/`db_diverged`).
- Conditional SL/TP now DO display (snapshot merges the `stop=True` query).

### ⚠️ Binance conditional-order quirk on THIS account (important — UPDATED 2026-06-21)
- **Regular limit/market orders** (incl. the Close button) work and are visible.
- **Conditional trigger orders (STOP / TAKE_PROFIT)** land in Binance's separate
  "strategy/algo order" book (ids like `1000002…`, `algoType=CONDITIONAL`). They are
  **invisible to `fetch_open_orders()` / `fapiPrivateGetOpenOrders`** and are **NOT
  cancelled by `cancel_all_orders()`** (proven empirically: orders survive it).
  - To **LIST** them: `ex.fetch_open_orders(sym, params={"stop": True})`.
  - To **CANCEL** them: `ex.cancel_order(id, sym, params={"stop": True})`.
- **`closePosition` triggers fail TWO ways here:** **-4509** before a position exists
  (pre-fill), and **-4130** when two closePosition legs sit on the same side. So the
  bot's old pre-fill `closePosition` brackets never actually protected anything.
- **`reduceOnly` triggers work** once the position exists, coexist (SL+TP), and can be
  set repeatedly — this is the only mechanism used now (bot + manual).
- **Fixes applied (section 4b):** the bot places `reduceOnly` SL+TP **on fill**;
  `account_snapshot` merges the `stop=True` query so the dashboard **does** now show
  SL/TP; `set_protection` clears stale conditional orders with the stop flag (no more
  stacking); a **Reset** button (`cancel_protection`) clears all SL/TP for a coin.

### Run mode
- Started **detached**: `./run_all.sh bg` (nohup+disown) — survives terminal close.
  Stop with `./run_all.sh stop`. The live fill-watcher used during the session is
  session-bound and ends when the terminal/Claude session closes; the bot does not.

### Cleanup done
- Wiped 10 stale dry-run `signal_record` rows (2026-06-18) → live stats start fresh.
- Reset `circuit_state.json` to 0 for the live day.

---

## 5. Critical context / decisions (don't re-litigate)

- **The backtest is survivorship-biased**: `top_symbols()` replays *today's*
  top-volume coins over history → headline numbers are optimistic. The `/paper`
  forward run on the live universe is the honest preview. This is THE caveat.
- **Low win rate + big return is legit** for S4 (trend-following: small capped
  losses, runners via the trailing stop). But live will land below the backtest
  due to survivorship + slippage + funding.
- **Fixed-notional** makes `/paper` dollar math match the real account; the user
  plans 100 USDT, 3 USDT margin, 3× leverage, ignoring funding. The math is
  faithful; the % returns still won't equal the backtest (see above).
- **Bot is not hot-reloaded on purpose** — a live trading bot must not silently
  restart mid-trade. Strategy/bot changes need a deliberate `./run_all.sh`.

---

## 6. Gotchas

- **Apps may be stopped.** They were stopped for the reorg; restart with
  `cd crypto && ./run_all.sh`. Check with `./run_all.sh status`.
- **One-time re-login**: the session secret key rotated, so old sessions are invalid.
- **Stale `app/bot.lock`** after a hard kill can block the bot; `run_all.sh` clears it.
- **Run tests after editing** `app/*.py`: `cd app && ./run_tests.sh` (or just launch —
  the gate runs them). Green = safe.

---

## 7. Open / possible next steps

- **Finish the survivorship-corrected walk-forward** (`walk_forward_pit.py`) and report the
  gap vs the biased `walk_forward.py` run (§4d, step 3).
- **Decide what to do about S1 being overfit (§4d):** true walk-forward OPTIMIZATION
  (re-tune params per fold → is it fixable?), OR regime-slice S1 (longs-only / BTC-bull to
  find the durable subset), OR accept it has no durable edge and treat live S1 as an experiment.
- Fully fix survivorship: historical symbol universe incl. DELISTED coins (needs external data).
- Run the same walk-forward / survivorship checks on S4 before trusting its +0.048R/90d.
- Telegram (or push) alerts when `/paper` opens/closes a trade.
- Persist an equity-curve history + chart on `/paper` and `/performance`.
- (Maybe) promote S4 to live in `bot.py` — only after a real forward track record AND it
  passes the validation above.

---

*Tip: to have Claude Code auto-load this, you can copy/symlink it to `CLAUDE.md`
at the repo root. Otherwise just tell Claude "read crypto/PROJECT_MEMORY.md".*
