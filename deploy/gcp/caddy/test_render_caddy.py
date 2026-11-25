"""python -m pytest deploy/gcp/caddy -q"""
import pytest

import render_caddy as rc

ROUTES = "0123456789abcdef\t8100\tlauncher\tlauncher-edge\nfedcba9876543210\t9500\tuser\tacme/priya\n"


def out(issuer="production"):
    return rc.render(rc.read_routes(ROUTES), "app.int3labs.app", issuer)


def test_https_sites_only_and_port_80_never_used():
    text = out()
    assert "https://0123456789abcdef.app.int3labs.app {" in text
    assert "http://" not in text.replace("https://", "")
    assert "auto_https disable_redirects" in text
    assert text.count("disable_http_challenge") == 2


def test_base_domain_is_only_the_setting():
    text = rc.render(rc.read_routes(ROUTES), "x.example.test", "production")
    assert "int3labs" not in text
    assert "https://fedcba9876543210.x.example.test {" in text


def test_host_is_rewritten_and_forwarded_headers_removed():
    text = out()
    assert "header_up Host 127.0.0.1:9500" in text
    for h in ("X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host"):
        assert f"header_up -{h}" in text


def test_no_access_log_and_labels_not_published():
    text = out()
    assert "log" not in text.replace("disable_redirects", "")
    assert "priya" not in text and "acme/" not in text


def test_staging_uses_staging_directory_only_when_asked():
    assert rc.STAGING not in out()
    assert rc.STAGING in out("staging")


@pytest.mark.parametrize("bad", [
    "not-hex\t9500\tuser\tl\n",
    "0123456789abcdef\tnan\tuser\tl\n",
    "0123456789abcdef\t9500\tuser\n",
    "0123456789abcdef\t1\tuser\ta\n0123456789abcdef\t2\tuser\tb\n",
    "0123456789abcdef\t1\tuser\ta\nfedcba9876543210\t1\tuser\tb\n",
])
def test_malformed_routes_refused(bad):
    with pytest.raises(ValueError):
        rc.read_routes(bad)


LOCAL_ROUTES = ROUTES + "1111111111111111\t8101\tlocal\tlauncher-edge-local\n2222222222222222\t8200\tmcp\tbrain-mcp-local\n"


def _site(text: str, rid: str) -> str:
    start = text.index(f"https://{rid}.app.int3labs.app {{")
    return text[start:text.index("\n}\n", start) + 3]


def test_local_mode_sites_stream_mcp_and_keep_its_session_header():
    """The brain MCP endpoint is stateful Streamable HTTP (deploy plan section 2): the proxy must pass the
    Mcp-Session-Id header both ways and must not buffer text/event-stream. Caddy's reverse_proxy does
    both by default (it flushes event streams at once, copies headers), so the site must not add
    anything that changes that: no flush_interval, no buffering, no header rule touching the session."""
    text = rc.render(rc.read_routes(LOCAL_ROUTES), "app.int3labs.app", "production")
    mcp = _site(text, "2222222222222222")
    assert "reverse_proxy 127.0.0.1:8200 {" in mcp and "header_up Host 127.0.0.1:8200" in mcp
    for banned in ("flush_interval", "buffer", "request_buffers", "response_buffers", "Mcp-Session-Id",
                   "header_down", "encode", "-Accept", "-Content-Type"):
        assert banned not in mcp, banned
    assert "reverse_proxy 127.0.0.1:8101 {" in _site(text, "1111111111111111")
    assert text.count("https://") == 4  # one site per route, nothing else published


def test_bad_inputs_refused():
    with pytest.raises(ValueError):
        rc.render([], "app.int3labs.app\n{", "production")
    with pytest.raises(ValueError):
        rc.render([], "app.int3labs.app", "plain")
