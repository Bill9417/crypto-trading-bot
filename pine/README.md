# pine/ — TradingView strategy scripts

All the Pine v6 **strategy** scripts live here. (`TV.pine` in the repo root is
the original **indicator** — the source of the confluence signal — and stays
where it is.)

## Which script is which

| File | What it is | Status |
|------|-----------|--------|
| `TV_strategy_XAUT_30min.pine` | **"Vegas Flag Flip — XAUT 30m"** — the exact rules the live bot runs on gold (flag + Vegas entry, exit on opposite flag, emergency SL 1.5%, 50x, 3000 USDT order size). | **LIVE — XAUT trades this.** |
| `TV_strategy_ETH_SOL_30min.pine` | **"Open Close Cross R5.1"** (JustUncleL) — SMMA8 of open vs close series on 90m bars (3× the 30m chart); cross over → long, under → short, stop-and-reverse. ⚠ Repaints in TV with default settings — the bot trades the closed-bar version (see the header inside). | **LIVE — ETH + SOL trade this** (500 USDT each, 10x, 4% disaster stop). |
| `TV_strategy_V2.pine` | V1 + anti-chop break-even (stop jumps to entry+0.15% once +0.75% in profit). Tested on HYPE 15m. | Superseded — 13-month replay showed break-even gives a high win rate but a net LOSS (it strangles the big winners). |
| `TV_strategy_HYPE_V3.pine` | Trend-catcher rebuild for HYPE 15m: break-even OFF, ATR chop-gate 0.6, SL 2.5%, **25x max** (a 2.5% stop at 50x sits outside liquidation). Walk-forward tested +715 USDT / 13 months, ~31% win rate. | Being tested in TradingView — NOT live. |
| `TV_strategy_TP.pine` | Variant with real take-profit targets (full or partial TP brackets) for personal experimentation. | Testing only — NOT live. |

## Testing notes

- **XAUT**: load `TV_strategy_XAUT_30min.pine` on XAUTUSDT **30m** — defaults
  are already correct (3000 USDT order size = live sizing 60 margin × 50x).
- **ETH / SOL**: load `TV_strategy_ETH_SOL_30min.pine` on ETHUSDT / SOLUSDT
  **30m** with default inputs. For an honest backtest set
  "Delay Open/Close MA" = 1 (default settings repaint — see the file header).
- **HYPE V3**: HYPEUSDT **15m**, defaults are already correct (650 qty, 25x).
- The live bot reads signals from **Binance** charts and places orders on
  **Bybit** — small price differences vs a Bybit chart in TV are normal.

None of these files are read by the bot at runtime — the live rules are ported
into `app/strategy3_signal.py` (flag-flip) and `app/strategy3_occ.py` (OCC),
both driven by `app/strategy3_scanner.py`. Editing a `.pine` file changes
nothing live; porting a change to the bot is a separate, explicit step.
