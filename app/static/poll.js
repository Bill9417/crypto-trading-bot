/* ============================================================================
 * Wolf Scanner — wolfPoll: setInterval that respects a hidden tab.
 *
 * Why this exists. Several pages poll REAL exchange accounts on a short timer
 * (/bybit every 8s, /account every 6s). A background tab kept polling them
 * forever, which is how three uncoordinated clients once collectively burst
 * past Bybit's per-key rate limit and spammed retCode 10006. Browsers throttle
 * background timers, but only to ~1/min and only after the tab has been hidden
 * for five minutes — plenty of headroom to keep hammering an account endpoint.
 *
 * index.html already solved this by hand: `if (document.hidden) return;` inside
 * every fetch plus one visibilitychange listener to catch up on return. That
 * idiom is correct but was copied into only 4 of 11 polling pages. This is the
 * same behaviour in one place:
 *
 *     wolfPoll(refresh, 8000);        // instead of setInterval(refresh, 8000)
 *
 *   • while the tab is hidden, ticks are skipped entirely (no fetch, no CPU)
 *   • on becoming visible again, fires once immediately IF a tick was missed,
 *     so the user sees fresh numbers instantly rather than waiting out the
 *     remaining interval
 *
 * Net effect: strictly less API pressure than before, and data on return is no
 * staler than it used to be. Returns a handle with .stop().
 *
 * Touches no trading logic — this is a browser timer, nothing else.
 * ========================================================================== */
(function () {
  "use strict";

  var registry = [];       // one shared visibilitychange listener, not one each

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    for (var i = 0; i < registry.length; i++) {
      var entry = registry[i];
      if (!entry.missed) continue;
      entry.missed = false;
      try { entry.fn(); } catch (e) { /* one bad poller must not stop the rest */ }
    }
  });

  window.wolfPoll = function (fn, ms) {
    if (typeof fn !== "function" || !(ms > 0)) return { stop: function () {} };
    var entry = { fn: fn, missed: false };
    registry.push(entry);
    var id = setInterval(function () {
      if (document.hidden) { entry.missed = true; return; }
      fn();
    }, ms);
    return {
      stop: function () {
        clearInterval(id);
        var at = registry.indexOf(entry);
        if (at >= 0) registry.splice(at, 1);
      }
    };
  };
})();
