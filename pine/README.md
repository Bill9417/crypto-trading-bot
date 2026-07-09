# pine/ — TradingView strategy scripts

All the Pine **strategy** scripts live here. (`TV.pine` in the repo root is
the original **indicator** — the source of the confluence signal — and stays
where it is.)

## Which script is which

| File | What it is | Status |
|------|-----------|--------|
| `TV_strategy_XAUT_30min.pine` | **"Vegas Flag Flip — XAUT 30m"** — the exact rules the live bot runs on gold (flag + Vegas entry, exit on opposite flag, emergency SL 1.5%, 50x, 2000 USDT order size). | **LIVE — XAUT trades this.** |
| `TV_strategy_ETH_SOL_PAIRS.pine` | **Pairs hedge** — market-neutral ETH/SOL spread mean-reversion (z-score of the log ratio; long the cheap leg, short the rich one, both 500 USDT). Non-repainting by construction (no `security()` — runs on a ratio chart, closed bars only). Fees for both legs modeled. | Testing only — NOT live. ⚠ Honest 1-year sim: ~+35–110 USDT/yr best case, and the LAST 4 months were negative in every configuration (spread is trending). Paper-test first. |
| `TV_strategy_ETH_MOM_2h.pine` | **ETH 14-day momentum** — one rule: long above the close 14 days ago, short below, checked every closed 2h bar. Non-repainting, fees modeled. 2-year test: +770 on 500 USDT notional, all 8 quarters positive at N=168 (neighbors +150–300 — expect those, not the headline); ~30% win rate, trend-style. | Testing only — NOT live. |
| `TV_strategy_HYPE_15min.pine` | HYPE 15m trend-catcher (formerly "V3"): break-even OFF, ATR chop-gate 0.6, SL 2.5%, **25x max** (a 2.5% stop at 50x sits outside liquidation). Walk-forward tested +715 USDT / 13 months, ~31% win rate. | Testing only — NOT live. |

Deleted in the 2026-07-09 cleanup: `TV_strategy_TP.pine` (take-profit
experiment, no longer wanted), `TV_strategy_V2.pine` (break-even anti-chop —
13-month replay showed its high win rate came with a net LOSS; the break-even
code itself still lives in the bot behind `STRATEGY3_BE_SYMBOLS`), and
`TV_strategy_ETH_SOL_30min.pine` (JustUncleL "Open Close Cross" — its 78%
win-rate backtest was lookahead repainting; the bot's OCC engine port remains
in `app/strategy3_occ.py` but no symbol uses it). All remain in git history
(`git log --diff-filter=D -- 'pine/*.pine'`).

## Testing notes

- **XAUT**: load `TV_strategy_XAUT_30min.pine` on XAUTUSDT **30m** — defaults
  are already correct (2000 USDT order size = live sizing 40 margin × 50x).
- **ETH / SOL pairs**: open the chart symbol
  `BINANCE:ETHUSDT.P/BINANCE:SOLUSDT.P` on **2h** and apply
  `TV_strategy_ETH_SOL_PAIRS.pine` — defaults are already tuned (500 USDT per
  leg). The strategy trades the RATIO; on a real account one "long spread"
  = long ETH + short SOL, 500 USDT each.
- **ETH momentum**: `TV_strategy_ETH_MOM_2h.pine` on ETHUSDT.P **2h**,
  defaults are already correct (500 USDT order size, N=168). Expect losing
  streaks — ~30% win rate with occasional big trend rides is its nature.
- **HYPE**: `TV_strategy_HYPE_15min.pine` on HYPEUSDT **15m**, defaults are
  already correct (650 qty, 25x).
- The live bot reads signals from **Binance** charts and places orders on
  **Bybit** — small price differences vs a Bybit chart in TV are normal.

None of these files are read by the bot at runtime — the live rules are ported
into `app/strategy3_signal.py` (flag-flip) and `app/strategy3_occ.py` (OCC),
both driven by `app/strategy3_scanner.py`. Editing a `.pine` file changes
nothing live; porting a change to the bot is a separate, explicit step.
