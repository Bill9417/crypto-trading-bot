# pine/ — every TradingView script in one place

```
pine/
├── strategies/     backtestable strategy() scripts — entries, exits, P&L
└── indicators/     chart indicator() scripts — visual tools, no orders
```

None of these files are read by the bot at runtime — live rules are ported
into `app/strategy3_signal.py` (flag-flip) and `app/strategy3_occ.py` (OCC),
driven by `app/strategy3_scanner.py`. Editing a `.pine` file changes nothing
live; porting a change into the bot is a separate, explicit step.

## strategies/

| File | What it is | Status |
|------|-----------|--------|
| `TV_strategy_XAUT_30min.pine` | **"Vegas Flag Flip — XAUT 30m"** — the exact rules the live bot runs on gold (flag + Vegas entry, exit on opposite flag, emergency SL 1.5%, 50x, 1500 USDT order size). | **LIVE — XAUT trades this on Bybit.** Since 2026-07-11 the bot also *reads* Bybit candles (data follows execution), so a `BYBIT:XAUTUSDT.P` chart matches the bot exactly. |
| `TV_strategy_HYPE_15min.pine` | HYPE 15m trend-catcher (formerly "V3"): break-even OFF, ATR chop-gate 0.6, SL 2.5%, **25x max** (a 2.5% stop at 50x sits outside liquidation). Walk-forward tested +715 USDT / 13 months, ~31% win rate. | Testing only — NOT live. |
| `TV_strategy_ETH_MOM_2h.pine` | **ETH 14-day momentum** — one rule: long above the close 14 days ago, short below, checked every closed 2h bar. Non-repainting, fees modeled. 2-year test: +770 on 500 USDT notional, all 8 quarters positive at N=168 (neighbors +150–300 — expect those, not the headline); ~30% win rate, trend-style. | Testing only — NOT live. The only ETH system we've tested that made honest money. |
| `TV_strategy_ETH_SOL_PAIRS.pine` | **Pairs hedge** — market-neutral ETH/SOL spread mean-reversion (z-score of the log ratio; long the cheap leg, short the rich one, both 500 USDT). Non-repainting by construction. | Testing only — NOT live. ⚠ Honest 1-year sim: ~+35–110 USDT/yr best case, last 4 months negative in every config. |
| `ETH_Precision_Confluence.pine` | ETH 15m/30m confluence strategy (EMA stack + structure + momentum voting) built for TradingView experimentation. | Testing only — NOT live, not validated. |
| `ETH_HighWinRate_RSI2.pine` | **Evidence script, not a trading system.** RSI-2 dip-buy tuned for maximum win rate: ~69% WR and still LOSES after fees (PF 0.87 tune / measured numbers in the header). Kept to demonstrate that win rate ≠ profit. | Educational — do not trade. |
| `US_Stock_Precision_Trend.pine` | US stock daily strategy for TV testing: SPY regime filter, relative-strength weight, earnings block/exit, liquidity floor, RTH options. | Testing only — nothing in this repo trades stocks. |
| `TW_Stock_Pullback_Daily.pine` | **台股 TW50 pullback** — the exact rules behind the daily 🇹🇼 台股掃描 digest (`app/tw_stocks.py`): TAIEX regime (close > SMA100 & higher than 20 sessions ago), setup = close > SMA60 + low tags SMA20 + green close, next-open entry, SL 3×ATR14 / TP 5×ATR14 / 40-session time-out, full Taiwan costs (0.585% round-trip averaged per side). Apply to ONE TW50 symbol on the DAILY chart. | Watch-only — the digest alerts, nothing in this repo trades TW stocks. |

Deleted in the 2026-07-09 cleanup: `TV_strategy_TP.pine`,
`TV_strategy_V2.pine` (13-month replay: high win rate, net LOSS — the
break-even code lives on behind `STRATEGY3_BE_SYMBOLS`), and
`TV_strategy_ETH_SOL_30min.pine` (JustUncleL "Open Close Cross" — its 78%
win-rate backtest was lookahead repainting). All remain in git history
(`git log --diff-filter=D -- 'pine/*.pine'`).

## indicators/

| File | What it is |
|------|-----------|
| `TV.pine` | An **older copy** of "All-in-One ULTIMATE" (2026-07-03), kept because `app/strategy2_meter.py` cites it as the source of the `/strategy2` page. ⚠ The mirror is **out of sync**: the meter's docstring claims it is "weighted EXACTLY as the Pine script", but it uses 25/20 for the EMA factors where both `.pine` files use 20/15, and it omits the 10-weight **Volume** factor entirely. Same candles → up to **10 points** apart, and a **different bias verdict in 11%** of factor combinations. It also carries the same bearish tunnel asymmetry fixed in v2. Not yet reconciled. |
| `All-in-One_ULTIMATE.pine` | **v2 — the current one.** The newest, largest copy of "All-in-One ULTIMATE". v2 fixed measurement defects, not cosmetics: the MTF Bias panel **repainted** (it read the *forming* HTF bar while the code claimed "non-repainting" — a 1H row could show LONG for 50 minutes and leave no trace once the candle closed bearish; now reads the last **closed** HTF bar); **Clean Chart Mode silently changed the Confidence score** (it capped the trendline *engine*, so breaks that should have fired never did — it now caps *drawing* only, making the header's "calculations are unchanged" promise true); the **tunnel factor was bearish-biased** (a close *inside* the tunnel scored a full −1 while the mirror-image bull case scored 0 — worth 5 free bearish points); and the **score could not reach 0 or 100** (the trendline factor held 5 weight but spoke on ~1 bar in 1000, so 95 and 100 meant the same thing — factors with no opinion now abstain out of the denominator). Also: S/R zones logged phantom breaks when a zone re-anchored, decayed while price sat inside them, and let a fresh untested pivot evict your strongest level. Verified: when every factor speaks, the new score equals the old one exactly across all 2,187 factor combinations. |
| `Reactive_SR_Zones.pine` | Reactive support/resistance zone boxes. |
| `Whale_Flow_Macro.pine` | **v3** whale flow + macro panel (separate pane). Honest proxies only: intrabar volume delta (auto lower-TF), top-percentile whale bars ranked against **prior** bars, CVD divergence with ATR/delta significance floors, multi-venue OI read that surfaces **venue disagreement** instead of averaging it away, DXY link. v3 fixed four measurement defects: the DXY correlation counted closed-market bars as zero-return (fabricated observations — now gap-aware, with its own O(1) rolling Pearson over dollar-trading bars only), CVD silently blended true intrabar delta with the fallback estimate (now tracked, shown in the table, and drawn hollow when a pivot is estimated), divergences had no significance floor (a hair-higher high qualified), and venue splits were hidden by the average. |

## Testing notes

- **XAUT**: `strategies/TV_strategy_XAUT_30min.pine` on `BYBIT:XAUTUSDT.P`
  **30m** — defaults are already correct (1500 USDT order = live 30 × 50x).
- **ETH / SOL pairs**: chart symbol `BINANCE:ETHUSDT.P/BINANCE:SOLUSDT.P` on
  **2h**, defaults tuned (500 USDT per leg). The strategy trades the RATIO;
  a real "long spread" = long ETH + short SOL, 500 USDT each.
- **ETH momentum**: `strategies/TV_strategy_ETH_MOM_2h.pine` on ETHUSDT.P
  **2h**, defaults correct (500 USDT, N=168). ~30% win rate with occasional
  big trend rides is its nature — expect losing streaks.
- **HYPE**: `strategies/TV_strategy_HYPE_15min.pine` on HYPEUSDT **15m**,
  defaults correct (650 qty, 25x).
- **US stocks**: `strategies/US_Stock_Precision_Trend.pine` on any liquid
  US name, **1D**. Deep Backtesting mode needs the Generate/Update click
  before the tester shows trades.
