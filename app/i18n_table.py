"""
🌐 中文 → English phrase table for the web UI.

WHY THIS EXISTS IN PYTHON. static/i18n.js already carried an English → 中文
dictionary, which was the whole job while the templates were written in
English. Most of this app is now authored in 中文, so that dictionary cannot
reach any of it — which is exactly why the pages read as a mixture rather than
as either language. This is the other direction.

Python rather than JS because the table needs to be MEASURED, not just
shipped: `coverage()` renders the real pages, collects every Chinese string
still on screen, and reports what is missing. "It looks mixed" is an opinion;
"41 strings on /market are untranslated" is a number, and it is the number
that makes finishing the job possible.

HOW IT IS APPLIED (see static/i18n.js): SUBSTRING replacement over text nodes,
longest key first. Not whole-string lookup — the rendered text is assembled at
runtime from fragments, '· 延遲 ' + 195 + 's', so the Chinese arrives glued to
numbers and an equality match finds none of it. Longest-first is what stops
'隧道' rewriting the inside of '隧道上方爆量'.

WHAT IS DELIBERATELY NOT TRANSLATED:
  · ticker symbols and trading vocabulary that is already English (LONG/SHORT,
    RSI, ATR, EMA, OI, R) — universal, and translating them would be worse
  · Taiwanese company names (中鋼, 南亞, 奇鋐, 智邦) — they are proper nouns,
    and an "English" version would be a different company to anyone checking
  · the 中文 label on the language toggle itself, which must stay 中文 in
    English mode or the button cannot say what it switches TO
"""

# ── card titles and page chrome ──────────────────────────────────────────────
TITLES = {
    "🏆 綜合前三名 · 買 / 賣": "🏆 Top 3 Combined · Buy / Sell",
    "🏆 綜合前三名 買/賣": "🏆 Top 3 Combined Buy/Sell",
    "👀 每日觀察清單": "👀 Daily Watchlist",
    "🌊 隧道翻多": "🌊 Tunnel Reclaim",
    "⚡ 隧道上方爆量": "⚡ Volume Thrust",
    "📦 供需區進場": "📦 Supply / Demand Zones",
    "🚀 壓力翻支撐": "🚀 Resistance→Support Flip",
    "🧩 板塊": "🧩 Sectors",
    "🐋 OI 異常": "🐋 OI Anomaly",
    "🐳 巨鯨持倉": "🐳 Whale Positioning",
    "💥 清算地圖": "💥 Liquidation Map",
    "📡 訊號脈動": "📡 Signal Pulse",
    "🎯 獵捕中": "🎯 Hunting",
    "🚀 動能雷達": "🚀 Pump Radar",
    "📊 S4 · 永續掃描": "📊 S4 · Perp Scan",
    "四大策略 · 規則與即時狀況": "Four strategies · rules and live status",
    "策略一 · 多重共振": "Strategy 1 · Multi-factor Confluence",
    "策略二 · 信心量表": "Strategy 2 · Confidence Meter",
    "策略三 · 旗標翻轉": "Strategy 3 · Flag Flip",
    "策略四 · 永續掃描": "Strategy 4 · Perp Scan",
    "← 返回 Dashboard": "← Back to Dashboard",
    "切換語言 / Switch language": "切換語言 / Switch language",
    "分析幣種… BTC / SOL / PENGU": "Analyse a coin… BTC / SOL / PENGU",
    "搜尋幣種代號，例如 BEAT、SOL、MYX…": "Search a ticker, e.g. BEAT, SOL, MYX…",
    "顯示/隱藏壓力支撐線": "Show/hide support & resistance",
}

# ── subtitles: what each card is ─────────────────────────────────────────────
SUBTITLES = {
    "EMA200 翻正 · 站回 Vegas 隧道 · 爆量":
        "EMA200 turns up · reclaims the Vegas tunnel · volume surge",
    "1 小時線；EMA200 由紅翻綠、收盤站回 EMA144/169 隧道上方，同時買方爆量":
        "1h chart; EMA200 turns from red to green, close reclaims the "
        "EMA144/169 tunnel, and buyers surge at the same time",
    "站穩隧道上方 · 一小時內買方爆量":
        "Holding above the tunnel · an hour of heavy buying",
    "15m 判隧道位置（免費），5m 確認量能（即時）—— 所以警報不會遲到":
        "15m reads the tunnel (free), 5m confirms the volume (live) — so the "
        "alert is not late",
    "壓力區賣、支撐區買 —— 收盤穿過的區間就不算了。":
        "Sell at supply, buy at demand — a zone price has closed through no "
        "longer counts.",
    "突破回踩不破 · 上方無壓": "Broke out, retested, held · nothing overhead",
    "壓力區被站上後回踩沒破 —— 舊壓力變新支撐":
        "Resistance was reclaimed and the retest held — old ceiling, new floor",
    "開倉／平倉的異常量 —— 跟每個幣自己過去": "Unusual open/close volume — against each coin's own past",
    "開倉／平倉的異常量 —— 跟每個幣自己過去 5 天比": "Unusual open/close volume — against each coin's own past 5 days",
    "看的是「典型成員」漲多少 —— 用中位數，一檔暴衝不會拉高整個板塊":
        "Shows what the TYPICAL member did — median, so one spike cannot lift "
        "a whole sector",
    "今天一直被點名的幣": "Coins flagged repeatedly today",
    "看的是「幾個獨立引擎點名、點了幾次」，不是分數 —— 只是觀察名單，沒有進出場":
        "Counts how many INDEPENDENT engines flagged it and how often — not a "
        "score. A watchlist, with no entries or exits",
    "排序看「幾個獨立引擎」，不是加權分數。這是觀察名單，不是進場訊號。":
        "Ranked by how many independent engines agree, not by a weighted "
        "score. A watchlist, not an entry signal.",
    "主動買賣 · 誰在追價": "Aggressive flow · who is lifting",
    "市場結構 · 未平倉": "Market structure · open interest",
    "多空比 · 散戶": "Long/short ratio · retail",
    "相對強弱 vs BTC": "Relative strength vs BTC",
    "近一小時價格方向": "Price direction, last hour",
    "近一日價格方向": "Price direction, last day",
}

# ── verdicts and the honesty lines ───────────────────────────────────────────
VERDICTS = {
    "只做觀察，不是進場訊號。": "Observation only — not an entry signal.",
    "只做觀察 —— 這個型態實測是負的。":
        "Observation only — this shape measures NEGATIVE.",
    "兩個區間都整段在零以下": "both intervals sit entirely below zero",
    "，不是「還沒證明」而是「證明了不賺」。所以不給進場價、停損或目標。":
        " — not \"unproven\" but \"proven not to pay\". So no entry, stop or "
        "target is given.",
    "] —— 區間整段在零以下。所以這張卡不給進場價、停損或目標。":
        "] — the interval is entirely below zero. So this card gives no entry, "
        "stop or target.",
    "平均為正、中位數為負 —— 一半以上的訊號之後是跌的。":
        "Mean positive, median negative — more than half of these are lower "
        "afterwards.",
    "平均為正、中位數為負 —— 代表": "Mean positive, median negative — meaning ",
    "超過一半的訊號隔天是下跌的": "more than half of the signals are down the next day",
    "，平均值是被少數大漲的那幾檔拉起來的。":
        ", and the average is carried by a handful of large winners.",
    "「要買方占多數」這個條件反而讓結果更差":
        "requiring the volume to be mostly BUYING makes the result WORSE",
    "R。你要求的那一關是最貴的一關 —— 條件留著（那是你要的型態），但它沒有在幫忙。":
        "R. The gate you asked for is the most expensive one — it stays "
        "(it is the shape you wanted), but it is not helping.",
    "量能那一關才是關鍵": "the volume gate is the one doing the work",
    "R —— 它是這個「型態」的一部分，不是有效的過濾條件。":
        "R — it is part of the SHAPE, not an effective filter.",
    "沒有穩定正期望值": "no stable positive expectancy",
    "樣本太少，還不能下任何結論": "too small a sample to conclude anything",
    "（分不出來）。": " (indistinguishable).",
    "信賴區間一樣包含": "the confidence interval still contains ",
    "R。前半段時間也測不出來 —— 只有一段行情，還不是結論。":
        "R. The early half of the window is inconclusive too — one regime, "
        "not a verdict.",
    "組在賠錢，所以「圖上看起來對」不等於有優勢。自己判斷，這裡不會自動下單。":
        "configurations lost money, so \"it looks right on the chart\" is not "
        "an edge. Judge for yourself; nothing here places an order.",
    "組在賠錢 —— 圖上看起來對，不等於有優勢。":
        "configurations lost money — looking right on a chart is not an edge.",
    "—— 形態偵測，不是已驗證的策略。":
        " — shape detection, not a validated strategy.",
    "是「掃描通知」，不是已驗證的策略。這些美股永續大多":
        "is a scan alert, not a validated strategy. Most of these equity perps ",
    "這是參考不是保證": "a reference, not a guarantee",
    "這是「持倉位置」不是進場訊號": "this is POSITIONING, not an entry signal",
    "這是模型推估，不是實際資料。": "This is a model estimate, not real data.",
    "— 這是持倉，不是預測；巨鯨也會錯。":
        "— positioning, not prediction; whales are wrong too.",
    "—— 本系統從未驗證過它能預測方向。":
        " — this system has never verified that it predicts direction.",
    "幾個獨立訊號同時指向同一邊": "how many independent signals point the same way",
    "幾個獨立引擎同時指向這一邊": "how many independent engines point this way",
    "資料僅供參考，不構成投資建議。台股掃描每個交易日更新一次，美股為前一交易日收盤。":
        "For reference only; not investment advice. The TW scan updates once "
        "per trading day; US figures are the previous session's close.",
}

# ── empty / error / loading states ───────────────────────────────────────────
STATES = {
    "這個功能還沒生效 —— 網站程式要重啟才會有（/restart）":
        "Not live yet — the web process needs a restart (/restart)",
    "這個功能還沒生效 —— 需要重啟（/restart）":
        "Not live yet — needs a restart (/restart)",
    "連不上伺服器：": "Cannot reach the server: ",
    "伺服器回應 HTTP": "Server returned HTTP ",
    "—— 稍後再試": " — try again shortly",
    "載入中…": "Loading…",
    "等待掃描…": "Waiting for the scan…",
    "掃描中": "Scanning",
    "待命中": "Idle",
    "排名載入失敗": "Ranking failed to load",
    "目前沒有訊號": "No signals right now",
    "目前沒有價格回到區間": "No coin has returned into a zone",
    "（掃描器剛重啟，等下一輪）": " (scanner just restarted — wait one sweep)",
    "目前沒有翻轉形態 ——": "No flip setups right now —",
    "四個條件（壓力區／站上／回踩不破／上方無壓）要全部成立。空的是常態。":
        "all four conditions (zone / reclaim / retest holds / clear overhead) "
        "must hold. Empty is the normal state.",
    "目前沒有異常堆積 ——": "No unusual build-up right now —",
    "空的是常態。": "Empty is the normal state.",
    "掃描器還沒跑第一輪（每小時一次）": "The scanner has not run yet (hourly)",
    "掃描器還沒跑第一輪": "The scanner has not run its first sweep",
    "小時沒有符合的幣 ——": "hours with no qualifying coin —",
    "小時沒有符合的幣。": "hours with no qualifying coin.",
    "這是一個「穿越」訊號，全市場平均一天約 9 檔。":
        "This is a CROSSING signal — about 9 a day across the whole market.",
    "還沒有結算完成的紀錄 ——": "No settled records yet —",
    "通知過的形態會在這裡累積成績。": "alerted setups accumulate their results here.",
    "尚未累積": "nothing yet",
    "筆已結算（還沒有）": "settled (none yet)",
    "（還沒有）": " (none yet)",
    "今天還沒有累積到 —— 掃描器剛開始跑":
        "Nothing has accumulated today — the scanner has just started",
    "追蹤中的": "Of the ",
    "隻巨鯨目前都沒有": " tracked whales, none currently hold ",
    "部位。": " .",
    "量能不明": "volume unknown",
    "等下次掃描": "next scan",
}

# ── common UI vocabulary ─────────────────────────────────────────────────────
WORDS = {
    "進場價": "Entry", "進場": "Entry", "停損": "Stop", "停利": "Target",
    "期望值": "Expectancy", "勝率": "Win rate", "樣本": "Sample",
    "信賴區間": "CI", "回測": "Backtest", "實測": "Measured",
    "多單": "long", "空單": "short", "做多": "LONG", "做空": "SHORT",
    "偏多": "bullish", "偏空": "bearish", "中性": "neutral",
    "順勢": "with trend", "逆勢": "against trend",
    "壓力": "resistance", "支撐": "support", "回踩": "retest",
    "上方無壓": "clear overhead", "上方壓力": "overhead resistance",
    "翻轉區": "flip zone", "次測試": "tests",
    "板塊": "sector", "全部": "All", "更新": "updated", "時間": "time",
    "價格": "price", "市值": "market cap", "市價": "market",
    "平倉": "close", "持有": "holding", "到期": "expiry", "收盤": "close",
    "判讀": "read", "投票": "votes", "對帳": "reconcile", "專區": "section",
    "同向": "aligned", "加密": "crypto", "台股": "TW stocks",
    "美股": "US stocks", "檔": " coins", "燈": " lights", "多": "long",
    "空": "short", "無": "none", "牛": "bull", "熊": "bear", "近": "last ",
    "上升": "rising", "顯示": "show", "隱藏": "hide",
    "上移": "move up", "下移": "move down",
    "掃描": "scan", "掃描於": "scanned ", "已扣成本": "net of costs",
    "手續費": "fee", "滑價": "slippage", "同時最多": "at most ",
    "沒空位而略過": " skipped for want of a slot",
    "資料": "data", "剛更新": "just now", "秒前": "s ago",
    "分前": "m ago", "小時前": "h ago", "天前": "d ago",
    "個引擎": " engines", "方向分歧": "engines disagree",
    "延遲": "delay ", "買方": "buyers ", "隧道上方": "above tunnel ",
    "這小時": "this hour ", "量": "vol ", "量能": "volume",
    "站回隧道": "reclaimed tunnel", "爆量": "volume surge",
    "翻正": "turned up", "完整型態": "full setup", "精選": "premium",
    "個小時": " hours", "筆": " trades", "每筆": "per trade",
    "觸發小時": "firing hour", "每個觸發小時": "per firing hour",
}


# Keys whose English form legitimately keeps Chinese in it — the language
# toggle's own label is the only one, and it must stay 中文 so the button can
# name what it switches TO.
KEEP_AS_IS = {"切換語言 / Switch language"}


# ── short fragments that ARE safe to replace inside a larger string ──────────
# The general rule (see static/i18n.js) is that a short phrase must BE the
# whole text node, because Chinese has no word boundaries and a 2-character
# entry otherwise rewrites the inside of longer words — 多→"long" turned
# 新多進場 into "新longEntry".
#
# But some short fragments only ever exist glued to a number: "資料 4分前" is
# assembled at runtime and can never be a whole-node match for any fixed key.
# Those need an explicit opt-in, and the opt-in is CHECKED: test_i18n asserts
# that no entry here occurs inside another key of the table, which is exactly
# the 專區-inside-幣種專區 mistake that made this rule necessary.
SUB_OK = {
    "資料 ": "data ",
    "分鐘前": " min ago", "小時前": "h ago",
    "分前": "m ago", "秒前": "s ago", "天前": "d ago",
    "剛更新": "just now",
    "檔加密永續": " crypto perps",
    " 次測試": " tests", "第 ": "#", " 個小時": " hours",
    "掃描 ": "scanned ", "掃描於 ": "scanned ",
    "延遲 ": "delay ", "買方 ": "buyers ", "這小時 ": "this hour ",
    "翻轉區 ": "flip zone ",
    "同時最多 ": "at most ", "已扣成本：": "net of costs: ",
    "中位 ": "median ", "區間 ": "zone ", "多空比 ": "L/S ratio ", "到期 ": "expired ",
    "上一輪": "last sweep", "全市場平均一天約 ": "about ",
    "停損 ": "stop ", "停利 ": "target ",
    # ── repeated tile patterns (verified by unsafe_substrings) ──────────────
    "· 損 ": "· stop ", "· 前": "· top ", "· 最強 ": "· best ",
    # Longer, unambiguous forms of the ones the corpus check rejected.
    "拆開來看：": "broken down: ", "只有回到隧道上方 ": "reclaim alone ",
    "上方有壓 ": "ceiling nearby ", " · 順勢": " · with trend",
    " · 逆勢": " · against trend", "未平倉異常堆積": "OI build-up",
    "·量 ": "·vol ",
    "檔（依成交量排序，回測用的也是流動性前 ": " coins (by volume; the backtest used the top ",
    " 名）": ")",
    "檔（美股永續已排除 ": " coins (equity perps excluded: ",
    " 檔不列入。": " left out.",
    "，其中 ": ", of which ",
    " 有分類；未分類 ": " are classified; unclassified ",
    "取成交額前 ": "Top ", 
    "收紅翻綠 ": "up bar ", "爆量 ": "surge ", "過波動門檻 ": "ATR gate ",
    "每小時掃一次（": "scanned hourly (", " 收線才會變）": " closes only)",
    "隧道 + EMA": "tunnel + EMA", "隧道 EMA": "tunnel EMA",
    "量能看最近 ": "volume over the last ",
    " 分鐘 vs 自己過去 ": " min vs its own past ",
    " 小時的平均（": "h average (", " 即時確認）": " live confirm)",
    "同一檔 ": "same coin ",
    " 小時內只報一次": "h cooldown",
    " 確認 ": " confirmed ", "檔（還有 ": " coins (another ",
    " 檔站上隧道但配額用完，沒確認）": " above the tunnel but out of budget)",
    "警報延遲中位數 ": "median alert delay ", "（最久 ": " (max ",
    "，一根 ": ", one ", " = 300s）": " = 300s)",
    "＋EMA200 翻正 ": "+EMA200 turn ", "＋量能爆發 ": "+volume surge ",
    "三個條件全滿足 ": "all three ",
    "翻正 ": "turn ", "翻正只動了 ": "turn moved it only ",
    "，窗口從 ": ", widening the window from ",
    " 根拉到 ": " bars to ", " 根結果只差 ": " changed it by ",
    "拿掉最賺的 ": "dropping the best ", " 檔後整體只剩 ": " leaves ",
    "（CI含 0）；順勢那半邊還有 ": " (CI contains 0); with-trend still ",
    "每小時只算一次的話，「": "counted once per hour, \u300c",
    " 檔以上同時觸發」從 ": "+ firing together\u300d goes from ",
    " 變成 ": " to ", "那個「優勢」是 ": "that \u300cedge\u300d is ",
    " 個時刻被算成 ": " market moments counted ", " 次": "×",
    "累積 · ": "accumulated · ",
    " 名不是分界，只是列表到此為止": " is not a boundary, just where the list stops",
    "同樣 ": "the same ",
    "上線後實際追蹤：": "live record: ",
    "（每筆都等滿 ": " (each waits the full ",
    " 小時才結算，用的是下一根 K 的開盤價）": "h before settling, entered at the next bar's open)",
    "回測後續漲跌（": "backtest forward returns (",
    "回測後續漲跌：": "backtest forward returns: ",
    " 平均 ": " mean ", "中位數 ": "median ",
    " 檔永續 × ": " perps × ", " 根 ": " bars of ",
    "扣掉手續費與滑價後，": "net of fees and slippage, ",
    "只測過 ": "measured only over ",
    "手續費 ": "fee ", "滑價 ": "slippage ",
    "，來回成本 ÷ 停損距離 = 每筆扣掉的 R｜": ", round-trip ÷ stop distance = R deducted per trade | ",
    "獲利集中在 ": "profit concentrated in ",
    " 檔幣（占 ": " coins (",
    "），拿掉最好的一檔剩 ": "); dropping the best leaves ",
    "修正偵測器後獨立重測 ": "independent re-test after fixing the detector: ",
    "價格走過的價位視為已清算並清空。強度已依目前 OI 名目縮放。":
        "levels price has traded through are treated as liquidated and cleared. "
        "Intensity is scaled to current OI notional.",
    "低 → 高預估清算量 · ": "low → high estimated liquidation volume · ",
    " 根（近 ": " bars (last ", " 天） · 依「未平倉量增加」加權 · 目前 OI $":
        "d) · weighted by OI increase · current OI $",
    " 檔也符合 —— 這裡只列量能最大的 ":
        " also qualify — only the strongest ",
    # 檔 is a COUNTER WORD ("items"), and it only means "shown" in the one
    # phrase above. Mapping the bare counter to " shown" turned "scanned 527
    # coins" into "scanned 527 shown" and "3 檔" into "3 shown" everywhere it
    # appeared. The general form gets the general meaning.
    " 檔": " coins",
    "滑過看進場價與損益": "hover for entry and P&L",
    "都在區間 · 賽克斯": "both in the zone · Sykes",
    "= 5 分線也判斷在同一個區間": "= the 5m read agrees on the same zone",
    "儲存後，所有一般使用者也會看到這個版面":
        "once saved, every ordinary user sees this layout too",
    "編輯版面": "Edit layout", "儲存版面": "Save layout", "回預設": "Reset",
    "幣種分析": "Coin analysis", "首頁": "Home", "新多單進場": "new LONG entry", "新空單進場": "new SHORT entry",
    "小型 $": "small $", "上方最近壓力還有 ": "nearest resistance above is ",
    "幣種專區": "Coins", "訊號專區": "Signals", "供需區": "Zones",
    "主流幣": "Majors", "綜合前三": "Top 3", "每日觀察": "Watchlist",
    "壓力翻支撐": "Flip", "OI 異常": "OI anomaly",
    "期望值 ": "expectancy ", "勝率 ": "win rate ",
    # Re-added as LONGER keys after the corpus check rejected the short forms.
    # " 檔" alone rewrote " 檔站上隧道但配額用完"; the qualified versions cannot.
    " 檔沒列出": " not listed", " 檔加密永續": " crypto perps",
    " 個引擎的還有": " engines, another ",
    "今天追蹤 ": "tracking ", "上線前回測 ": "pre-launch backtest ",
    "站上隧道後用 ": "after clearing the tunnel, ",
}


def unsafe_substrings(corpus) -> list:
    """SUB_OK entries that would rewrite the inside of a longer Chinese word.

    Chinese has no word boundaries, so this cannot be eyeballed — I added
    訊號 ("signal") to SUB_OK and it turned 訊號專區 into "signals專區" within
    minutes of fixing the identical bug for 多 and 平倉.

    The rule: for every occurrence of a key in the corpus, take the maximal
    contiguous run of Chinese characters around it. If that run is longer than
    the key, the key is a word FRAGMENT there and replacing it corrupts the
    word. `corpus` is every Chinese string the real pages actually render.
    """
    def cjk(ch):
        return "\u4e00" <= ch <= "\u9fff"

    full = table()
    # The applier replaces LONGEST FIRST, so by the time a short key is tried
    # every longer key has already consumed its own text. A checker that does
    # not model that reports false alarms — it flagged '門檻 ' for sitting
    # inside '過波動門檻 ' when '過波動門檻 ' is itself in the table and is
    # applied first. Simulate the real order, then ask what is left.
    order = sorted(full, key=len, reverse=True)

    bad = []
    for key in SUB_OK:
        if not any(cjk(c) for c in key):
            continue
        grow_left, grow_right = cjk(key[0]), cjk(key[-1])
        if not (grow_left or grow_right):
            continue
        longer = [k for k in order if len(k) > len(key)]
        for raw in corpus:
            text = raw
            for k in longer:                       # everything that wins first
                if k in text:
                    text = text.replace(k, "\x00")
            at = text.find(key)
            hit = None
            while at >= 0:
                lo, hi = at, at + len(key)
                if grow_left:
                    while lo > 0 and cjk(text[lo - 1]):
                        lo -= 1
                if grow_right:
                    while hi < len(text) and cjk(text[hi]):
                        hi += 1
                if text[lo:hi] != key:
                    hit = (key, text[lo:hi], raw[:60])
                    break
                at = text.find(key, at + 1)
            if hit:
                bad.append(hit)
                break
    return bad


# ── the Strategies hub (2026-09-27 redesign: rules · record · live) ─────────
HUB = {
    "四大策略 · 規則、實測紀錄與即時狀況": "Four strategies · rules, measured record, live status",
    "每一套策略都有三張卡：": "Every strategy has three cards: ",
    "它的規則": "its rules", "它量出來的紀錄": "the record it measured", "它現在在做什麼": "what it is doing now",
    "紀錄是這個專案自己的實測 —— 勝率不等於優勢，只有期望值和它的信賴區間算數。":
        "The record is this project's own measurement — win rate is not edge; only expectancy and its interval count.",
    "完整記分板 Reality Check →": "Full scoreboard · Reality Check →",
    "實測紀錄": " Measured record", "已淘汰的策略": "Retired strategies",
    "淘汰原因附上數字": "each with the number that retired it",
    "這裡是被自己的紀錄殺掉的東西。留著是因為「為什麼不做」和「做什麼」一樣重要 —— 每一條都附上把它淘汰的那個數字。":
        "What its own record killed. Kept because \"why not\" matters as much as \"what\" — every entry carries the number that retired it.",
    "僅發訊號 · 無下單路徑": "Alerts only · no order path",
    "Bybit 鏡單": "Bybit mirror", "只發訊號": "alerts only",
    "沒有下單路徑。": "No order path. ",
    "原本可選的實盤下單層已於 2026-09-27 移除 —— 右邊的紀錄就是原因。":
        "The opt-in live order layer was removed 2026-09-27 — the record beside this is the reason.",
    "波動天花板": "Volatility ceiling ", "已啟用": "enabled",
    "幣本身的 ATR 超過價格": "the symbol's own ATR above ", "就不進場。": " of price blocks the entry.",
    "（預設）。這是實驗室找到的唯一一致改善，等右邊的前向紀錄證明再開。":
        " (default). The one consistent improvement the labs found — switched on once the forward record beside this earns it.",
    "每日斷路器：24 小時內虧損": "Daily circuit breaker: ", "筆或淨虧超過上限就停止新單，直到管理員 /resume。":
        " losing trades in 24h or a net loss past the cap halts new entries until an admin sends /resume.",
    "每筆 · 360 天樣本外驗證": "per trade · 360-day out-of-sample",
    "沒有可持續的優勢 —— 兩次樣本外驗證都是淨負，只有最近兩段是正的":
        "no durable edge — net negative out of sample both times, positive only in the two most recent folds",
    "段為正（最近兩段）；": " folds positive (the two most recent); ",
    "修正倖存者偏差後": "survivorship-corrected ", "重跑": " rerun ",
    "前向紙上測試": "Forward paper test", "尚無平倉": "no closes yet", "賠率": "payoff",
    "現行規則": "live rules", "只做多": "longs only", "低波動濾網": "low-volatility filter",
    "低波動 + 單一 3R 目標": "low-volatility + single 3R target",
    "實盤（帳本）": "Live (ledger)", "手動平倉比例": "Manual-close share",
    "低波動變體來自 s1_regime_lab（期望值隨 ATR 天花板收緊而": "The low-vol variant comes from s1_regime_lab (expectancy rises ",
    "單調": "monotonically", "上升，31 檔、多空皆轉正）；": " as the ATR ceiling tightens; 31 symbols, both sides turn positive); ",
    "出場變體來自 s1_exit_lab（S1 的贏單被 1.5R 封頂、輸單卻要付 1.07R）。":
        "the exit variant from s1_exit_lab (S1's winners are capped at 1.5R while a loser costs 1.07R). ",
    "前向紀錄已有": "The forward record has ", "筆且為正 —— 值得考慮把": " closes and is positive — worth considering switching on ",
    "打開。": ".", "在前向紀錄累積到 30 筆以上且為正之前，": "Until the forward record reaches 30+ closes and stays positive, ",
    "維持關閉。": " stays off.", "樣本外驗證的細節見": " Out-of-sample details on ",
    "未證明有優勢 · 樣本外淨負": "unproven edge · negative out of sample",
    "筆訊號 · 抱到底規則": " signals · hold rule", "信賴區間": "interval ", "（含 0）": " (contains 0)", "（不含 0）": " (clear of 0)",
    "淨損益": "Net", "獲利因子": "Profit factor", "分群 · 同一條出場規則": "Cohorts · same exit rule",
    "六種出場規則全部為負（抱到底、保本、減半、TP1 全出、移動停利…）—— 這就是 S2 沒有下單路徑的原因。":
        "All six exit rules are negative (hold, breakeven, half off, all at TP1, trailing…) — which is why S2 has no order path. ",
    "「很可能」是有意義的：舊帳只存總和，區間是最窄的可能值。": "\"Probably\" is doing real work: the old tally stores totals only, so the range is the narrowest possible. ",
    "看完整記分板 →": "Full scoreboard →",
    "還沒有訊號完成 48 小時的評估視窗。每一筆訊號都會在 48 小時後對真實 K 線重播、記分。":
        "No signal has completed its 48-hour window yet. Every signal is replayed against real candles 48h later and scored.",
    "尚無紀錄": "no record yet", "無紀錄": "no record", "確定為負": "negative", "很可能為負": "probably negative",
    "看不出優勢": "no edge", "樣本太少": "too few", "確定為正": "positive", "可能為正，未證明": "probably positive, unproven",
    "紀錄暫時無法讀取。": "The record is temporarily unavailable.",
    "S3 的損益在 Bybit 的": "S3's P&L lives in Bybit's ", "已平倉紀錄": "closed-P&L record",
    "裡，由帳本按「誰開的倉」歸屬（S1 鏡單、S3、手動共用一個子帳戶，交易所不知道是誰下的）。":
        ", attributed by the ledger by who opened the position (the S1 mirror, S3 and manual trades share one sub-account; the exchange does not know who ordered).",
    "帳本紀錄": "Ledger rows", "持有中": "Open", "正在讀 Bybit 已平倉紀錄…": "Reading Bybit closed P&L…",
    "實盤損益只有管理員看得到。": "Live P&L is visible to the admin only.",
    "每週一的日報用真實獲利因子判定每個引擎：10 筆以上且 PF &lt; 1 會印「不值得它承擔的風險」。手動平倉比例高時，這個數字量的是人、不是策略。":
        "Every Monday the daily report judges each engine by its real profit factor: 10+ trades with PF &lt; 1 prints \"not paying for its risk\". A high manual-close share means this number measures the operator, not the strategy.",
    "看實盤紀錄": "see live record", "實盤紀錄無法讀取": "live record unavailable", "Bybit 紀錄暫時無法讀取": "Bybit record temporarily unavailable",
    "Bybit 實盤 · 依帳本歸屬": "Bybit live · attributed by the ledger", "S3 翻轉引擎": "S3 flip engine", "S1 鏡單": "S1 mirror",
    "S4 掃描": "S4 scan", "尚無已平倉": "no closes yet", "不值得它的風險": "not paying for its risk",
    "Bybit 實盤 · 鏡單（帳本歸屬）": "Bybit live · mirror (ledger-attributed)",
    "重新開始計算（改記淨 R、扣手續費與滑價、且只在有槽位時才收訊號）。舊帳是毛的、不可比較，所以不顯示。":
        " the book restarted (net R, fees and slippage deducted, signals accepted only when a slot was free). The old book was gross and is not comparable, so it is not shown.",
    "尚無新紀錄": "no new record yet", "筆已結算 · 淨 R": " settled · net R", "樣本": "Sample",
    "之前的版本量了 90 筆：−0.022R ± 0.282，區間含 0。做空那一側從沒被量過，是應要求加上去的。":
        "The previous version measured 90 trades: −0.022R ± 0.282, interval containing 0. The short side has never been measured; it was added on request. ",
    "每一筆訊號和它之後發生的事都記在": "Every signal and what followed it is recorded on the ", "S4 頁": "S4 page",
    "未證明": "unproven",
    # ── the coin page's measured context (2026-09-27) ──
    "🎯 精準度 · 品質旗標與 ⭐ 精選門檻": "🎯 Precision · quality flags and the ⭐ premium gate",
    "項因子的方向統計 —— 不是加權分數，也不是預測": " factors, counted by direction — not a weighted score, not a prediction",
    "大盤 · BTC 趨勢": "Market · BTC regime", "時框一致性 · 4H/1H": "Timeframe agreement · 4H/1H",
    "趨勢強度 · ADX": "Trend strength · ADX", "波動 · 1h ATR / 價格": "Volatility · 1h ATR / price",
    "量能 · 最近一小時 vs 24h": "Volume · last hour vs 24h", "S4 五道關卡": "S4 five gates",
    "⭐ 精選門檻": "⭐ Premium gate", "與 BTC 同向": "aligned with BTC", "三關全過": "all three gates pass",
    "沒過的關：": "failed gates: ", "不到 ⭐ 精選等級": "below the ⭐ premium tier",
    "需求區內": "in demand zone", "供給區內": "in supply zone", "區間外": "outside zones",
    "順勢做多。": "trade with it, long. ", "順勢做空。": "trade with it, short. ",
    "實測：與大盤同向的訊號 57% 先到 TP1，逆勢只有 41%": "Measured: signals aligned with it reach TP1 first 57% of the time, counter-regime 41%",
    "BTC 盤整 —— 沒有方向濾網；⭐ 精選要求與大盤同向，所以此刻不會發": "BTC is ranging — no directional filter; the ⭐ tier requires alignment, so nothing fires now",
    "K 線不足以算": "not enough candles for ", "時框不一致": "timeframes disagree",
    "有趨勢": " trending", "盤整；共振訊號在這裡最常失準": "ranging; confluence signals fail most often here",
    "本專案 60 天回放量到最好的組合：58.7% 先到 TP1（原始訊號約 49%）": "the best combination in this project's 60-day replay: 58.7% first to TP1 (raw feed ~49%)",
    "前四關通過 · 未平倉未查": "first four gates passed · OI not checked", "通過 · ": "passed · ", "卡在 · ": "stopped at · ",
    "價格正在新鮮的需求區內": "price is inside a fresh demand zone", "價格正在新鮮的供給區內": "price is inside a fresh supply zone",
    "不在任何新鮮區間內": "outside every fresh zone", "附近沒有新鮮的供需區": "no fresh supply/demand zone nearby",
    "沒有量能確認": "no volume confirmation",
}


# ── 🐋 OI 異常 / 📦 供需區進場 cards (2026-09-27) ───────────────────────────
CARDS_0927 = {
    # Whole-node labels (the markup wraps each in its own element).
    "建倉": "Builds", "平倉": "Unwinds", "進行中": "Open", "順勢 + 5m✓": "With trend + 5m✓",
    "已結算": "settled", "未通知": "not alerted", "只列": "showing last", "小時內": "hours",
    "檔": "coins", "進": "in", "價": "px", "小型": "small", "中型": "mid", "大型": "large", "判讀": "Read", "切面": "Cut", "整體": "All", "反向": "reversal", "延續": "continuation",
    "h 後": "h later", "4h 後": "after 4h", "📐 回放": "📐 Replay", "📓 實盤累積": "📓 Live record",
    "事後驗證": "Scored afterwards", "累積": "So far", "筆結案、": "closed, ", "筆等待中；不到": "pending; no verdict under",
    "筆不下結論。": "events.", "反向率": "reversal rate", "延續率": "continuation rate",
    "空單回補": "short covering", "多單平倉": "long liquidation",
    "5m 也在區間": "5m also in zone", "去掉最賺 5 檔": "Minus the best 5 coins",
    "多半延續": "mostly continued", "多半反轉": "mostly reversed",
    # Longer phrases, substituted anywhere.
    "這一類目前沒有 —— 換個篩選看看。": "None of this kind right now — try another filter.",
    "這個篩選目前沒有 —— 其他 ": "Nothing under this filter — the other ",
    " 筆在「全部」。": " are under \u300cAll\u300d.",
    " —— 每筆上板的堆積都拿真實 K 線在 1h / 4h / 24h 後對答案。建倉看":
        " — every pile-up on the board is checked against real candles 1h / 4h / 24h later. Builds show the ",
    "（價格轉頭 = 擁擠的一方被軋），平倉看": " (price turned = the crowded side got squeezed); unwinds show the ",
    "還沒有結案的樣本 —— 第一批 1h 結果會在下一次堆積後一小時出現。":
        "No scored events yet — the first 1h results appear an hour after the next pile-up.",
    "建倉後 24h 內對堆積方向最大反向幅度中位數 ": "Median largest move against the crowd within 24h of a build: ",
    "—— 這就是「燃料」實際燒了多少。": "— how much of the \u201cfuel\u201d actually burned.",
    "樣本不足（": "too few events (",
    "反向擠壓成立：多數時候價格轉頭": "squeeze confirmed: price usually turned",
    "沒有擠壓：多數時候順著堆積方向走": "no squeeze: price usually kept going with the crowd",
    "分不出來（區間含 50%）": "cannot tell (the interval contains 50%)",
    "罕見度：跟自己近 5 天比的排名": "rarity: rank against its own last 5 days",
    "（信賴區間含 0）；順勢那半邊還有 ": " (CI contains 0); the with-trend half still ",
    "。前半段時間也測不出來 —— 只有一段行情，還不是結論。":
        ". The first half of the window could not show it either — one regime, not a conclusion.",
}


def table() -> dict:
    """The whole ZH → EN map."""
    out = {}
    for part in (TITLES, SUBTITLES, VERDICTS, STATES, WORDS, HUB, CARDS_0927, SUB_OK):
        out.update(part)
    return out


def as_js() -> str:
    """Emit static/i18n_zh_en.js. Generated, not hand-maintained, so the
    Python table stays the single source of truth that coverage() measures."""
    import json
    rows = sorted(table().items(), key=lambda kv: (-len(kv[0]), kv[0]))
    body = ",\n".join(f"  {json.dumps(k, ensure_ascii=False)}: "
                      f"{json.dumps(v, ensure_ascii=False)}" for k, v in rows)
    sub_ok = json.dumps(sorted(SUB_OK), ensure_ascii=False)
    return ("/* GENERATED from i18n_table.py — do not edit by hand.\n"
            " * 中文 → English for the web UI. Applied by static/i18n.js as a\n"
            " * longest-first substring replacement over text nodes.\n"
            " * WOLF_ZH_EN_SUB lists the SHORT keys that are safe to replace\n"
            " * inside a larger string; everything else short must be the\n"
            " * whole node. See the SUB_OK note in i18n_table.py.\n"
            " */\n"
            "window.WOLF_ZH_EN = {\n" + body + "\n};\n"
            "window.WOLF_ZH_EN_SUB = " + sub_ok + ";\n")


if __name__ == "__main__":
    import os
    dst = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "static", "i18n_zh_en.js")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(as_js())
    print(f"wrote {dst} — {len(table())} phrases")
