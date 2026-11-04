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
