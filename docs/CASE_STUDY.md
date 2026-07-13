# Wolf Scanner — an engineering case study

*Building, operating, and honestly measuring a live multi-venue trading system.*

> This document is written as a portfolio piece: it explains the architecture
> and — more importantly — the engineering judgment calls. Numbers are real
> and current as of July 2026.

---

## The system in one paragraph

Wolf Scanner is a self-hosted trading platform that scans the entire Binance
USDT-perpetuals market (~530 symbols) on a 5-minute cycle, runs a second
independent engine on Bybit, serves a real-time web dashboard through a
Cloudflare tunnel, and operates a Telegram group with eight topic channels —
signals with entry/stop/target plans, a liquidation-cascade radar, a Taiwan
stock-market scanner, macro event alerts, tech news digests, and a command
bot. It is ~17,000 lines of Python across four cooperating processes, guarded
by 300+ tests that must pass before any launch reaches live trading.

## Why this project is different from most "trading bots"

Most hobby trading systems are built to *confirm* that a strategy works.
This one is built to *find out whether it does* — and the honest answer has
repeatedly been "not yet." The most valuable artifacts here are the
investigations that killed bad ideas before they cost real money:

### 1. The 78% win rate that didn't exist
A TradingView strategy showed a 78% win rate over months of backtest. Before
going live, I ported it to closed-bar-only evaluation — and the edge
evaporated (~22% of trades matched). The original script **repainted**: it
used lookahead into unclosed candles that live trading can never have. The
honest port lost money over 100 days of replay, and the one live morning it
ran confirmed the prediction almost exactly. Lesson institutionalized: every
strategy port in this repo evaluates on closed bars, and the pine/ directory
labels each script's repaint status.

### 2. Win rate ≠ edge (48 configurations of proof)
Asked to build "a high win-rate strategy," I first built the experiment: 48
mean-reversion configurations over 400 days of real ETH data, with real fees,
next-open fills, and pessimistic same-candle stop priority. Result: win rates
of 55–79% everywhere, and **46 of 48 configurations lost money** — the
highest-win-rate config had a profit factor of 0.85. Tight targets and wide
stops manufacture win rate while destroying expectancy. The live account's
own record (57% wins, negative net) was the confirming data point. The
deliverable was the evidence, not the strategy the question asked for.

### 3. Data must come from the venue where the money executes
A live short signal failed to fire despite the chart clearly showing it. Root
cause: the bot read Binance candles but traded on Bybit — and on a thin gold
perpetual, the two venues printed different candles. The same 30-minute bar
computed ADX 19.7 on Binance (below the entry gate) and 22.5 on Bybit (above
it). Fix: a per-symbol feed architecture so signal data always comes from the
execution venue. "Data follows execution" is now a design rule.

### 4. Silent message loss found in the logs
A routine log review found two classes of silently dropped Telegram messages:
bursts hitting the ~20 msg/min group limit (HTTP 429 treated as final), and a
chop-day digest exceeding the 4096-character limit (HTTP 400, entire message
gone — while the log claimed success). The fix hardened the single send
choke-point: a thread-safe pacer, retry-after-aware 429 handling, automatic
chunking, and honest delivery logging. The linter added the same week caught
a real `NameError` I introduced during the fix — infrastructure paying for
itself immediately.

## Architecture

```
┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐   ┌────────────┐
│  bot.py      │   │ strategy2_scanner │   │ strategy3_scanner │   │  app.py    │
│  S1 engine   │   │ market sweep +    │   │ Bybit engine      │   │  Flask     │
│  (Binance)   │   │ Telegram services │   │ (gold, flag-flip) │   │  dashboard │
└──────┬───────┘   └────────┬──────────┘   └────────┬──────────┘   └─────┬──────┘
       │                    │                       │                    │
       ▼                    ▼                       ▼                    ▼
  executor.py          8 topic modules        strategy3_exec.py    13 pages, PWA
  (brackets, SL/TP)    (signals, liq radar,   (Bybit orders,       via Cloudflare
                        TW stocks, events,     circuit breaker)     tunnel
                        news, reports, bot)
```

- **Four processes, one flat module tree.** Deliberately no package nesting:
  every module imports siblings directly, processes share code but not state,
  and each state file has exactly one writer.
- **Pure-core pattern.** Every feature separates decision logic (pure
  functions over plain data — unit-testable in milliseconds) from I/O shells
  (thin, exception-guarded, never able to kill a scan loop). The 300+ test
  suite runs in ~8 seconds because of this.
- **Exchange-side safety.** Entries rest as maker-only limit orders with
  stop-loss/take-profit brackets held by the exchange, so exits execute even
  if the bot is down.

## Operational hardening (the unglamorous 40%)

| Layer | What it does |
|---|---|
| Pre-flight gate | `./run_all.sh` refuses to launch if lint or any of the 300+ tests fail |
| CI | GitHub Actions: ruff + full suite with warnings-as-errors on a clean checkout |
| Watchdog | The two scanners cross-monitor all four processes → Telegram alert on death |
| Auto-heal | A launchd agent relaunches the stack after crashes/reboots (respecting deliberate stops) |
| Circuit breaker | The live engine halts new entries after 2 losing closes or a capped daily loss — verified against the exchange's own closed-P&L records, cleared only by an admin command |
| Backups | Daily zip of all state + auth DB, mirrored off-disk to iCloud, 14-day retention |
| Rate-limit discipline | Candle cache (per-bucket invalidation + live-price patching) cut exchange API calls ~3× with byte-identical closed-bar data |

## Measurement loops (closing the honesty gap)

The newest layer makes the system grade itself:

- **Signal outcome tracker** — every alert is replayed against real candles
  48 hours later (stop-before-target pessimism) and scored: full target,
  partial, stopped out, or nothing. A weekly scorecard reports the truth.
- **Equity curve from balances, not trade logs** — one daily snapshot of
  real account totals per venue; trade databases can drift, balances can't.
- **Weekly engine verdicts** — each live engine's real profit factor is
  judged every Monday; PF < 1 over 10+ trades prints "not paying for its
  risk" in the daily report.
- **Paper-first promotion** — the one strategy that passed honest backtesting
  runs as a forward paper-test with a public ledger; promotion to real money
  is a human decision against pre-committed criteria.

## By the numbers

- ~17,000 lines of Python · 13 dashboard pages · 8 Telegram topics
- 300+ tests, all pure-core, ~8s runtime, enforced at launch and in CI
- 2 execution venues (Binance USDT-M, Bybit linear) + TWSE/US market data
- 3 public data-source integrations rebuilt for resilience (WebSocket
  liquidation streams across 3 exchanges with reconnect-forever loops;
  contract-multiplier caches to avoid 100× notional errors)

## What I'd tell someone building their own

1. **Instrument before you optimize, measure before you believe.** Every
   major fix in this repo started with a measured number, not a hunch.
2. **Win rate is the most seductive lie in trading.** Expectancy and profit
   factor or it didn't happen.
3. **The failure modes that hurt are silent ones** — dropped alerts, dead
   processes, repainted backtests. Budget as much engineering for detection
   as for features.
4. **Make honesty structural.** Scorecards, verdicts, and kill-rules that
   run automatically are worth more than discipline you have to remember.
