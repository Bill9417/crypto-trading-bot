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
        applyZh();
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
    if (lang === "zh") {
      startObserver();
      applyZh();
    }
  });
})();
