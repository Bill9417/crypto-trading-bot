"""📡 資料來源狀態 — the layer that notices a failed API call on the cards' behalf.

An audit on 2026-08-21 found 39 of this app's 67 fetch() sites either swallow
the error (`.catch(function(){})`) or never check `r.ok`. Both render the same
thing: an empty card, indistinguishable from a card whose answer is genuinely
empty. That ambiguity cost real time twice in two days.

THE TESTS RUN THE ACTUAL JAVASCRIPT. Asserting that the file contains the string
"404" would pass against a file that never classifies anything — this repo has
shipped that kind of test and had to replace it three times in one week. node is
already installed; a minimal DOM shim is enough to exercise the real logic, and
the one thing that matters most — that wrapping fetch does not CHANGE fetch — is
only checkable by running it.
"""
import json
import os
import shutil
import subprocess
import textwrap

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(APP_DIR, "static", "feedhealth.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not installed")

SHIM = """
// Minimal DOM. Only what feedhealth.js touches.
const listeners = {};
const made = [];
function mkEl(tag) {
  return { tagName: tag, style: { cssText: "" }, innerHTML: "", children: [],
           setAttribute(){}, addEventListener(t,f){ this['on'+t]=f; },
           appendChild(c){ this.children.push(c); } };
}
global.document = {
  body: mkEl("body"),
  createElement(tag) {
    // <a href> is used to resolve a URL against the current origin.
    if (tag === "a") {
      const a = {};
      Object.defineProperty(a, "href", {
        set(v) {
          const m = /^https?:\\/\\/([^/]+)(\\/[^?#]*)?/.exec(v);
          if (m) { a.host = m[1]; a.pathname = m[2] || "/"; }
          else { a.host = "app.test"; a.pathname = (v || "").split("?")[0]; }
        },
        get() { return ""; }
      });
      return a;
    }
    const e = mkEl(tag); made.push(e); return e;
  },
  addEventListener(t, f) { listeners[t] = f; },
  hidden: false,
};
global.location = { host: "app.test" };
global.window = global;

// The fetch we are wrapping. Controlled per-call by the test.
let NEXT = null;
global.__setNext = (v) => { NEXT = v; };
global.fetch = function (url) {
  const n = NEXT;
  if (n && n.throwSync) throw new Error("sync boom");
  if (n && n.reject) {
    const e = new Error(n.reject);
    if (n.reject === "AbortError") e.name = "AbortError";
    return Promise.reject(e);
  }
  return Promise.resolve({ ok: (n ? n.status : 200) < 400,
                           status: n ? n.status : 200,
                           marker: "ORIGINAL_RESPONSE", url });
};
"""


def _run(body):
    """Run feedhealth.js under the shim plus `body`, return the JSON it prints."""
    with open(SCRIPT, encoding="utf-8") as f:
        src = f.read()
    prog = SHIM + "\n" + src + "\n(async () => {\n" + textwrap.dedent(body) + \
        "\n})().then(o => console.log(JSON.stringify(o)))" \
        ".catch(e => { console.log(JSON.stringify({__error: String(e)})); });"
    p = subprocess.run(["node", "-e", prog], capture_output=True, text=True,
                       timeout=30)
    assert p.returncode == 0, f"node failed:\n{p.stderr}"
    out = p.stdout.strip().split("\n")[-1]
    d = json.loads(out)
    assert "__error" not in d, d["__error"]
    return d


# ── the thing that must not change ───────────────────────────────────────────
def test_wrapping_fetch_does_not_change_what_fetch_returns():
    """Every card's own .then/.catch must still see exactly what it saw before.
    If this ever fails, the monitor has become a bug rather than a detector."""
    out = _run("""
        __setNext({status: 200});
        const ok = await fetch('/api/x');
        __setNext({status: 500});
        const bad = await fetch('/api/y');
        let caught = null;
        __setNext({reject: 'offline'});
        try { await fetch('/api/z'); } catch (e) { caught = e.message; }
        return {marker: ok.marker, okFlag: ok.ok, badStatus: bad.status,
                badMarker: bad.marker, caught: caught};
    """)
    assert out["marker"] == "ORIGINAL_RESPONSE"
    assert out["okFlag"] is True
    # A non-ok response must still RESOLVE, not reject — that is fetch's
    # contract, and turning it into a rejection would break every call site.
    assert out["badStatus"] == 500 and out["badMarker"] == "ORIGINAL_RESPONSE"
    # A network failure must still reject with the same error.
    assert out["caught"] == "offline"


def test_a_synchronous_throw_is_still_thrown():
    out = _run("""
        __setNext({throwSync: true});
        let msg = null;
        try { fetch('/api/x'); } catch (e) { msg = e.message; }
        return {msg: msg};
    """)
    assert out["msg"] == "sync boom"


# ── classification ───────────────────────────────────────────────────────────
def test_it_names_a_404_as_needing_a_restart():
    """The exact confusion this exists for: app.py does not hot-reload, so a
    new route 404s until someone restarts. 'no data yet' is the wrong story."""
    out = _run("""
        __setNext({status: 404});
        await fetch('/api/daily_watch'); await fetch('/api/daily_watch');
        const b = window.wolfFeedHealth.broken();
        return {n: b.length, path: b[0].path,
                reason: window.wolfFeedHealth.reason(b[0].f)};
    """)
    assert out["n"] == 1 and out["path"] == "/api/daily_watch"
    assert "restart" in out["reason"]


def test_the_three_failure_kinds_read_differently():
    out = _run("""
        __setNext({status: 404});  await fetch('/api/a'); await fetch('/api/a');
        __setNext({status: 500});  await fetch('/api/b'); await fetch('/api/b');
        __setNext({reject: 'down'});
        for (const p of ['/api/c','/api/c']) { try { await fetch(p); } catch(e){} }
        const r = {};
        window.wolfFeedHealth.broken().forEach(b =>
            r[b.path] = window.wolfFeedHealth.reason(b.f));
        return r;
    """)
    assert "restart" in out["/api/a"]
    assert "伺服器錯誤" in out["/api/b"]
    assert "連不上" in out["/api/c"]
    assert len({out["/api/a"], out["/api/b"], out["/api/c"]}) == 3


# ── when it stays quiet ──────────────────────────────────────────────────────
def test_one_bad_poll_does_not_raise_the_alarm():
    """Every restart produces a burst of failures. A badge that fires on the
    first one is a badge people learn to ignore."""
    out = _run("""
        __setNext({status: 500});
        await fetch('/api/a');
        return {afterOne: window.wolfFeedHealth.broken().length,
                display: document.body.children.length
                    ? document.body.children[0].style.cssText.indexOf('display:none') >= 0
                    : true};
    """)
    assert out["afterOne"] == 0, "alarmed on a single failure"


def test_recovery_clears_it_completely():
    """A feed that answers is healthy. Carrying old failures forward would keep
    the badge up after the cure — which is worse than never showing it."""
    out = _run("""
        __setNext({status: 500});
        await fetch('/api/a'); await fetch('/api/a'); await fetch('/api/a');
        const bad = window.wolfFeedHealth.broken().length;
        __setNext({status: 200});
        await fetch('/api/a');
        return {bad: bad, after: window.wolfFeedHealth.broken().length};
    """)
    assert out["bad"] == 1 and out["after"] == 0


def test_an_aborted_request_is_not_an_outage():
    """Navigating away aborts in-flight requests. Counting those would light
    the badge on every page change."""
    out = _run("""
        __setNext({reject: 'AbortError'});
        for (let i = 0; i < 4; i++) { try { await fetch('/api/a'); } catch(e){} }
        return {n: window.wolfFeedHealth.broken().length};
    """)
    assert out["n"] == 0


def test_it_only_watches_this_app_s_api():
    """A third-party or static request failing is not this indicator's business,
    and counting it would make the badge cry wolf."""
    out = _run("""
        __setNext({status: 500});
        await fetch('/static/app.css'); await fetch('/static/app.css');
        await fetch('https://other.example/api/x');
        await fetch('https://other.example/api/x');
        await fetch('/login'); await fetch('/login');
        return {n: window.wolfFeedHealth.broken().length};
    """)
    assert out["n"] == 0


def test_it_does_not_grow_without_bound():
    out = _run("""
        __setNext({status: 500});
        for (let i = 0; i < 200; i++) { await fetch('/api/p' + i); }
        return {tracked: Object.keys(window.wolfFeedHealth.feeds).length};
    """)
    assert out["tracked"] <= 60


# ── wiring ───────────────────────────────────────────────────────────────────
def test_the_nav_loads_it_before_any_page_script():
    """It wraps window.fetch, so a deferred load would miss every call made by
    a page script that runs first."""
    with open(os.path.join(APP_DIR, "templates", "_nav.html"), encoding="utf-8") as f:
        nav = f.read()
    i = nav.index("feedhealth.js")
    tag_start = nav.rindex("<script", 0, i)
    tag = nav[tag_start:nav.index(">", i)]
    assert "defer" not in tag and "async" not in tag, \
        "loaded late — page scripts would fetch before fetch is wrapped"
    assert "?v=" in tag, "no cache-bust: browsers pin static files for 7 days"


def test_it_survives_a_page_with_no_fetch():
    """Some pages never call fetch. The module must not throw at load."""
    out = _run("return {ok: typeof window.wolfFeedHealth === 'object'};")
    assert out["ok"] is True
