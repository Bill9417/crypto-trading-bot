/* ============================================================================
 * Wolf Scanner — lightweight Traditional-Chinese (繁體中文) UI toggle.
 *
 * 100% client-side. Touches NO backend, trading, or order logic — it only
 * swaps visible English UI text for 繁體中文 in the browser. The chosen
 * language is saved in localStorage and re-applied on every page.
 *
 * Scope (per request): navigation + Dashboard chrome. Trading symbols,
 * LONG/SHORT, RSI, SL/TP and live numbers stay in English on purpose
 * (universal trading vocabulary). To cover more pages later, just add the
 * English → 中文 pairs to DICT below.
 * ========================================================================== */
(function () {
  "use strict";

  // English (exact visible text) → 繁體中文
  var DICT = {
    // ── Navigation (quiet-luxury nav 2026-07-05 — plain labels, no emoji;
    //    CSS uppercases them, the DOM text stays as written here) ──────────
    "Dashboard": "儀表板",
    "Account": "帳戶",
    "Bybit": "Bybit",
    "Performance": "績效",
    "Funnel": "漏斗",
    "Market": "市場",
    "Stocks": "股票",
    "Strategy 2": "策略二",
    "Admin": "管理",
    "Health": "系統健康",
    "Tools": "工具",
    "Strategies": "策略",
    "Universe": "宇宙",

    // ── Sidebar nav + Market Pulse board (2026-08-09 redesign) ────────────
    "Overview": "總覽",
    "Market Pulse": "市場脈動",
    "3D Universe": "3D 星圖",
    "Signal Funnel": "訊號漏斗",
    "S4 Radar": "S4 雷達",
    "Calculators": "計算工具",
    "TW Stocks": "台股",
    "US Stocks": "美股",
    "Market Board": "盤勢看板",
    "Copy Trading": "跟單",
    "Users": "使用者",
    "Strategy": "策略",
    "Stocks": "股市",
    "Logout": "登出",
    "Market Overview": "市場總覽",
    "BTC · 30-day trend": "BTC · 30 日走勢",
    "Money & Mood": "資金與情緒",
    "who is paying to hold their side": "誰在付錢持有部位",
    "Breadth": "市場廣度",
    "Momentum": "動能",
    "Data confidence": "資料可信度",
    "Market Breadth": "市場廣度",
    "Sector Rotation": "產業輪動",
    "median 24h": "24 小時中位數",
    "Top Gainers": "漲幅榜",
    "Top Losers": "跌幅榜",
    "Funding (avg)": "平均資金費率",
    "Fear & Greed": "恐懼貪婪指數",
    "BTC dominance": "BTC 市佔率",
    "Total market cap": "總市值",
    "Scanner regime": "掃描器判定",
    "gates every trade": "決定每一筆交易能不能開",
    "Up share": "上漲佔比",
    "Up : down": "漲跌比",
    "Conviction": "信心度",
    "All Conviction": "全部信心度",
    "Rules": "規則",
    "contracts": "合約",
    "detail →": "詳細 →",

    // ── Strategy 2 — confidence meter ─────────────────────────
    "Live mirror of the TradingView indicator": "TradingView 指標的即時鏡像",
    "Symbol": "交易對",
    "Score (0–100)": "分數 (0–100)",
    "Neutral": "中性",
    "LONG bias": "偏多",
    "SHORT bias": "偏空",
    "Factors": "因子",
    "EMA Stack": "EMA 排列",
    "Price vs EMA200": "價格 vs EMA200",
    "SMC Structure": "SMC 結構",
    "Vegas Slope": "Vegas 斜率",
    "Tunnel Position": "通道位置",
    "Trendline Break": "趨勢線突破",
    "Volume Bias": "成交量偏向",
    "abstain": "無意見",
    "Live chart": "即時圖表",
    "Recent score": "近期分數",

    // ── Top bar / header ───────────────────────────────────────
    "BOT IDLE": "機器人閒置",
    "HUNTING...": "搜尋中...",
    "Rules": "規則",
    "↻ Refresh": "↻ 重新整理",
    "Logout": "登出",
    "Save": "儲存",
    "⚙ Live strategy": "⚙ 實盤策略",
    "Trading now:": "目前交易：",

    // ── Circuit-breaker banner ────────────────────────────────
    "TRADING HALTED TODAY": "今日已停止交易",
    "Trading Allowed": "允許交易",
    "Losses": "虧損次數",
    "Loss DD": "虧損回撤",

    // ── Live account strip ────────────────────────────────────
    "💰 LIVE ACCOUNT": "💰 實盤帳戶",
    "Equity": "權益",
    "Available": "可用",
    "Unrealized": "未實現",
    "Open positions": "持倉數",
    "Realized trend": "已實現趨勢",

    // ── Stats strip ───────────────────────────────────────────
    "Hunting Signals": "搜尋訊號",
    "Long / Short": "多 / 空",
    "Win Rate (4+ Lights)": "勝率（4+ 燈）",
    "Total Strategy PnL": "策略總損益",
    "🔻 Funnel · Queued": "🔻 漏斗 · 排隊中",

    // ── Top entry candidates ──────────────────────────────────
    "🎯 Top Entry Candidates": "🎯 最佳進場候選",

    // ── 🌌 Market Cloud (3D hero) ──────────────────────────────
    "Armed": "已掛單",
    "size = conviction": "點大小 = 信心度",
    "Drag to rotate · hover a body for detail · click to open its chart":
      "拖曳旋轉 · 指向任一點看資訊 · 點擊開圖表",
    "Hunting signals": "搜尋訊號",
    "Long / Short": "多 / 空",
    "Win rate · 4+ lights": "勝率 · 4+ 燈",
    "Strategy P&L": "策略損益",
    "🔻 Funnel · queued": "🔻 漏斗 · 排隊中",
    "Market": "市場",

    // ── Section titles & analytics ────────────────────────────
    "Signal Pulse": "訊號脈動",
    "Strategy Signals": "策略訊號",
    "Signal Bias": "訊號偏向",
    "Active Trades": "進行中交易",
    "Queued Plans": "排隊計畫",
    "Recent Closed": "近期平倉",
    "Rejected Setups": "遭拒設定",
    "Recent Win Rate": "近期勝率",
    "Conviction": "信心度",
    "High Conviction": "高信心度",
    "Live Price": "即時價格",
    "Entry": "進場",
    "Exit Price": "出場價",
    "SMC Zone": "SMC 區域",
    "SMC Confluence": "SMC 匯合",
    "EMA50 Trend": "EMA50 趨勢",
    "Average Win": "平均獲利",
    "Average Loss": "平均虧損",
    "Average RSI": "平均 RSI",
    "Best Edge": "最佳優勢",
    "LONG WR": "多單勝率",
    "SHORT WR": "空單勝率",
    "4-Light WR": "4燈勝率",
    "5-Light WR": "5燈勝率",
    "6+ Light WR": "6+燈勝率",

    // ── Filter controls ───────────────────────────────────────
    "All": "全部",
    "Active": "進行中",
    "Queued": "排隊中",
    "Closed": "已平倉",
    "All Lights": "全部燈號",
    "All RSI": "全部 RSI",
    "All Scores": "全部分數",
    "All Status": "全部狀態",
    "Hide ≥90/≤10": "隱藏 ≥90/≤10",
    "Only ≥90/≤10": "僅 ≥90/≤10",

    // ── Strategy "Rules" modal ────────────────────────────────
    "🐺 Wolf Scanner Strategy Rules": "🐺 Wolf Scanner 策略規則",
    "Overview — How a Trade Happens": "概覽 — 交易如何發生",
    "Entry Conditions — Symmetric (LONG = SHORT)": "進場條件 — 對稱（多 = 空）",
    "Conviction System — 5 Base Lights + SMC Bonus": "信心系統 — 5 基礎燈 + SMC 加成",
    "Cadence & Controls": "節奏與控制",
    "Daily Circuit Breaker": "每日熔斷機制",
    "Risk Protection & Cooldowns": "風險保護與冷卻",
    "Live vs Simulation — Know the Difference": "實盤 vs 模擬 — 了解差異",
    "Live account": "實盤帳戶",
    "Simulation (dashboard)": "模擬（儀表板）",
    "Behaviour": "行為",
    "Live scan health, queue pressure, and RSI extremes":
      "即時掃描健康度、排隊壓力與 RSI 極值",
    "Compact analysis from your qualified trading history":
      "來自合格交易紀錄的精簡分析"
  };

  var LANG_KEY = "wolfLang";
  var translating = false;
  var observer = null;

  function getLang() {
    try { return localStorage.getItem(LANG_KEY) || "en"; }
    catch (e) { return "en"; }
  }

  // Walk visible text nodes and swap any whose trimmed text is a known phrase,
  // preserving the surrounding whitespace so layout/indentation is untouched.
  function translateTextNodes(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        var p = node.parentNode;
        if (!p) return NodeFilter.FILTER_REJECT;
        var tag = p.nodeName;
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT")
          return NodeFilter.FILTER_REJECT;
        if (p.closest && p.closest("#nav-regime")) // dynamic regime chip
          return NodeFilter.FILTER_REJECT;
        return node.nodeValue && node.nodeValue.trim()
          ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    var n;
    while ((n = walker.nextNode())) {
      var raw = n.nodeValue;
      var key = raw.trim();
      if (Object.prototype.hasOwnProperty.call(DICT, key)) {
        n.nodeValue = raw.replace(key, DICT[key]);
      }
    }
  }

  // Translate a few attributes that surface as visible text (tooltips/inputs).
  function translateAttrs(root) {
    var els = root.querySelectorAll("[title],[placeholder]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      ["title", "placeholder"].forEach(function (attr) {
        var v = el.getAttribute(attr);
        if (v && Object.prototype.hasOwnProperty.call(DICT, v.trim())) {
          el.setAttribute(attr, DICT[v.trim()]);
        }
      });
    }
  }

  // ── 中文 → English ────────────────────────────────────────────────────────
  // The DICT above translates English markup INTO Chinese, which was the whole
  // job while the templates were written in English. Most of this app is now
  // authored in 中文 — 910 distinct phrases across the templates — and that
  // dictionary cannot reach any of it, which is why the pages read as a
  // mixture rather than as either language.
  //
  // SUBSTRING replacement, longest phrase first, not whole-string lookup. The
  // rendered text is assembled at runtime from fragments — '· 延遲 ' + 195 +
  // 's' — so the Chinese arrives glued to numbers and a whole-string match
  // finds none of it. After concatenation the fragment is still there
  // verbatim, so replacing within the text node works where equality does not.
  // Longest-first is what stops '隧道' rewriting the inside of '隧道上方爆量'.
  //
  // IDEMPOTENT BY CONSTRUCTION: the output contains no Chinese, so a second
  // pass over an already-translated node matches nothing. That matters because
  // the MutationObserver re-runs this on every card re-render.
  // SHORT keys are whole-node only. Chinese has no word boundaries, so a
  // 1-2 character entry used as a substring rewrites the inside of longer
  // words: 多→"long" and 進場→"Entry" turned 新多進場 into "新longEntry",
  // 平倉→"close" turned 未平倉 into "未close", and 近→"last" turned 最近 into
  // "最last". Longest-first ordering does not help, because the longer word
  // is not in the table at all — that is precisely why the short key reached
  // inside it. Requiring a short phrase to BE the whole text node removes the
  // class rather than the instances.
  var WHOLE_MAX = 3;
  var SPLIT = null;

  function zhEnPairs() {
    if (SPLIT) return SPLIT;
    var table = window.WOLF_ZH_EN || {};
    var keys = Object.keys(table);
    keys.sort(function (a, b) { return b.length - a.length; });
    var okSub = {};
    (window.WOLF_ZH_EN_SUB || []).forEach(function (k) { okSub[k] = 1; });
    var sub = [], whole = {};
    keys.forEach(function (k) {
      var cjk = k.replace(/[^\u4e00-\u9fff]/g, "").length;
      // Long enough to be unambiguous, or explicitly vetted as safe.
      if (cjk > WHOLE_MAX || okSub[k]) sub.push([k, table[k]]);
      else whole[k] = table[k];
    });
    SPLIT = { sub: sub, whole: whole };
    return SPLIT;
  }

  function toEnglish(text) {
    var t = zhEnPairs();
    // A short phrase only counts when it IS the node.
    var trimmed = text.trim();
    if (Object.prototype.hasOwnProperty.call(t.whole, trimmed)) {
      return text.replace(trimmed, t.whole[trimmed]);
    }
    var out = text;
    for (var i = 0; i < t.sub.length; i++) {
      if (out.indexOf(t.sub[i][0]) >= 0) {
        out = out.split(t.sub[i][0]).join(t.sub[i][1]);
      }
    }
    return out;
  }

  var CJK = /[\u4e00-\u9fff]/;

  function translateToEnglish(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    var node, hits = [];
    while ((node = walker.nextNode())) {
      if (!node.nodeValue || !CJK.test(node.nodeValue)) continue;
      var p = node.parentNode;
      if (p && (p.nodeName === "SCRIPT" || p.nodeName === "STYLE")) continue;
      hits.push(node);
    }
    for (var i = 0; i < hits.length; i++) {
      var v = hits[i].nodeValue;
      var t = toEnglish(v);
      if (t !== v) hits[i].nodeValue = t;
    }
    // title / placeholder / aria-label carry text too.
    ["title", "placeholder", "aria-label"].forEach(function (attr) {
      var els = root.querySelectorAll ? root.querySelectorAll("[" + attr + "]") : [];
      Array.prototype.forEach.call(els, function (el) {
        var val = el.getAttribute(attr);
        if (val && CJK.test(val)) el.setAttribute(attr, toEnglish(val));
      });
    });
  }

  function applyEn() {
    translating = true;
    if (observer) observer.disconnect();
    try {
      translateToEnglish(document.body);
      document.documentElement.setAttribute("lang", "en");
    } finally {
      if (observer) observer.observe(document.body, {
        childList: true, subtree: true, characterData: true
      });
      translating = false;
    }
  }

  // How much of what is on screen right now is still untranslated. Exposed
  // rather than hidden: "mixed" was the complaint, so it needs to be
  // MEASURABLE instead of a matter of opinion.
  window.wolfLangCoverage = function () {
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT,
                                           null, false);
    var node, left = [];
    while ((node = walker.nextNode())) {
      var p = node.parentNode;
      if (p && (p.nodeName === "SCRIPT" || p.nodeName === "STYLE")) continue;
      var v = (node.nodeValue || "").trim();
      // 中文 on the toggle is deliberate — the button names the language it
      // switches TO, so in English mode it must read 中文. Counting it as a
      // miss would mean the coverage number could never reach zero.
      if (v && CJK.test(v) && v !== "中文") left.push(v);
    }
    return { untranslated: left.length, samples: left.slice(0, 40) };
  };

  function applyZh() {
    translating = true;
    if (observer) observer.disconnect();
    try {
      translateTextNodes(document.body);
      translateAttrs(document.body);
      document.documentElement.setAttribute("lang", "zh-Hant");
    } finally {
      if (observer) observer.observe(document.body, {
        childList: true, subtree: true, characterData: true
      });
      translating = false;
    }
  }

  // Re-translate dynamically rendered chrome (scan cards refresh, status flips).
  function startObserver() {
    if (observer) return;
    var pending = null;
    observer = new MutationObserver(function () {
      if (translating) return;
      if (pending) return;
      pending = setTimeout(function () {
        pending = null;
        if (getLang() === "zh") { applyZh(); } else { applyEn(); }
      }, 150);
    });
    observer.observe(document.body, {
      childList: true, subtree: true, characterData: true
    });
  }

  // The button shows the language you'll switch TO.
  function setToggleLabel(lang) {
    var lbl = document.getElementById("wolf-lang-label");
    if (lbl) lbl.textContent = lang === "zh" ? "EN" : "中文";
  }

  window.wolfToggleLang = function () {
    var next = getLang() === "zh" ? "en" : "zh";
    try { localStorage.setItem(LANG_KEY, next); } catch (e) {}
    location.reload(); // reload → English server render returns cleanly when EN
  };

  document.addEventListener("DOMContentLoaded", function () {
    var lang = getLang();
    setToggleLabel(lang);
    // Both directions now run. EN used to be a no-op because the server
    // rendered English; it renders 中文 now, so English is a translation too.
    startObserver();
    if (lang === "zh") { applyZh(); } else { applyEn(); }
  });
})();
