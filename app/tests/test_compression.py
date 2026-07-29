"""🗜 Response compression — the dashboard must not ship raw over the tunnel.

The home page renders ~500KB of HTML and /api/scan_data ~310KB; both were
being sent uncompressed on every load. `compress_response` gzips text
responses, which measured 86% fewer bytes across the site.

Compression is a correctness hazard if done sloppily: send a gzipped body to
a client that didn't ask for it and the page is garbage; forget `Vary` and a
shared cache replays it to such a client. These tests pin both, plus the
fail-soft promise that compression never turns a working page into a 500.
"""
import gzip

import app as APP

GZ = {"Accept-Encoding": "gzip, deflate"}


def _client():
    APP.app.config["TESTING"] = True
    return APP.app.test_client()


def test_html_is_gzipped_and_decodes_back_to_the_original():
    c = _client()
    plain = c.get("/login")
    packed = c.get("/login", headers=GZ)
    assert packed.headers["Content-Encoding"] == "gzip"
    # The body must survive the round trip byte-for-byte — /login is static
    # enough to compare directly (no timestamps in it).
    assert gzip.decompress(packed.get_data()) == plain.get_data()
    assert len(packed.get_data()) < len(plain.get_data())


def test_client_that_refuses_gzip_gets_a_plain_body():
    c = _client()
    r = c.get("/login", headers={"Accept-Encoding": "identity"})
    assert r.headers.get("Content-Encoding") is None
    assert b"<html" in r.get_data().lower()


def test_missing_accept_encoding_header_is_treated_as_no_gzip():
    c = _client()
    r = c.get("/login", environ_overrides={"HTTP_ACCEPT_ENCODING": ""})
    assert r.headers.get("Content-Encoding") is None


def test_vary_includes_accept_encoding_without_dropping_cookie():
    """A shared cache keys on Vary. Losing `Cookie` here would let one user's
    authenticated page be served to another — far worse than a missed gzip."""
    c = _client()
    r = c.get("/login", headers=GZ)
    vary = r.headers.get("Vary", "")
    assert "Accept-Encoding" in vary
    assert "Cookie" in vary


def test_content_length_matches_the_compressed_body():
    c = _client()
    r = c.get("/login", headers=GZ)
    assert int(r.headers["Content-Length"]) == len(r.get_data())


def test_tiny_responses_are_left_alone():
    """Below ~1KB gzip framing can make the payload bigger, not smaller."""
    c = _client()
    r = c.get("/manifest.webmanifest", headers=GZ)
    assert r.headers.get("Content-Encoding") is None


def test_static_assets_compress_despite_passthrough_mode():
    """send_from_directory hands back a file wrapper in direct_passthrough
    mode; reading it needs an explicit opt-out. Regression guard for that."""
    c = _client()
    r = c.get("/static/app.css", headers=GZ)
    assert r.headers["Content-Encoding"] == "gzip"
    assert b"--" in gzip.decompress(r.get_data())      # CSS custom properties


def test_static_requests_do_not_leak_file_descriptors():
    """Opting a passthrough response out of passthrough drains the file
    wrapper but does not close it. Left unclosed that is a slow FD leak on a
    server that runs for weeks — every /static hit costs one descriptor.
    """
    import gc
    import warnings

    c = _client()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(25):
            c.get("/static/app.css", headers=GZ)
            c.get("/manifest.webmanifest", headers=GZ)   # early-return path
        gc.collect()
    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not leaks, f"unclosed file handles: {[str(w.message) for w in leaks]}"


def test_compression_failure_degrades_to_an_uncompressed_page(monkeypatch):
    """Fail-soft: a broken compressor must not take the site down."""
    def boom(*a, **k):
        raise RuntimeError("compressor exploded")

    monkeypatch.setattr(APP.gzip, "compress", boom)
    c = _client()
    r = c.get("/login", headers=GZ)
    assert r.status_code == 200
    assert r.headers.get("Content-Encoding") is None
    assert b"<html" in r.get_data().lower()
