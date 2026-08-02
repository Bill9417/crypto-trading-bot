/* ============================================================================
 * wolf3d.js — the small 3D core behind /universe.
 *
 * Deliberately NOT a 3D library. Every scene here is a few hundred points or
 * a coarse quad mesh; projecting those by hand costs ~200 lines and keeps
 * text crisp, versus ~150KB of WebGL that renders labels badly. If a scene
 * ever needs real geometry, lighting or tens of thousands of vertices, that
 * is the moment to reach for three.js — not before.
 *
 * Provides: orbit/zoom input, a perspective projector, painter's-algorithm
 * helpers for points and surfaces, a collision-avoiding label placer, and
 * hover/click hit-testing. Views supply a draw() and own their own data.
 * ========================================================================== */
(function () {
    "use strict";

    function Wolf3D(canvas, opts) {
        opts = opts || {};
        var self = this;
        this.cv = canvas;
        this.ctx = canvas.getContext('2d');
        this.W = 0; this.H = 0; this.DPR = 1;
        this.home = Object.assign({ yaw: -0.62, pitch: 0.42, dist: 3.5 }, opts.view || {});
        this.view = Object.assign({}, this.home);
        this.spin = opts.spin !== false;
        this.spinRate = opts.spinRate || 0.0022;
        this.focal = opts.focal || 1.42;
        this.onDraw = null;          // set by the active view
        this.onHover = null;
        this.onClick = null;
        this.hit = [];               // filled during draw: {x,y,r,data}
        this.hover = null;           // the hovered entry's data
        this._dirty = true;
        this._drag = null;

        this.resize = this.resize.bind(this);
        window.addEventListener('resize', this.resize);
        this.resize();
        this._bindInput();

        (function loop() {
            if (self.spin && !self.hover && !self._drag) {
                self.view.yaw += self.spinRate;
                self._dirty = true;
            }
            if (self._dirty && self.onDraw) {
                self.hit.length = 0;
                self.ctx.clearRect(0, 0, self.W, self.H);
                self.onDraw(self);
                self._dirty = false;
            }
            requestAnimationFrame(loop);
        })();
    }

    Wolf3D.prototype.dirty = function () { this._dirty = true; };

    Wolf3D.prototype.resize = function () {
        this.DPR = Math.min(window.devicePixelRatio || 1, 2);
        this.W = this.cv.clientWidth;
        this.H = this.cv.clientHeight;
        this.cv.width = Math.round(this.W * this.DPR);
        this.cv.height = Math.round(this.H * this.DPR);
        this.ctx.setTransform(this.DPR, 0, 0, this.DPR, 0, 0);
        this._dirty = true;
    };

    Wolf3D.prototype.reset = function (v) {
        this.view = Object.assign({}, this.home, v || {});
        this._dirty = true;
    };

    /* ── projection: yaw about Y, pitch about X, then perspective divide ── */
    Wolf3D.prototype.project = function (x, y, z) {
        var v = this.view;
        var cy = Math.cos(v.yaw), sy = Math.sin(v.yaw);
        var x1 = x * cy + z * sy, z1 = -x * sy + z * cy;
        var cp = Math.cos(v.pitch), sp = Math.sin(v.pitch);
        var y2 = y * cp - z1 * sp, z2 = y * sp + z1 * cp;
        var zc = z2 + v.dist;
        if (zc < 0.06) return null;                       // behind the camera
        var f = (Math.min(this.W, this.H) * this.focal) / zc;
        return { x: this.W / 2 + x1 * f, y: this.H / 2 - y2 * f, z: zc, f: f };
    };

    /* ── input ── */
    Wolf3D.prototype._bindInput = function () {
        var self = this, cv = this.cv;
        cv.addEventListener('pointerdown', function (e) {
            self._drag = { x: e.clientX, y: e.clientY, yaw: self.view.yaw, pitch: self.view.pitch, moved: 0 };
            cv.classList.add('drag');
            try { cv.setPointerCapture(e.pointerId); } catch (err) { /* not fatal */ }
        });
        cv.addEventListener('pointermove', function (e) {
            var r = cv.getBoundingClientRect();
            if (self._drag) {
                var dx = e.clientX - self._drag.x, dy = e.clientY - self._drag.y;
                self._drag.moved = Math.max(self._drag.moved, Math.abs(dx) + Math.abs(dy));
                self.view.yaw = self._drag.yaw + dx * 0.0075;
                self.view.pitch = Math.max(-1.45, Math.min(1.45, self._drag.pitch + dy * 0.0075));
                self._dirty = true;
                return;
            }
            var mx = e.clientX - r.left, my = e.clientY - r.top;
            var best = null, bd = 1e9;
            for (var i = 0; i < self.hit.length; i++) {
                var h = self.hit[i];
                var d = (h.x - mx) * (h.x - mx) + (h.y - my) * (h.y - my);
                if (d < h.r * h.r * 4 && d < bd) { bd = d; best = h; }
            }
            var changed = (best && best.data) !== self.hover;
            self.hover = best ? best.data : null;
            if (changed) self._dirty = true;
            if (self.onHover) self.onHover(self.hover, mx, my);
        });
        function end() {
            if (self._drag && self._drag.moved < 4 && self.hover && self.onClick) self.onClick(self.hover);
            self._drag = null; cv.classList.remove('drag');
        }
        cv.addEventListener('pointerup', end);
        cv.addEventListener('pointercancel', end);
        cv.addEventListener('pointerleave', function () {
            self._drag = null; cv.classList.remove('drag');
            if (self.hover) { self.hover = null; self._dirty = true; }
            if (self.onHover) self.onHover(null, 0, 0);
        });
        cv.addEventListener('wheel', function (e) {
            e.preventDefault();
            self.view.dist = Math.max(1.7, Math.min(9, self.view.dist + (e.deltaY > 0 ? 0.28 : -0.28)));
            self._dirty = true;
        }, { passive: false });
    };

    /* ── wireframe box + floor grid + axis ticks ──────────────────────────
       axes: [{name, domain:[lo,hi], fmt?}, …] for x, y, z. Ticks skip the
       shared (-1,-1,-1) corner — three sets of labels land on top of each
       other there. */
    Wolf3D.prototype.drawBox = function (axes) {
        var ctx = this.ctx, self = this;
        var C = [];
        [-1, 1].forEach(function (x) { [-1, 1].forEach(function (y) { [-1, 1].forEach(function (z) { C.push([x, y, z]); }); }); });
        var E = [[0,1],[0,2],[0,4],[1,3],[1,5],[2,3],[2,6],[3,7],[4,5],[4,6],[5,7],[6,7]];
        ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(255,255,255,.07)';
        E.forEach(function (e) {
            var a = self.project(C[e[0]][0], C[e[0]][1], C[e[0]][2]);
            var b = self.project(C[e[1]][0], C[e[1]][1], C[e[1]][2]);
            if (!a || !b) return;
            ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        });
        ctx.strokeStyle = 'rgba(255,255,255,.045)';
        for (var i = 1; i < 4; i++) {
            var t = -1 + i * 0.5;
            var p1 = self.project(t, -1, -1), p2 = self.project(t, -1, 1);
            var p3 = self.project(-1, -1, t), p4 = self.project(1, -1, t);
            if (p1 && p2) { ctx.beginPath(); ctx.moveTo(p1.x,p1.y); ctx.lineTo(p2.x,p2.y); ctx.stroke(); }
            if (p3 && p4) { ctx.beginPath(); ctx.moveTo(p3.x,p3.y); ctx.lineTo(p4.x,p4.y); ctx.stroke(); }
        }
        if (!axes) return;
        var specs = [
            { a: axes[0], at: function (t) { return [t, -1, -1]; }, off: [0, 15],  tOff: [0, 38] },
            { a: axes[1], at: function (t) { return [-1, t, -1]; }, off: [-26, 0], tOff: [-58, -14] },
            { a: axes[2], at: function (t) { return [-1, -1, t]; }, off: [26, 13], tOff: [64, 34] }
        ];
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        specs.forEach(function (s) {
            if (!s.a) return;
            ctx.font = '600 10px Inter, sans-serif';
            ctx.fillStyle = 'rgba(138,153,173,.8)';
            [-0.45, 0.3, 1].forEach(function (t) {
                var c = s.at(t), p = self.project(c[0], c[1], c[2]);
                if (!p || !s.a.domain) return;
                var val = s.a.domain[0] + (t + 1) / 2 * (s.a.domain[1] - s.a.domain[0]);
                var txt = s.a.fmt ? s.a.fmt(val)
                        : Math.abs(val) >= 100 ? val.toFixed(0)
                        : Math.abs(val) >= 10 ? val.toFixed(1) : val.toFixed(2);
                ctx.fillText(txt, p.x + s.off[0], p.y + s.off[1]);
            });
            var m = s.at(0.1), pm = self.project(m[0], m[1], m[2]);
            if (pm) {
                ctx.font = '800 11px Inter, sans-serif';
                ctx.fillStyle = 'rgba(0,210,255,.9)';
                ctx.fillText(s.a.name, pm.x + s.tOff[0], pm.y + s.tOff[1]);
            }
        });
    };

    /* ── quad-mesh surface, painter's algorithm ───────────────────────────
       h(i,j) returns a height in [-1,1] or null for "no data" (that quad is
       skipped rather than drawn flat at zero). col(i,j,height) → fillStyle. */
    Wolf3D.prototype.drawSurface = function (nx, nz, h, col, wire) {
        var ctx = this.ctx, self = this, quads = [];
        function pos(i, j) {
            var y = h(i, j);
            if (y == null) return null;
            return self.project(-1 + 2 * i / nx, y, -1 + 2 * j / nz);
        }
        for (var i = 0; i < nx; i++) {
            for (var j = 0; j < nz; j++) {
                var a = pos(i, j), b = pos(i + 1, j), c = pos(i + 1, j + 1), d = pos(i, j + 1);
                if (!a || !b || !c || !d) continue;
                quads.push({ p: [a, b, c, d], z: (a.z + b.z + c.z + d.z) / 4, i: i, j: j,
                             h: (h(i, j) + h(i + 1, j + 1)) / 2 });
            }
        }
        quads.sort(function (u, v) { return v.z - u.z; });
        quads.forEach(function (q) {
            ctx.beginPath();
            ctx.moveTo(q.p[0].x, q.p[0].y);
            for (var k = 1; k < 4; k++) ctx.lineTo(q.p[k].x, q.p[k].y);
            ctx.closePath();
            ctx.fillStyle = col(q.i, q.j, q.h);
            ctx.fill();
            if (wire) { ctx.strokeStyle = wire; ctx.lineWidth = 0.6; ctx.stroke(); }
        });
        return quads.length;
    };

    /* ── labels that refuse to overlap ── */
    Wolf3D.prototype.labelPlacer = function (limit) {
        var ctx = this.ctx, taken = [], n = 0;
        return function (txt, x, y) {
            if (n >= limit) return false;
            var w = ctx.measureText(txt).width + 6, h = 13;
            var r = { a: x - w / 2, b: x + w / 2, c: y - h, d: y };
            for (var i = 0; i < taken.length; i++) {
                var t = taken[i];
                if (r.a < t.b && r.b > t.a && r.c < t.d && r.d > t.c) return false;
            }
            taken.push(r); n++;
            return true;
        };
    };

    /* percentile domain — min/max lets two outliers squash everything else */
    Wolf3D.domain = function (values, loP, hiP, padP) {
        var v = values.filter(function (x) { return x != null && !isNaN(x); })
                      .sort(function (a, b) { return a - b; });
        if (!v.length) return [0, 1];
        var lo = v[Math.floor(v.length * (loP == null ? 0.02 : loP))];
        var hi = v[Math.floor(v.length * (hiP == null ? 0.98 : hiP))];
        if (hi - lo < 1e-9) { lo -= 0.5; hi += 0.5; }
        var pad = (hi - lo) * (padP == null ? 0.05 : padP);
        return [lo - pad, hi + pad];
    };

    /* normalise into the box and clamp — out-of-domain points pin to the wall
       instead of flying off-screen */
    Wolf3D.norm = function (v, d) {
        var n = ((v - d[0]) / (d[1] - d[0])) * 2 - 1;
        return n < -1 ? -1 : n > 1 ? 1 : n;
    };

    window.Wolf3D = Wolf3D;
})();
