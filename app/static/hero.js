/* Wolf Scanner — landing hero.
 *
 * The cinematic treatment belongs HERE and only here: /welcome is a page
 * someone lands on once, from a Telegram link, and decides in five seconds
 * whether to join. The dashboard is the opposite kind of page and got the
 * opposite kind of motion (see motion.js).
 *
 * The backdrop is a drifting price field rather than a stock photo, because
 * the honest visual for a trading tool is a market. It is ABSTRACT on purpose
 * — a random walk, seeded and slow — and never labelled with a symbol or a
 * price, so it cannot be mistaken for live data or read as a prediction. This
 * page's whole argument is that it publishes losing trades; decorating it with
 * a fake green chart would undercut the one thing it is selling.
 */
(function (global) {
    'use strict';
    var REDUCED = global.matchMedia &&
                  global.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function field(canvas) {
        if (!canvas || !canvas.getContext) return;
        var ctx = canvas.getContext('2d'), w = 0, h = 0, dpr = 1;
        // A fixed seed: the hero looks the same on every visit, so it reads as
        // a designed backdrop rather than something that changed under you.
        var seed = 20260820;
        function rnd() { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; }

        var LINES = 3, pts = [];
        for (var l = 0; l < LINES; l++) {
            var arr = [], v = 0;
            for (var i = 0; i < 160; i++) { v += (rnd() - 0.48) * 0.9; arr.push(v); }
            pts.push(arr);
        }
        function size() {
            dpr = Math.min(global.devicePixelRatio || 1, 2);
            w = canvas.clientWidth; h = canvas.clientHeight;
            canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        }
        size();
        if (global.ResizeObserver) new ResizeObserver(size).observe(canvas);

        var COLORS = ['rgba(255,210,87,', 'rgba(41,221,255,', 'rgba(240,212,154,'];
        var t = 0, raf = null;
        function draw() {
            ctx.clearRect(0, 0, w, h);
            for (var l = 0; l < LINES; l++) {
                var a = pts[l], n = a.length, span = w / (n - 1);
                var amp = h * (0.10 + l * 0.05), mid = h * (0.52 + l * 0.06);
                var off = (t * (0.16 + l * 0.07)) % span;
                ctx.beginPath();
                for (var i = 0; i < n; i++) {
                    var x = i * span - off, y = mid - a[i] * amp * 0.16;
                    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                }
                var g = ctx.createLinearGradient(0, 0, w, 0);
                g.addColorStop(0, COLORS[l] + '0)');
                g.addColorStop(0.45, COLORS[l] + (0.38 - l * 0.09) + ')');
                g.addColorStop(1, COLORS[l] + '0)');
                ctx.strokeStyle = g; ctx.lineWidth = 1.4 - l * 0.3; ctx.stroke();
            }
            t += 1;
            raf = requestAnimationFrame(draw);
        }
        function start() { if (!raf && !document.hidden) raf = requestAnimationFrame(draw); }
        function stop() { if (raf) { cancelAnimationFrame(raf); raf = null; } }
        // A backdrop must not spin a phone's GPU behind a tab nobody is looking
        // at — this page is opened from a link and left open.
        document.addEventListener('visibilitychange', function () { document.hidden ? stop() : start(); });

        if (REDUCED) { draw(); stop(); return; }   // one static frame, then still
        start();
    }

    /* Headline: each word rises out of its own mask. Reads as one motion
     * rather than a letter-by-letter effect, which at this size looks like a
     * loading state. */
    function kinetic(el) {
        if (!el) return;
        var words = el.textContent.trim().split(/\s+/);
        el.innerHTML = words.map(function (word, i) {
            return '<span class="kw"><i style="animation-delay:' + (i * 90 + 80) + 'ms">' +
                   word.replace(/[&<>]/g, '') + '</i></span>';
        }).join(' ');
        el.classList.add('kinetic-on');
    }

    /* Stats count up the FIRST time they are seen — the numbers are the
     * page's evidence, and a number that arrives is read; one that was always
     * there is scenery. */
    function countUp(els) {
        if (!els.length) return;
        var roll = (global.WolfMotion && global.WolfMotion.rollTo);
        Array.prototype.forEach.call(els, function (el) {
            var target = el.textContent.trim();
            if (REDUCED || !roll) { el.textContent = target; return; }
            var m = /^([^\d\-+]*)([-+]?[\d,]*\.?\d+)(.*)$/.exec(target);
            if (!m) return;
            el.textContent = m[1] + '0' + m[3];
            var io = new IntersectionObserver(function (es) {
                es.forEach(function (e) {
                    if (!e.isIntersecting) return;
                    roll(el, target, 900);
                    io.unobserve(el);
                });
            }, {threshold: 0.5});
            io.observe(el);
        });
    }

    function init() {
        field(document.getElementById('hero-field'));
        kinetic(document.querySelector('[data-kinetic]'));
        countUp(document.querySelectorAll('[data-count]'));
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})(window);
