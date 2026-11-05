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


def test_bad_inputs_refused():
    with pytest.raises(ValueError):
        rc.render([], "app.int3labs.app\n{", "production")
    with pytest.raises(ValueError):
        rc.render([], "app.int3labs.app", "plain")
