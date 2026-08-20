/* Wolf Scanner — motion layer.
 *
 * Asked for after a TikTok of agency landing pages: full-bleed hero video,
 * scroll-driven serif type, cursor-follow parallax. Most of that is wrong
 * HERE and would make the product worse: those pages are read once for five
 * seconds and are trying to impress, while this one is read twenty times a day
 * and is trying to be legible. Motion that moves a number while someone is
 * reading it costs them the reading.
 *
 * So: motion on ARRIVAL and on CHANGE, never on idle.
 *   · reveal  — cards rise and fade in, staggered, once. Also on scroll for
 *               anything below the fold, so a long page feels assembled
 *               rather than dumped.
 *   · numbers — a value that changes counts to its new figure instead of
 *               teleporting, which is the one animation a live dashboard
 *               genuinely earns: it tells you something moved and by roughly
 *               how much, without you having to have been watching.
 *
 * Every effect is off under prefers-reduced-motion — not degraded, OFF, with
 * the end state applied immediately. A dashboard someone trades from must
 * never be waiting on an animation.
 */
(function (global) {
    'use strict';
    var REDUCED = global.matchMedia &&
                  global.matchMedia('(prefers-reduced-motion: reduce)').matches;

    /* ── reveal ─────────────────────────────────────────────────────────── */
    function reveal(root) {
        var els = (root || document).querySelectorAll('[data-card], .bcard, .analytics-panel, .card, .panel');
        if (!els.length) return;
        if (REDUCED) {
            Array.prototype.forEach.call(els, function (el) { el.classList.add('is-in'); });
            return;
        }
        var io = global.IntersectionObserver ? new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (!e.isIntersecting) return;
                // Stagger by POSITION IN THE BATCH, not by index in the page:
                // scrolling to card 40 should not wait 40 steps for its turn.
                var i = Number(e.target.getAttribute('data-reveal-i') || 0);
                e.target.style.transitionDelay = Math.min(i, 6) * 45 + 'ms';
                e.target.classList.add('is-in');
                io.unobserve(e.target);
            });
        }, { rootMargin: '0px 0px -8% 0px', threshold: 0.04 }) : null;

        Array.prototype.forEach.call(els, function (el, i) {
            if (el.classList.contains('is-in') || el.hasAttribute('data-no-reveal')) return;
            el.classList.add('will-reveal');
            el.setAttribute('data-reveal-i', i % 8);
            if (io) io.observe(el); else el.classList.add('is-in');
        });

        /* SAFETY NET. A decoration must never be able to hide content.
         * Measured on the dashboard: a full-width panel stayed at opacity 0
         * after scrolling to the bottom — IntersectionObserver does not fire
         * for every way an element can come into view (a container resizing
         * around it, a layout that reflows after data lands, a card the layout
         * editor un-hides). Rather than chase each case, sweep: anything whose
         * box is on screen gets revealed, observer or not.
         *
         * The sweep STOPS after a few passes — it exists to catch the arrival,
         * not to poll forever behind a page someone leaves open all day. */
        if (io) {
            var passes = 0;
            var sweep = setInterval(function () {
                var left = (root || document).querySelectorAll('.will-reveal:not(.is-in)');
                Array.prototype.forEach.call(left, function (el) {
                    var r = el.getBoundingClientRect();
                    // No box at all = display:none (a hidden dashboard card).
                    // Reveal it now: it is not visible anyway, so there is no
                    // animation to lose, and it cannot get stuck at 0 later.
                    if (!r.width && !r.height) { el.classList.add('is-in'); return; }
                    if (r.top < (global.innerHeight || 0) && r.bottom > 0) el.classList.add('is-in');
                });
                if (++passes >= 6) clearInterval(sweep);
            }, 700);
        }
    }

    /* ── numbers ────────────────────────────────────────────────────────── */
    // Parses "$64,953.10" / "+1.33%" / "-0.486R" into prefix, number, suffix so
    // the currency, sign and unit survive the animation. A roll that drops the
    // % is a roll that changed the meaning.
    var NUM_RE = /^([^\d\-+]*)([-+]?[\d,]*\.?\d+)(.*)$/;

    function rollTo(el, text, ms) {
        if (REDUCED) { el.textContent = text; return; }
        var to = NUM_RE.exec(String(text == null ? '' : text));
        var from = NUM_RE.exec(el.textContent || '');
        if (!to || !from) { el.textContent = text; return; }
        var a = parseFloat(from[2].replace(/,/g, '')),
            b = parseFloat(to[2].replace(/,/g, ''));
        if (!isFinite(a) || !isFinite(b) || a === b) { el.textContent = text; return; }
        var dec = (to[2].split('.')[1] || '').length,
            grouped = to[2].indexOf(',') >= 0,
            t0 = null, dur = ms || 520;
        if (el._roll) cancelAnimationFrame(el._roll);
        function frame(t) {
            if (t0 === null) t0 = t;
            var p = Math.min((t - t0) / dur, 1);
            var e = 1 - Math.pow(1 - p, 3);           // easeOutCubic
            var v = a + (b - a) * e;
            var s = v.toFixed(dec);
            if (grouped) s = Number(s).toLocaleString('en-US',
                {minimumFractionDigits: dec, maximumFractionDigits: dec});
            if (to[2][0] === '+' && v > 0 && s[0] !== '+') s = '+' + s;
            el.textContent = to[1] + s + to[3];
            if (p < 1) el._roll = requestAnimationFrame(frame);
            else { el.textContent = text; el._roll = null; }
        }
        el._roll = requestAnimationFrame(frame);
    }

    /* Watches an element and rolls it whenever its own text is replaced, so a
     * page does not have to call anything — it keeps rendering as it always
     * did and the number animates itself. */
    function watchNumbers(root) {
        if (REDUCED || !global.MutationObserver) return;
        var els = (root || document).querySelectorAll('[data-roll]');
        Array.prototype.forEach.call(els, function (el) {
            if (el._rollWatch) return;
            el._rollWatch = new MutationObserver(function (recs) {
                var next = el.textContent;
                if (el._rollLast === next || el._roll) return;
                var prev = el._rollLast;
                el._rollLast = next;
                if (prev == null) return;              // first paint: no roll
                el.textContent = prev;
                rollTo(el, next);
            });
            el._rollLast = el.textContent;
            el._rollWatch.observe(el, {childList: true, characterData: true, subtree: true});
        });
    }

    function init() { reveal(); watchNumbers(); }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else { init(); }

    global.WolfMotion = {reveal: reveal, rollTo: rollTo,
                         watchNumbers: watchNumbers, reduced: REDUCED};
})(window);
