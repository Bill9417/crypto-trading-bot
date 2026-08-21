/* ============================================================================
 * Wolf Scanner — wolfLive: refresh a card the moment its data lands.
 *
 * WHY. Measured 2026-08-21, the age of the number on a card is
 *
 *     poll interval  +  upstream cadence
 *
 * and those are fixed by different things. The 供需區 card polls every 120s
 * against a scanner that writes every 300s, so up to 120 of those seconds are
 * simply not having asked yet. One push removes that half entirely, and does
 * it while making FEWER requests than polling faster would.
 *
 * The other half cannot be shortened by refreshing at all — 隧道翻多 reads
 * closed 1h bars, so asking every second returns the same answer 3,600 times.
 * That is why every event carries `ages` and why wolfLive exposes it: a card
 * that says "掃描於 12 分鐘前" is honest, and one that merely looks live
 * because it sits on a live-looking page is not.
 *
 * USAGE — a drop-in for wolfPoll, with the feed name added:
 *
 *     wolfLive('zones', load, 120000);     // instead of wolfPoll(load, 120000)
 *
 * The poll is KEPT as a fallback, deliberately. If the stream is refused (the
 * server caps concurrent streams because each one holds a worker thread), or
 * EventSource is unavailable, or a proxy eats the connection, the card behaves
 * exactly as it does today rather than freezing. Push is an improvement layered
 * on top of a working thing, never a replacement for it.
 *
 * ONE connection per page, shared by every card. Ten EventSources would be ten
 * held server threads out of twelve.
 * ========================================================================== */
(function () {
  "use strict";

  var subs = {};          // feed -> [fn]
  var ages = {};          // feed -> seconds since that data was written
  var seenAt = {};        // feed -> Date.now() when that age was received

  // feed name -> data-card id, where they differ. Everything else matches by
  // construction, which is why the feed names were chosen to.
  var CARD_OF = { liq: "liqmap" };
  var es = null;
  var connected = false;
  var retryMs = 2000;
  var stopped = false;

  function fire(feed) {
    var list = subs[feed] || [];
    for (var i = 0; i < list.length; i++) {
      // One card throwing must not stop the others being refreshed.
      try { list[i](); } catch (e) { /* keep going */ }
    }
  }

  function onPayload(d) {
    if (d && d.ages) {
      var now = Date.now();
      for (var k in d.ages) { ages[k] = d.ages[k]; seenAt[k] = now; }
      stampAll();
    }
    if (d && d.feeds && d.feeds.length) {
      for (var i = 0; i < d.feeds.length; i++) fire(d.feeds[i]);
    }
  }

  function connect() {
    if (stopped || typeof window.EventSource !== "function") return;
    try {
      es = new EventSource("/api/stream");
    } catch (e) {
      return;                                  // polling still covers it
    }

    es.addEventListener("hello", function (ev) {
      connected = true;
      retryMs = 2000;
      try { onPayload(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
    });
    es.addEventListener("changed", function (ev) {
      try { onPayload(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
    });
    es.addEventListener("beat", function (ev) {
      try { onPayload(JSON.parse(ev.data)); } catch (e) { /* ignore */ }
    });
    es.addEventListener("bye", function () {
      // The server ends streams on a timer so a leaked connection cannot hold
      // a worker thread forever. Reconnect immediately — this is the normal
      // path, not an error.
      close();
      setTimeout(connect, 250);
    });
    es.onerror = function () {
      connected = false;
      close();
      // Backoff, capped. A refused stream (the server caps concurrency) must
      // not turn into a reconnect loop that spends more than polling would.
      setTimeout(connect, retryMs);
      retryMs = Math.min(retryMs * 2, 60000);
    };
  }

  function close() {
    if (es) { try { es.close(); } catch (e) { /* ignore */ } }
    es = null;
  }

  // A hidden tab does not need a held server thread. Same reasoning as
  // wolfPoll skipping hidden ticks — and here it frees a scarce resource
  // rather than just saving a request.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      close();
      connected = false;
    } else if (!es) {
      retryMs = 2000;
      connect();
      // Catch up on whatever was missed while away, immediately.
      for (var feed in subs) fire(feed);
    }
  });

  // ── the honest half ───────────────────────────────────────────────────────
  // Pushing removes the poll lag. It does NOT make an hourly scanner
  // hourly-fresh, and a card that merely LOOKS live because it sits on a
  // live-looking page is the failure this repo keeps finding. So every pushed
  // card gets stamped with how old its data actually is.
  //
  // The age is carried forward locally rather than only refreshed on a
  // heartbeat: age = (what the server said) + (how long ago it said it). That
  // stays correct between beats AND while the stream is down, which is exactly
  // when a frozen "3秒前" would be most misleading.
  function liveAge(feed) {
    if (!Object.prototype.hasOwnProperty.call(ages, feed)) return null;
    var drift = seenAt[feed] ? (Date.now() - seenAt[feed]) / 1000 : 0;
    return ages[feed] + drift;
  }

  function human(s) {
    if (s == null) return "";
    if (s < 20) return "剛更新";
    if (s < 90) return Math.round(s) + "秒前";
    if (s < 5400) return Math.round(s / 60) + "分前";
    if (s < 172800) return Math.round(s / 3600) + "小時前";
    return Math.round(s / 86400) + "天前";
  }

  function stamp(feed) {
    var id = CARD_OF[feed] || feed;
    var card = document.querySelector('[data-card="' + id + '"]');
    if (!card) return;
    var head = card.querySelector(".analytics-head");
    if (!head) return;
    var el = head.querySelector(".wolf-age");
    if (!el) {
      el = document.createElement("span");
      el.className = "wolf-age";
      // .72rem, not .6rem: at 390px that was 9.6px, which the phone-layout
      // pass measured as unreadable. Inline, so it beats the stylesheet —
      // which means the media query cannot fix it and the size has to be
      // right here.
      el.style.cssText = "flex:0 0 auto;align-self:center;font-size:.72rem;" +
        "font-weight:700;letter-spacing:.04em;white-space:nowrap;" +
        "color:var(--wolf-muted,#8a99ad);opacity:.75;";
      head.appendChild(el);
    }
    var a = liveAge(feed);
    // Title carries the exact seconds; the chip stays short on a phone.
    el.textContent = "資料 " + human(a);
    el.title = "這張卡的資料寫入於 " + Math.round(a) + " 秒前" +
      (connected ? "（即時推送已連線）" : "（推送未連線，改用輪詢）");
    el.style.opacity = connected ? ".75" : ".45";
  }

  function stampAll() {
    for (var f in ages) stamp(f);
  }

  // Tick locally so the chip ages between heartbeats instead of freezing on
  // whatever the last beat said.
  setInterval(stampAll, 5000);

  window.wolfLive = function (feed, fn, ms) {
    if (typeof fn !== "function") return { stop: function () {} };
    (subs[feed] = subs[feed] || []).push(fn);
    if (!es) connect();
    // The fallback poll. Slower is fine BECAUSE the push exists, but it must
    // exist: a card whose only trigger is a stream is a card that freezes the
    // first time the stream is refused.
    var handle = (window.wolfPoll ? window.wolfPoll(fn, ms)
                                  : { stop: function () {} });
    if (!window.wolfPoll) {
      var id = setInterval(fn, ms);
      handle = { stop: function () { clearInterval(id); } };
    }
    return {
      stop: function () {
        handle.stop();
        subs[feed] = (subs[feed] || []).filter(function (f) { return f !== fn; });
      }
    };
  };

  window.wolfLiveInfo = {
    // How old each feed's data is, straight from the server's file mtimes.
    // Exposed so a card can PRINT it instead of implying freshness.
    age: liveAge,
    human: human,
    ages: function () { return ages; },
    connected: function () { return connected; },
    stop: function () { stopped = true; close(); }
  };
})();
