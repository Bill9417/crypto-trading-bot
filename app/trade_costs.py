"""
💸 What a recorded R is worth after the market takes its cut.

Every outcome book here scored trades as though the fill happened AT the level:
no fee, no slippage, no funding. That is not a rounding error at these stop
distances — at a 1.75% stop, a 0.11% round-trip fee has already spent 6% of the
R before price moves — and it is the single biggest reason a page can show
+0.29R while a live account shows something else.

The model is deliberately crude and stated rather than fitted:

    cost_R = 2 × (taker_fee + slippage) / stop_fraction

Two sides, each paying the taker fee and giving up slippage, expressed in units
of the trade's own risk. A wide stop absorbs the cost; a tight one is mostly
cost. That is why MIN_STOP_PCT exists in S4 and why it is not a tuning knob.

SLIPPAGE IS AN ASSUMPTION, NOT A MEASUREMENT. 10bp per side is the default
because it is roughly what a market order gives up on a liquid perp; it is
optimistic for a thin alt and pessimistic for BTC. It is applied uniformly and
reported, so the number on the page is honest about what it assumed instead of
quietly assuming zero — which is what "no cost model" actually means.
"""
import os

# Bybit taker, per side. Maker would be better and is not assumed: these books
# record market entries into a level, which is a taker fill.
TAKER_FEE = float(os.getenv("COST_TAKER_FEE", "0.00055"))
SLIPPAGE = float(os.getenv("COST_SLIPPAGE", "0.0010"))      # per side
# Below this the cost model stops being believable at all — a stop that tight
# is dominated by the spread, and no fill assumption rescues it. The stop is
# CLAMPED to it rather than the cost being waived: a 0.003% stop is the case
# where cost is largest, and returning 0.0 for it said "free" about the single
# worst trade the book can hold. Found 2026-08-21 while measuring the Vegas
# scan, where seven USDC/USDT rows with a 0.003% ATR carried a true cost of
# 100-240R each and scored as costless — dragging a -0.50R sample to -0.07R.
MIN_STOP_FRAC = float(os.getenv("COST_MIN_STOP_FRAC", "0.001"))


def cost_r(stop_pct: float) -> float:
    """Round-trip cost of one trade, in units of its own R.

    stop_pct is a PERCENT (1.75 means 1.75%), matching what the books store.
    Returns 0.0 when the stop is unknown — an unmeasurable cost must not be
    invented, and the caller records that it was not applied.
    """
    try:
        frac = float(stop_pct) / 100.0
    except (TypeError, ValueError):
        return 0.0
    if frac <= 0:
        # Not a stop distance at all. Unknown, like the branch above — and an
        # unmeasurable cost must not be invented in either direction.
        return 0.0
    # Clamped, never waived. See MIN_STOP_FRAC.
    return 2.0 * (TAKER_FEE + SLIPPAGE) / max(frac, MIN_STOP_FRAC)


def net_r(gross_r: float, stop_pct: float) -> tuple:
    """(net_r, cost_r) — the cost always subtracts from the RESULT.

    Not from the direction: a loss becomes a bigger loss and a win a smaller
    win, because you paid the spread either way. Halving the cost on losers
    (the tempting shortcut, since a stop-out only crosses once) would flatter
    every book by exactly the amount that matters.
    """
    c = cost_r(stop_pct)
    try:
        return round(float(gross_r) - c, 4), round(c, 4)
    except (TypeError, ValueError):
        return gross_r, 0.0


def describe() -> str:
    return (f"手續費 {TAKER_FEE*100:.3f}%/邊 + 滑價 {SLIPPAGE*1e4:.0f}bp/邊，"
            f"來回成本 ÷ 停損距離 = 每筆扣掉的 R")
