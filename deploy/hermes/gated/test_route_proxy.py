"""Unit tests for route_proxy.py (stdlib only; no Docker, no Hermes).

    python -m pytest deploy/hermes/gated -q

A fake upstream records every request that reaches the "backend", so a stub can be shown to be
answered by the proxy alone. Rules are loaded from the REAL allowlist.txt, plus a few temp files
for the parser's refusals.
"""
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import route_proxy as rp

ALLOWLIST = Path(__file__).with_name("allowlist.txt")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def proxy(monkeypatch, tmp_path):
    seen: list[str] = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def _any(self):
            seen.append(f"{self.command} {self.path}")
            body = b'{"upstream": true}'
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = _any

    up = ThreadingHTTPServer(("127.0.0.1", _free_port()), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    monkeypatch.setattr(rp, "UPSTREAM", f"http://127.0.0.1:{up.server_port}")
    monkeypatch.setattr(rp, "ALLOW", rp._load_rules(str(ALLOWLIST)))
    monkeypatch.setattr(rp, "ENFORCING", True)
    monkeypatch.setattr(rp, "LOG_PATH", str(tmp_path / "routes.log"))
    srv = ThreadingHTTPServer(("127.0.0.1", _free_port()), rp.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()
    up.shutdown()


def _call(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_messaging_platforms_is_stubbed_and_never_reaches_backend(proxy):
    base, seen = proxy
    status, body = _call(base + "/api/messaging/platforms?profile=default")
    assert status == 200
    assert json.loads(body) == {"platforms": []}
    assert seen == []


def test_messaging_writes_and_pairing_stay_denied(proxy):
    base, seen = proxy
    assert _call(base + "/api/messaging/platforms/telegram", "PUT", b"{}")[0] == 403
    assert _call(base + "/api/messaging/platforms/telegram/test", "POST", b"")[0] == 403
    assert _call(base + "/api/messaging/platforms", "POST", b"{}")[0] == 403
    assert _call(base + "/api/pairing")[0] == 403
    assert seen == []


def test_ordinary_allowed_route_is_forwarded_and_write_text_denied(proxy):
    base, seen = proxy
    assert _call(base + "/api/fs/git-root?path=/opt/data")[0] == 200
    assert seen == ["GET /api/fs/git-root?path=/opt/data"]
    assert _call(base + "/api/fs/write-text", "POST", b"{}")[0] == 403


@pytest.mark.parametrize(
    "line",
    [
        'POST /api/x => {"ok": true}',      # a stubbed write would lie about success
        'GET /api/x* => {"ok": true}',      # stubs are exact paths only
        "GET /api/x => {not json",          # refuse to start on a malformed body
    ],
)
def test_parser_refuses_bad_stub_rules(tmp_path, line):
    f = tmp_path / "rules.txt"
    f.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        rp._load_rules(str(f))


def test_real_allowlist_has_exactly_one_stub():
    stubs = [(m, r) for m, r, b in rp._load_rules(str(ALLOWLIST)) if b is not None]
    assert stubs == [("GET", "/api/messaging/platforms")]


# ── the two launcher edges (deploy plan step 1: hosted and local mode never share a door) ──────────

HOSTED_EDGE = Path(__file__).with_name("launcher-edge.allowlist")
LOCAL_EDGE = Path(__file__).with_name("launcher-edge-local.allowlist")
ROUTES_PY = Path(__file__).resolve().parents[3] / "backend" / "app" / "launcher" / "routes.py"

SHARED = [("GET", "/launcher/healthz"), ("POST", "/launcher/auth/invite"), ("POST", "/launcher/auth/refresh"),
          ("POST", "/launcher/auth/revoke"), ("GET", "/launcher/devices"), ("POST", "/launcher/devices/d1/revoke"),
          ("GET", "/launcher/me"), ("GET", "/launcher/compat")]
LOCAL_ONLY = [("GET", "/launcher/local/policy"), ("POST", "/launcher/local/posture"),
              ("POST", "/launcher/local/launch"), ("GET", "/launcher/local/memory"),
              ("POST", "/launcher/local/memory/save"), ("POST", "/launcher/local/memory/recall"),
              ("POST", "/launcher/local/memory/forget"), ("POST", "/launcher/local/memory/delete")]
HOSTED_ONLY = [("POST", "/launcher/launch"), ("POST", "/launcher/byok/link")]
NEITHER = [("POST", "/launcher/introspect"), ("POST", "/v1/search"), ("POST", "/v1/brain_ask"),
           ("GET", "/api/auth/me"), ("GET", "/launcher/local/policy/../../v1/search")]


@pytest.fixture()
def edge(monkeypatch, tmp_path):
    """edge(path) -> (base url, upstream log): the real route_proxy with one of the real allowlists."""
    servers = []

    def start(allowlist: Path):
        seen: list[str] = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def _any(self):
                seen.append(f"{self.command} {self.path}")
                body = b'{"upstream": true}'
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _any

        up = ThreadingHTTPServer(("127.0.0.1", _free_port()), Upstream)
        threading.Thread(target=up.serve_forever, daemon=True).start()
        monkeypatch.setattr(rp, "UPSTREAM", f"http://127.0.0.1:{up.server_port}")
        monkeypatch.setattr(rp, "ALLOW", rp._load_rules(str(allowlist)))
        monkeypatch.setattr(rp, "ENFORCING", True)
        monkeypatch.setattr(rp, "LOG_PATH", str(tmp_path / "routes.log"))
        srv = ThreadingHTTPServer(("127.0.0.1", _free_port()), rp.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.extend([srv, up])
        return f"http://127.0.0.1:{srv.server_port}", seen

    yield start
    for s in servers:
        s.shutdown()


def _hits(base, routes):
    return {(m, p): _call(base + p, m, b"{}" if m == "POST" else None)[0] for m, p in routes}


def test_hosted_edge_refuses_every_local_mode_route(edge):
    base, seen = edge(HOSTED_EDGE)
    assert set(_hits(base, LOCAL_ONLY + NEITHER).values()) == {403}
    assert seen == []                                   # refused by the edge: never reached the brain
    assert _call(base + "/launcher/local/policy?x=1")[0] == 403  # query does not change the verdict
    assert set(_hits(base, SHARED + HOSTED_ONLY).values()) == {200}


def test_local_edge_refuses_hosted_launch_and_byok(edge):
    base, seen = edge(LOCAL_EDGE)
    assert set(_hits(base, HOSTED_ONLY + NEITHER).values()) == {403}
    assert seen == []
    assert set(_hits(base, SHARED + LOCAL_ONLY).values()) == {200}


def _allowed(allowlist: Path, method: str, path: str) -> bool:
    return any(m == method and (path == r or (r.endswith("*") and path.startswith(r[:-1])))
               for m, r, _ in rp._load_rules(str(allowlist)))


def test_every_launcher_route_is_on_the_right_edge():
    """Guards future routes: each @router route in backend/app/launcher/routes.py is on the hosted edge,
    the local edge, both, or (introspect) neither, by what it is for."""
    import re
    routes = re.findall(r'@router\.(get|post|put|delete|patch)\("([^"]+)"', ROUTES_PY.read_text(encoding="utf-8"))
    assert len(routes) >= 19
    for method, path in routes:
        m, p = method.upper(), "/launcher" + re.sub(r"\{[^}]+\}", "x", path)
        hosted, local = _allowed(HOSTED_EDGE, m, p), _allowed(LOCAL_EDGE, m, p)
        if p == "/launcher/introspect":
            want = (False, False)
        elif p.startswith("/launcher/local/"):
            want = (False, True)
        elif p in ("/launcher/launch", "/launcher/byok/link"):
            want = (True, False)
        else:
            want = (True, True)
        assert (hosted, local) == want, f"{m} {p}: hosted={hosted} local={local}, want {want}"
