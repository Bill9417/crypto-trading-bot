# Wolf Scanner — Project Status

_Last updated: 2026-06-17 (stage milestone)_

A quick, human-readable snapshot of where the project stands so you can pick up
fast in any new session.

---

## 1. What the bot does

Scans Binance USDT-perp futures, scores each coin with a **5-light conviction
system** (StochRSI, Strategy Score, EMA momentum, TSI, MACD) plus SMC bonus
lights, and — when a setup passes every filter — queues a bracket trade
(Entry → Stop-Loss → TP1 → TP2). A dashboard (Flask) shows live signals; Telegram
sends alerts. Currently **dry-run / simulation only** (`LIVE_TRADING=False`).

---

## 2. The validated strategy (current config)

| Setting | Value | Why |
|---|---|---|
| **Timeframe** | **1h** | Full-history backtest showed the real edge is on 1h, not 15m |
| Scan interval | 60 min | One scan per closed 1h candle |
| Scan universe | **300 pairs** (`SCAN_SYMBOL_LIMIT`) | Market breadth / diverse view |
| Tradeable | **top 150** (`TOP_SYMBOL_LIMIT`) | Only the most liquid pairs actually trade |
| Min conviction | 5 lights (both directions) | 4-light tier proved negative |
| Shorts | Enabled | Direction is regime-adaptive (BTC filter decides) |
| BTC regime filter | ON (1h EMA50) | "Don't fight Bitcoin" — bull→longs, bear→shorts, chop→both |
| Stop-Loss | ATR×1.5 beyond swing, capped 4% | Defines 1R |
| Take-Profit | TP1 = 1R (50%), TP2 = 2R (50%), stop→breakeven after TP1 | Matches live executor exactly |

### Backtest that justified 1h (top 50 symbols, fees included)
| Window | Trades | Win% | Expectancy | Total | Max DD | Curve R² |
|---|---|---|---|---|---|---|
| 120d | 44 | 65.9% | +0.294R | +49.3% | −3.1R | +0.921 |
| 90d | 36 | 63.9% | +0.282R | +37.9% | −3.1R | +0.901 |
| 60d | 27 | 63.0% | +0.314R | +31.5% | −3.1R | +0.867 |

Consistent across all three windows, both directions positive, and a near-straight
rising equity curve (R²≈0.9). Re-run anytime: `python _fullhist.py`.

> **The bug that mattered:** `fetch_ohlcv` had `if len(batch) < 1500: break`, but
> Binance returns max 1000 bars/call — so every old backtest secretly used only
> ~1000 candles (~10 days on 15m). Fixed in `backtest.py`, `backtest_funding.py`,
> `backtest_pairs.py`. This is why earlier "no edge" conclusions were wrong.

---

## 3. Scan-wide / trade-narrow (how the UI shows it)

- Bot scans **300** pairs, but only the **top 150** by volume can trade.
- Pairs ranked 151–300 are **"Watch only"**: scored and displayed, but they never
  queue, record, or place an order.
- In the dashboard they show a slate **"Watch only"** badge and a faded card, so
  they can't be mistaken for an actionable signal.

---

## 4. Data state — FRESH SLATE (cleared 2026-06-17)

Wiped so performance reflects only the new 1h strategy:
- `signal_record` table → 0 rows
- `scan_results.json` → trade state reset
- `circuit_state.json` → 0 losses / 0% drawdown
- `backtest_runs.json` → empty

**Preserved:** `instance/users.db` (your logins).
**Backups (roll back if needed):** `instance/signals.db.pre1h-*`,
`scan_results.json.bak-*`, `backtest_runs.json.bak-*`.

---

## 5. How to run

Always use the miniforge python (it has numpy/pandas; `python3` does not):

```bash
# Start the scanner bot
/Users/wolfman/miniforge3/bin/python bot.py

# Start the web dashboard
/Users/wolfman/miniforge3/bin/python app.py

# Re-validate the strategy on full history (1h grid)
/Users/wolfman/miniforge3/bin/python _fullhist.py

# Ad-hoc backtest: <days> <n_symbols>
/Users/wolfman/miniforge3/bin/python backtest.py 90 50
```

> Both processes are **stopped** right now (from the data clear). Start them when
> you want it live again.

---

## 6. Alternative strategies tested — all lose after fees

| Strategy | File | Result |
|---|---|---|
| Funding mean-reversion (fade extreme funding) | `backtest_funding.py` | Negative, every grid config |
| Funding momentum (follow funding) | `backtest_funding.py` | Negative |
| Cointegrated-pair stat-arb (market-neutral) | `backtest_pairs.py` | Negative out-of-sample |

The directional 1h strategy is the keeper.

---

## 7. Honest caveats

- The edge is **in-sample** (last 2–4 months). It's real and consistent across
  windows, but forward-test on dry-run/testnet before risking real money.
- Binance **demo** API keys don't work on the futures API — keys were invalid.
  Reconnect a real key (in `.env`, never in `note.txt`) when ready to go live.

---

## 8. Key files

| File | Purpose |
|---|---|
| `config.py` | All strategy settings (timeframe, limits, filters, TP/SL) |
| `bot.py` | Scanner + qualification + trade tracking |
| `app.py` | Flask web dashboard |
| `indicators.py`, `smc.py` | Signal math + Smart Money Concepts |
| `backtest.py` | Main strategy backtester (fees included) |
| `_fullhist.py` | Multi-window 1h validation driver |
| `templates/` | Dashboard, performance, market, backtester pages |
| `instance/signals.db` | Trade records · `users.db` = logins |
