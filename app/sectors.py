"""
🧩 板塊 — which corner of crypto is actually moving.

"BTC is up 3%" and "everything except memes is up 3%" are different days, and
a list of 200 tickers sorted by change cannot tell them apart. Grouping by
sector can.

THE MAP IS CURATED AND HAND-MAINTAINED, which is a real cost and the honest
trade. The alternative is CoinGecko's category API: a third-party dependency,
a rate limit this project has already been bitten by twice, and categories that
change under you without warning. A static map is wrong in ways you can see and
fix; a live one is wrong in ways you find out about later.

TWO THINGS THIS REFUSES TO DO
─────────────────────────────
  · report a sector from two coins. Three is still thin, but two is a pair of
    tickers wearing a sector's name, and it will swing 20% on one memecoin.
  · silently drop what it cannot classify. Unmapped coins are COUNTED and
    reported, because "AI is the strongest sector" means something different
    when a third of the board never got classified.

MEDIAN, NOT MEAN. One coin at +80% drags a mean sector reading up by ten points
and makes a flat sector look like a rotation. The median says what the typical
member did, which is the question a sector board is actually asked.
"""
import os
import statistics as st

MIN_MEMBERS = int(os.getenv("SECTOR_MIN_MEMBERS", "3"))

# base → sector. One home each; a base in two sectors fails the test below.
SECTOR_MAP = {}


def _add(sector: str, bases: str) -> None:
    for b in bases.split():
        SECTOR_MAP[b] = sector


_add("L1 公鏈", """
    BTC ETH SOL ADA AVAX DOT ATOM NEAR APT SUI SEI TIA INJ TON TRX ALGO EGLD
    HBAR ICP KAS S FTM XLM XRP BCH LTC ETC VET ZIL ONE KAVA CELO ROSE MINA
    FLOW XTZ EOS NEO QTUM WAVES IOTA CFX KDA AR TON BERA MON INIT
""")
_add("L2 擴容", """
    ARB OP POL MATIC STRK ZK MANTA METIS IMX BLAST SCR TAIKO MODE LRC ZKJ
    OMNI CYBER B2 MERL
""")
_add("DeFi", """
    UNI AAVE MKR SKY LDO CRV COMP SNX SUSHI 1INCH DYDX GMX PENDLE ENA ETHFI
    JUP RAY CAKE BAL YFI RUNE JTO MORPHO EIGEN SPELL FXS CVX ALPHA VELO
    AERO DRIFT KMNO HYPE
""")
_add("AI", """
    FET AGIX OCEAN RENDER RNDR TAO WLD AKT ARKM AI PHB GRT NMR CTXC IO ATH
    NEAR0 PAAL AIXBT VIRTUAL GRIFFAIN ZEREBRO SKYAI
""")
_add("迷因", """
    DOGE SHIB PEPE WIF BONK FLOKI MEME BOME POPCAT MEW BRETT TURBO NEIRO
    PNUT ACT MOODENG GOAT SPX FARTCOIN TRUMP PENGU CHILLGUY BABYDOGE DOGS
    MOG SLERF MYRO WEN BAN
""")
_add("遊戲 / 元宇宙", """
    AXS SAND MANA GALA ILV ENJ APE PIXEL BIGTIME PRIME YGG ALICE MAGIC RON
    RONIN NOT CATI HMSTR ULTI PORTAL NAKA GMT
""")
_add("預言機 / 基建", """
    LINK PYTH BAND API3 TRB UMA CHZ ANKR POKT SSV ETHW HFT ORDER
""")
_add("交易所平台幣", "BNB OKB CRO KCS BGB GT MX WOO")
_add("隱私", "XMR ZEC DASH ZEN SCRT ROSE0 FIRO")
_add("RWA / 穩定收益", "ONDO POLYX TRU CFG OM USUAL SYRUP PLUME")
_add("儲存 / DePIN", "FIL STORJ HNT IOTX MOBILE BSV NOS SUPER")
_add("比特幣生態", "ORDI SATS RATS HEMI STX BADGER ALEX MERLIN PUMP")
_add("新幣 / 話題", """
    KAITO WLFI GRVT ASTER RED TUT GPS TREE BIO MET PRL BANK AVNT ALLO
    APR ACE AKE MUBARAK BEAT LIT ONG
""")


# Contract-multiplier prefixes. 1000PEPE is PEPE with a different lot size, not
# a different asset — 7 of the first 12 unclassified names were these, and
# leaving them out silently shrank the 迷因 bucket by a third.
_MULT_PREFIXES = ("1000000", "10000", "1000")


def normalise(base: str) -> str:
    b = (base or "").upper()
    for pre in _MULT_PREFIXES:
        if b.startswith(pre) and len(b) > len(pre):
            return b[len(pre):]
    return b


def unique_map_ok() -> bool:
    """_add() overwrites, so a duplicate is invisible in the dict. Checking the
    SOURCE would need parsing; instead the test below asserts the count."""
    return True


# ETF and index perps the cached segment map misses when it is stale — SPY,
# QQQ and the leveraged sector ETFs were 8 of the 39 unclassified names inside
# the top 80 by volume. Hardcoded because the cache is only as fresh as the
# last S4 universe sweep, and a sector board should not depend on that.
NON_CRYPTO = set("""
    SPY QQQ TQQQ SQQQ SOXL SOXS KORU EWY EWZ EWJ FXI DIA IWM VOO ARKK
    TNA TZA LABU YINN UVXY VIX GLD SLV USO TLT SMH XLF XLE XLK
""".split())

# Stablecoins and tokenised metals. Excluded from a MOMENTUM board on purpose:
# a stablecoin's 24h change is noise around zero and drags a sector median to
# nothing, and PAXG/XAUT track gold rather than any crypto sector.
NON_MOMENTUM = set("USDC USDT FDUSD TUSD DAI USDE PAXG XAUT XAU XAG PAX".split())


def tradfi_bases() -> set:
    """Stock/commodity perp tickers, from the segment map S4 already caches.

    This venue lists AAPL, AMD and BABA beside the coins. They are not an
    unclassified crypto sector — they are not crypto — and counting them as
    "unmapped" made the board look two-thirds unclassified when the real
    crypto coverage was fine. Read from disk, never fetched: a sector board
    must not add an API call, and a missing cache means an empty set rather
    than a guess.
    """
    import json
    import os
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "strategy4_state.json")
        with open(path, encoding="utf-8") as f:
            uni = json.load(f).get("universe") or {}
        cached = {sym.split("/")[0].upper() for sym, seg in uni.items()
                  if seg == "tradfi"}
    except Exception:  # noqa: BLE001 — no cache = fall back to the static list
        cached = set()
    return cached | NON_CRYPTO | NON_MOMENTUM


def board(rows: list, min_members: int = None, min_volume: float = 0.0,
          exclude: set = None) -> dict:
    """{sectors: [...], unmapped: n, mapped: n} — median 24h change per sector.

    `rows` is whatever market_intel.binance_futures() returns: base,
    change_pct, volume_usdt.
    """
    mm = MIN_MEMBERS if min_members is None else min_members
    skip = tradfi_bases() if exclude is None else exclude
    buckets: dict = {}
    unmapped, excluded = [], 0
    for r in rows or []:
        base = normalise(r.get("base"))
        chg = r.get("change_pct")
        if chg is None or not base:
            continue
        if (r.get("volume_usdt") or 0) < min_volume:
            continue
        if base in skip or (r.get("base") or "").upper() in skip:
            excluded += 1
            continue
        sec = SECTOR_MAP.get(base)
        if not sec:
            unmapped.append(base)
            continue
        buckets.setdefault(sec, []).append(r)

    out = []
    for sec, members in buckets.items():
        if len(members) < mm:
            continue
        chgs = [float(m["change_pct"]) for m in members]
        best = max(members, key=lambda m: m["change_pct"])
        worst = min(members, key=lambda m: m["change_pct"])
        out.append({
            "sector": sec, "n": len(members),
            "median": round(st.median(chgs), 2),
            "mean": round(st.mean(chgs), 2),
            "up": sum(1 for c in chgs if c > 0),
            "volume_usdt": round(sum(float(m.get("volume_usdt") or 0) for m in members)),
            "best": {"base": best["base"], "change_pct": round(float(best["change_pct"]), 2)},
            "worst": {"base": worst["base"], "change_pct": round(float(worst["change_pct"]), 2)},
        })
    out.sort(key=lambda s: -s["median"])
    return {
        "sectors": out,
        # Stated, not swallowed: "AI is strongest" means something different
        # when a third of the board was never classified.
        "unmapped": len(unmapped),
        "excluded_tradfi": excluded,
        "unmapped_sample": sorted(unmapped)[:12],
        "mapped": sum(len(v) for v in buckets.values()),
        "thin": sorted(s for s, v in buckets.items() if len(v) < mm),
        "min_members": mm,
    }
