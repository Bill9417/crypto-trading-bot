/* 📈 wolf_chart — the chart engine shared by /strategy2 and /coin.
 *
 * ONE COPY, on purpose. This was inline in strategy2.html and the moment a
 * second page needed a chart the obvious move was to paste it, which is how
 * this repo has already lost the Pine↔Python weights, the public-page CSS
 * tokens, and the outer tunnel below. A drawing that disagrees with the number
 * printed next to it is worse than no drawing.
 *
 * EMA PERIODS COME FROM THE SERVER (`ema_defs` on the OHLCV payload), which
 * derives them from strategy2_meter, which is itself pinned to the .pine. The
 * periods were hardcoded here as 288/338 and stayed that way when the outer
 * tunnel moved to 576/676 on 2026-08-11 — for five days the chart drew a
 * tunnel the score did not use, under a legend promising they matched.
 * DEFAULT_EMA_DEFS below is a last resort for an old cached response; it is
 * deliberately missing the tunnel pair rather than carrying a stale guess,
 * because no line is honest and a wrong line is not.
 */
(function (global) {
    'use strict';

    var DEFAULT_EMA_DEFS = [
        { key: 'e20',  len: 20,  color: '#FFEB3B', width: 1, label: 'EMA 20' },
        { key: 'e50',  len: 50,  color: '#FF9800', width: 1, label: 'EMA 50' },
        { key: 'e100', len: 100, color: '#E040FB', width: 1, label: 'EMA 100' },
        { key: 'e200', len: 200, color: '#2962FF', width: 2, label: 'EMA 200' }
    ];
    var VEGAS_UP = '#16a34a', VEGAS_DN = '#dc2626';
    var LINE_BASE = { priceLineVisible: false, lastValueVisible: false,
                      crosshairMarkerVisible: false };

    // EMA seeded at the first value (α = 2/(n+1)) — matches the server's
    // pandas ewm(adjust=False), so the on-chart lines equal the meter's factors.
    function emaSeries(values, period) {
        var k = 2 / (period + 1), out = new Array(values.length), prev = values[0];
        out[0] = prev;
        for (var i = 1; i < values.length; i++) { prev = values[i] * k + prev * (1 - k); out[i] = prev; }
        return out;
    }
    function smaSeries(values, period) {
        var out = new Array(values.length), sum = 0;
        for (var i = 0; i < values.length; i++) {
            sum += values[i];
            if (i >= period) sum -= values[i - period];
            out[i] = i >= period - 1 ? sum / period : null;
        }
        return out;
    }

    // v4 uses addCandlestickSeries/addLineSeries; v5 swaps to addSeries(Type,…).
    function addCandles(chart, opts) {
        return chart.addCandlestickSeries ? chart.addCandlestickSeries(opts)
            : chart.addSeries(global.LightweightCharts.CandlestickSeries, opts);
    }
    function addLine(chart, opts) {
        return chart.addLineSeries ? chart.addLineSeries(opts)
            : chart.addSeries(global.LightweightCharts.LineSeries, opts);
    }

    function priceFmt(p) {
        var prec = p >= 100 ? 2 : p >= 1 ? 4 : p >= 0.01 ? 5 : 8;
        return { type: 'price', precision: prec, minMove: Math.pow(10, -prec) };
    }

    function WolfChart(elId, opts) {
        this.elId = elId;
        this.opts = opts || {};
        this.lwc = null;
        this.defs = DEFAULT_EMA_DEFS;
    }

    WolfChart.prototype.ensure = function () {
        if (this.lwc || !global.LightweightCharts) return this.lwc;
        var el = document.getElementById(this.elId);
        if (!el) return null;
        var chart = global.LightweightCharts.createChart(el, {
            width: el.clientWidth, height: el.clientHeight,
            layout: { background: { type: 'solid', color: 'rgba(0,0,0,0)' },
                      textColor: '#8a99ad', fontFamily: "'Inter', sans-serif" },
            grid: { vertLines: { color: 'rgba(255,255,255,.04)' },
                    horzLines: { color: 'rgba(255,255,255,.04)' } },
            rightPriceScale: { borderColor: 'rgba(255,255,255,.08)' },
            timeScale: { borderColor: 'rgba(255,255,255,.08)', timeVisible: true,
                         secondsVisible: false },
            crosshair: { mode: 0 }
        });
        var candle = addCandles(chart, {
            upColor: '#16a34a', downColor: '#dc2626', borderUpColor: '#16a34a',
            borderDownColor: '#dc2626', wickUpColor: '#16a34a', wickDownColor: '#dc2626'
        });
        var vegas = addLine(chart, Object.assign({ color: VEGAS_UP, lineWidth: 2 }, LINE_BASE));
        // Meter-score history on its own overlay scale pinned to the bottom
        // ~20%, time-synced with the candles for free. 85/15 mark
        // live-entry-grade conviction.
        var score = addLine(chart, Object.assign({
            color: '#22d3ee', lineWidth: 2, priceScaleId: 'wscore'
        }, LINE_BASE));
        chart.priceScale('wscore').applyOptions({
            scaleMargins: { top: 0.80, bottom: 0.02 }, visible: false
        });
        [{ v: 85, c: 'rgba(74,222,128,.55)' }, { v: 15, c: 'rgba(248,113,113,.55)' }]
            .forEach(function (t) {
                score.createPriceLine({ price: t.v, color: t.c, lineWidth: 1,
                                        lineStyle: 2, axisLabelVisible: false, title: '' });
            });
        try {
            new ResizeObserver(function () {
                if (el.clientWidth) chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
            }).observe(el);
        } catch (e) { /* no ResizeObserver → static size, still fine */ }
        this.lwc = { chart: chart, candle: candle, lines: {}, vegas: vegas, score: score };
        return this.lwc;
    };

    /* Lines are (re)built when the server's definitions arrive, so a period
     * change on the server reshapes the chart without a deploy here. */
    WolfChart.prototype.applyDefs = function (defs) {
        var c = this.ensure();
        if (!c || !defs || !defs.length) return;
        var same = JSON.stringify(defs) === JSON.stringify(this.defs) &&
                   Object.keys(c.lines).length;
        if (same) return;
        var self = this;
        Object.keys(c.lines).forEach(function (k) {
            try { c.chart.removeSeries(c.lines[k]); } catch (e) { /* already gone */ }
        });
        c.lines = {};
        defs.forEach(function (d) {
            c.lines[d.key] = addLine(c.chart,
                Object.assign({ color: d.color, lineWidth: d.width }, LINE_BASE));
        });
        self.defs = defs;
        if (self.opts.onLegend) self.opts.onLegend(defs, VEGAS_UP, VEGAS_DN);
    };

    WolfChart.prototype.draw = function (rows) {
        var c = this.ensure();
        if (!c || !rows || !rows.length) return;
        var times = rows.map(function (r) { return Math.floor(r[0] / 1000); });
        var closes = rows.map(function (r) { return r[4]; });
        c.candle.setData(rows.map(function (r) {
            return { time: Math.floor(r[0] / 1000), open: r[1], high: r[2], low: r[3], close: r[4] };
        }));
        c.candle.applyOptions({ priceFormat: priceFmt(closes[closes.length - 1] || 0) });
        this.defs.forEach(function (d) {
            if (!c.lines[d.key]) return;
            var s = emaSeries(closes, d.len);
            // A period longer than the history would draw a line seeded from
            // one bar and pretend it is an EMA — leave it empty instead.
            if (closes.length < d.len) { c.lines[d.key].setData([]); return; }
            c.lines[d.key].setData(times.map(function (t, i) {
                return { time: t, value: i >= d.len - 1 ? s[i] : null };
            }).filter(function (p) { return p.value != null; }));
        });
        // Vegas EMA200 = SMA5 of EMA200, each segment coloured by its slope.
        var vg = smaSeries(emaSeries(closes, 200), 5), vdata = [];
        for (var i = 0; i < vg.length; i++) {
            if (vg[i] == null) continue;
            var col = (i > 0 && vg[i - 1] != null && vg[i] < vg[i - 1]) ? VEGAS_DN : VEGAS_UP;
            vdata.push({ time: times[i], value: vg[i], color: col });
        }
        c.vegas.setData(vdata);
        c.chart.timeScale().fitContent();
    };

    WolfChart.prototype.setScore = function (points) {
        var c = this.ensure();
        if (!c || !c.score) return;
        c.score.setData((points || []).map(function (p) {
            return { time: Math.floor(p[0] / 1000), value: p[1] };
        }));
    };

    /* One symbol: candles + EMA defs first, score history after. The candles
     * never wait for the score — the first call recomputes ~100 bars server
     * side and blocking the picture on it makes the page feel broken. */
    WolfChart.prototype.load = function (sym) {
        var self = this;
        var el = document.getElementById(this.elId);
        if (!global.LightweightCharts) {
            if (el) el.innerHTML = '<div class="lwc-fallback">Chart library didn’t load.</div>';
            return;
        }
        fetch('/api/strategy2_ohlcv/' + sym)
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                if (!d) return;
                self.applyDefs(d.ema_defs);
                if (d.candles && d.candles.length) self.draw(d.candles);
            })
            .catch(function () { });
        this.setScore([]);
        fetch('/api/strategy2_score_history/' + sym)
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) { if (d && d.points && d.points.length) self.setScore(d.points); })
            .catch(function () { });
    };

    WolfChart.VEGAS_UP = VEGAS_UP;
    WolfChart.VEGAS_DN = VEGAS_DN;
    global.WolfChart = WolfChart;
})(window);
