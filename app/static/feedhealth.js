/* ============================================================================
 * Wolf Scanner — 資料來源狀態: one place that notices when an API call fails.
 *
 * WHY THIS EXISTS. An audit on 2026-08-21 found 39 of the 67 fetch() sites in
 * these templates either swallow the error outright — `.catch(function(){})` —
 * or never look at `r.ok` at all. Both produce the same thing on screen: a card
 * that shows nothing, and looks exactly like a card whose answer is legitimately
 * empty. That ambiguity has now cost real time twice:
 *
 *   · 每日觀察清單 sat on 載入中… forever because /api/daily_watch was 404ing on
 *     a stale process, and the empty catch never said so
 *   · when that was fixed, the message it renders was invisible on a phone
 *
 * Fixing 39 call sites by hand fixes 39 call sites. Wrapping fetch once fixes
 * every one of them, plus the next one somebody writes. The card's own handling
 * is unchanged — this layer only OBSERVES.
 *
 * IT MUST NOT CHANGE BEHAVIOUR. The wrapper returns the same response object and
 * re-throws the same rejection, so every downstream .then / .catch still runs
 * exactly as before. If this file fails to load, the pages work as they do today.
 *
 * WHAT IT REPORTS is the distinction the cards keep losing:
 *   404 → the route does not exist on the running process (deploy without a
 *         restart — app.py does not hot-reload, templates do), so the fix is
 *         /restart, not "wait for data"
 *   5xx → the server broke on this endpoint
 *   network → the browser could not reach the site at all (Funnel down, offline)
 * ========================================================================== */
(function () {
  "use strict";

  // Only same-origin API calls. Third-party and static requests are not this
  // indicator's business, and counting them would make it cry wolf.
  var WATCH = /^\/api\//;
  // A cap so a page left open for a day cannot grow this without bound.
  var MAX_TRACKED = 60;
  // A failure has to persist to be worth interrupting anyone about: one bad
  // poll during a restart is normal, and a badge that flashes on every deploy
  // is a badge people learn to ignore.
  var MIN_FAILS = 2;

  var feeds = {};          // path -> {fails, status, kind, last, lastOk}
  var el = null;
  var open = false;

  function pathOf(input) {
    try {
      var u = typeof input === "string" ? input : (input && input.url) || "";
      if (!u) return null;
      var a = document.createElement("a");
      a.href = u;
      if (a.host && a.host !== location.host) return null;   // cross-origin
      return a.pathname;
    } catch (e) { return null; }
  }

  function note(path, kind, status) {
    if (!path || !WATCH.test(path)) return;
    var f = feeds[path];
    if (!f) {
      var keys = Object.keys(feeds);
      if (keys.length >= MAX_TRACKED) delete feeds[keys[0]];
      f = feeds[path] = { fails: 0, status: 0, kind: "", last: 0, lastOk: 0 };
    }
    if (kind === "ok") {
      // Recovery clears the count outright. A feed that answers is healthy;
      // carrying old failures forward would keep the badge up after the cure.
      f.fails = 0; f.kind = ""; f.status = 0; f.lastOk = Date.now();
    } else {
      f.fails += 1; f.kind = kind; f.status = status || 0; f.last = Date.now();
    }
    render();
  }

  function broken() {
    var out = [];
    for (var p in feeds) {
      if (feeds[p].fails >= MIN_FAILS) out.push({ path: p, f: feeds[p] });
    }
    return out.sort(function (a, b) { return b.f.fails - a.f.fails; });
  }

  function reason(f) {
    if (f.kind === "network") return "連不上伺服器";
    if (f.status === 404) return "404 — 需要重啟（/restart）";
    if (f.status === 401 || f.status === 403) return f.status + " — 需要重新登入";
    if (f.status >= 500) return f.status + " — 伺服器錯誤";
    return (f.status || "?") + " — 載入失敗";
  }

  function ensure() {
    if (el) return el;
    el = document.createElement("div");
    el.id = "wolf-feed-health";
    el.setAttribute("role", "status");
    el.style.cssText = [
      "position:fixed", "z-index:9998", "right:10px",
      // Above the iOS home indicator, and clear of any bottom bar.
      "bottom:calc(12px + env(safe-area-inset-bottom, 0px))",
      "max-width:min(340px, calc(100vw - 20px))",
      "font:600 12px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
      "background:rgba(24,18,12,.94)", "color:#f8d477",
      "border:1px solid rgba(248,113,113,.55)", "border-radius:10px",
      "padding:7px 11px", "cursor:pointer", "display:none",
      "box-shadow:0 6px 20px rgba(0,0,0,.45)", "backdrop-filter:blur(6px)",
      "-webkit-backdrop-filter:blur(6px)"
    ].join(";");
    el.addEventListener("click", function () { open = !open; render(); });
    document.body.appendChild(el);
    return el;
  }

  function render() {
    if (!document.body) return;                 // called before body exists
    var bad = broken();
    var box = ensure();
    if (!bad.length) { box.style.display = "none"; open = false; return; }
    var head = "⚠ " + bad.length + " 個資料來源載入失敗";
    if (!open) {
      box.innerHTML = head + ' <span style="opacity:.6;font-weight:400">— 點一下看細節</span>';
    } else {
      var rows = bad.map(function (b) {
        return '<div style="margin-top:4px;font-weight:400;opacity:.9">' +
          '<span style="opacity:.7">' + b.path.replace(/^\/api\//, "") + '</span> · ' +
          reason(b.f) + '</div>';
      }).join("");
      box.innerHTML = head + rows +
        '<div style="margin-top:6px;font-weight:400;opacity:.55">' +
        '其他卡片顯示的資料是正常的 —— 只有上面這幾個沒載入到。</div>';
    }
    box.style.display = "block";
  }

  // ── the wrapper ───────────────────────────────────────────────────────────
  var native = window.fetch;
  if (typeof native !== "function") return;     // nothing to wrap

  window.fetch = function (input, init) {
    var path = pathOf(input);
    var p;
    try {
      p = native.apply(this, arguments);
    } catch (e) {
      note(path, "network", 0);
      throw e;
    }
    if (!p || typeof p.then !== "function") return p;
    return p.then(function (resp) {
      // Redirects and 304s are not failures.
      note(path, resp && resp.ok ? "ok" : "http", resp && resp.status);
      return resp;                              // unchanged, always
    }, function (err) {
      // An aborted request is a navigation or a deliberate cancel, not an
      // outage — counting it would light the badge every time a page unloads.
      var aborted = err && (err.name === "AbortError" || err.code === 20);
      if (!aborted) note(path, "network", 0);
      throw err;                                // same rejection, always
    });
  };
  window.fetch.wolfWrapped = true;

  // Exposed for the tests and for anything that wants to reason about it.
  window.wolfFeedHealth = {
    feeds: feeds,
    broken: broken,
    reason: reason,
    reset: function () { for (var k in feeds) delete feeds[k]; render(); }
  };
})();
