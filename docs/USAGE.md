# Crypto — Wolf Scanner

A personal crypto trading bot + web dashboard. Scans Binance perpetuals for
setups, manages trades, and shows everything in a local web UI. **Runs only on
your own machine — not meant to be public.**

---

## 📁 What's in here

```
crypto/
├── USAGE.md          ← you are here
├── run_web.sh        ← start the web dashboard  (http://127.0.0.1:4000)
├── run_bot.sh        ← start the trading bot
├── run_all.sh        ← start BOTH (opens two Terminal tabs)
│
├── app/              ← all the code + data (the actual program)
│   ├── app.py            web server (the dashboard)
│   ├── bot.py            the live trading bot (Strategy 1)
│   ├── backtest.py       strategy backtester + Strategy 4 logic
│   ├── paper_s4.py       Strategy 4 live paper-trade (the /paper page)
│   ├── indicators.py     RSI / ATR / volume / etc.
│   ├── config.py         all settings (reads app/.env)
│   ├── templates/        the web pages (HTML)
│   ├── static/           icons, styles
│   ├── tests/            automated checks (the safety net)
│   ├── .env              your secrets (API keys, Telegram) — never share
│   └── *.json            saved data (scans, trades, cache)
│
└── _archive/         ← old/unused files kept just in case
```

> 📓 **Reference material lives in `../diary/knowledge/`** (outside `crypto/`):
> `notes/` (MACD/RSI, SMC/TSI study notes) and `tradingview/` (Pine scripts,
> incl. `strategy4_adaptive_trend.pine`).

---

## 🚀 How to run

Open Terminal and run from the `crypto/` folder:

| I want to…                            | Command              |
| ------------------------------------- | -------------------- |
| Start **both** (recommended)          | `./run_all.sh`       |
| …and **stop** both                    | `./run_all.sh stop`  |
| …**check** what's running             | `./run_all.sh status`|
| Start just the **web dashboard**      | `./run_web.sh`       |
| Start just the **trading bot**        | `./run_bot.sh`       |

- `run_all.sh` starts both in one window, logs to `app/logs/`, streams the bot
  log live, and stops both cleanly when you press **Ctrl+C**.
- The web opens at **http://127.0.0.1:4000**.
- Every launcher **runs the tests first** and refuses to start if anything is
  broken (your safety lock). To start anyway: `SKIP_TESTS=1 ./run_all.sh`.

### 🔁 Making changes without restarting (dev mode)

Tired of re-running `run_all.sh` after every edit? Use **`./dev.sh`** for the web:

```bash
./dev.sh        # web only, auto-reloads on save → http://127.0.0.1:4000
```

| You changed…                          | What you do to see it            |
| ------------------------------------- | -------------------------------- |
| A page / CSS / JS (`templates/`, `static/`) | **just refresh the browser** — no restart at all |
| Web Python (`app.py`, routes)         | nothing — `dev.sh` **reloads itself** on save, then refresh |
| Bot / strategy (`bot.py`, `backtest.py`, `config.py`) | restart the bot: `./run_all.sh` (the bot is intentionally not hot-reloaded) |

So for UI and web work you basically never restart. Only **bot/strategy logic**
needs a deliberate restart — which is what you want, since a live trading bot
should never silently restart itself mid-trade. `dev.sh` skips the test gate for
speed; run `./run_all.sh` (which runs the tests) when you're done iterating.

---

## 🖥️ The web pages

| Page          | What it shows                                                  |
| ------------- | -------------------------------------------------------------- |
| `/`           | Live dashboard — current signals and quick stats               |
| `/performance`| Track record of the live bot's trades                          |
| `/backtester` | Replay strategies over history (S1–S4). Toggle realistic costs |
| `/paper`      | Strategy 4 forward paper-trade as a **fake 100-USDT account**  |
| `/market`     | Market overview / intel                                        |

First time: register a user (the **first** account becomes admin).

---

## 🧠 The strategies (quick map)

- **The live bot (`bot.py`) runs Strategy 1** — the multi-signal confluence setup.
- **Strategy 4 ("Adaptive Trend")** is the trend-following one. It is **not live** —
  it runs in the `/paper` dry-run and the backtester so you can build a track
  record before risking anything. See [knowledge/tradingview](knowledge/tradingview)
  for its TradingView version.

---

## ✅ Tests (the safety net)

The tests check the things you can't eyeball — the strategy math, the trade
exits, and the fake-account dollar math — in about 1 second.

```bash
cd app
./run_tests.sh           # run everything
./run_tests.sh -v        # show each test name
```

**Run this after changing any code in `app/`.** Green = safe. Red = you broke
something; fix it before trading. The launchers run it for you automatically.

---

## ⚙️ Settings & data

- **Settings / secrets:** `app/.env` (copy from `app/.env.example`). Holds your
  Binance API keys and Telegram token. Strategy thresholds live in `app/config.py`.
- **Your data** lives in `app/`: `scan_results.json` (latest scan),
  `ohlcv_cache.json` (price cache, large — safe to delete, it rebuilds),
  `paper_s4_state.json` (the fake account), and `instance/` (the user + signal
  databases). Back these up if you care about your history.

---

## 🔧 Common tasks

- **Change how much the paper account risks per trade:** set
  `PAPER_S4_MARGIN` / `PAPER_S4_LEVERAGE` in `app/.env` (default 3 USDT × 3×).
- **Make the backtest pessimistic/realistic:** the "Realistic costs" checkbox on
  `/backtester` (adds slippage + funding).
- **Reset the paper account:** the Reset button on `/paper` (admin only).
- **Forgot you're logged out after an update:** the session key rotated once for
  security — just log in again.
