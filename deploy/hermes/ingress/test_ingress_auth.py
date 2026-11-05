"""Unit tests for ingress_auth.py (stdlib only; no Docker, no Hermes).

    python -m pytest deploy/hermes/ingress -q

A fake upstream records what reaches the "backend"; a fake /launcher/introspect answers from a
mutable set of live tokens; a raw-socket upstream stands in for the WebSocket.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import ingress_auth as ia

INTERNAL = "internal-backend-token-never-leaves-server"
SECRET = "cb_ing_test-secret"
LIVE = "cb_dt_live"
DEAD = "cb_dt_dead"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class World:
    def __init__(self) -> None:
        self.live = {LIVE}
        self.introspect_calls: list[dict] = []
        self.introspect_down = False
        self.seen: list[dict] = []          # requests that reached the upstream
        self.ws_heads: list[str] = []       # raw request heads of WS upgrades upstream


@pytest.fixture()
def world():
    w = World()

    class Introspect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            w.introspect_calls.append({"auth": self.headers.get("authorization"), "body": body,
                                       "path": self.path})
            if w.introspect_down:
                self.send_response(500); self.send_header("content-length", "0"); self.end_headers()
                return
            if self.headers.get("authorization") != f"Bearer {SECRET}":
                self.send_response(401); self.send_header("content-length", "0"); self.end_headers()
                return
            out = json.dumps({"active": body["token"] in w.live}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            w.seen.append({"path": self.path, "token": self.headers.get("x-hermes-session-token"),
                           "headers": dict(self.headers)})
            out = (b'{"auth_required":false,"version":"v9","install_id":"secret-id","active_sessions":3}'
                   if self.path == "/api/status" else b'{"ok":true}')
            self.send_response(200)
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        do_POST = do_GET

    ip, up_port, ws_port, port = _free_port(), _free_port(), _free_port(), _free_port()
    servers = [ThreadingHTTPServer(("127.0.0.1", ip), Introspect),
               ThreadingHTTPServer(("127.0.0.1", up_port), Upstream)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    w.upstream_servers = servers[1:]

    # Raw WebSocket stand-in: read the head, answer 101, then echo bytes.
    ws_listener = socket.socket()
    ws_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ws_listener.bind(("127.0.0.1", ws_port))
    ws_listener.listen()

    def ws_serve():
        while True:
            try:
                conn, _ = ws_listener.accept()
            except OSError:
                return

            def handle(c):
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = c.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                w.ws_heads.append(buf.split(b"\r\n\r\n")[0].decode("latin-1"))
                c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          b"Connection: Upgrade\r\n\r\n")
                try:
                    while d := c.recv(4096):
                        c.sendall(d)
                except OSError:
                    pass
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=ws_serve, daemon=True).start()

    checker = ia.Checker(f"http://127.0.0.1:{ip}/launcher/introspect", SECRET, cache_seconds=0.3)
    sockets = ia.Sockets()
    w.checker, w.sockets = checker, sockets
    w.http = ia.serve(port, checker, sockets, f"http://127.0.0.1:{up_port}", INTERNAL, sweep_seconds=3600)
    w.ws = ia.serve(_free_port(), checker,
                    sockets, f"http://127.0.0.1:{ws_port}", INTERNAL, sweep_seconds=3600)
    w.base = f"http://127.0.0.1:{port}"
    w.ws_addr = ("127.0.0.1", w.ws.server_address[1])
    yield w
    for s in [servers[0], w.http, w.ws] + ([] if getattr(w, "upstream_stopped", False) else servers[1:]):
        s.shutdown()
    ws_listener.close()


def _get(url: str, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_tokenless_status_returns_only_what_the_desktop_probe_needs(world):
    status, body = _get(world.base + "/api/status")
    assert status == 200
    assert json.loads(body) == {"auth_required": False}
    assert world.seen[-1]["token"] is None
    assert world.introspect_calls == []


def test_tokenless_status_reports_a_dead_backend(world):
    for s in world.upstream_servers:
        s.shutdown()
        s.server_close()
    world.upstream_stopped = True
    status, _ = _get(world.base + "/api/status")
    assert status == 502


def test_a_live_token_gets_the_full_status(world):
    status, body = _get(world.base + "/api/status", {"X-Hermes-Session-Token": LIVE})
    assert status == 200
    assert json.loads(body)["install_id"] == "secret-id"
    assert world.seen[-1]["token"] == INTERNAL


@pytest.mark.parametrize("path", ["/api/health", "/api/model/info", "/api/config/schema",
                                  "/api/config/defaults", "/api/messaging/platforms", "/", "/.env",
                                  "/api/status/", "/api/status/x"])
def test_everything_else_needs_a_token_and_never_reaches_the_backend(world, path):
    status, _ = _get(world.base + path)
    assert status == 401
    assert world.seen == []


def test_tokenless_writes_to_status_are_refused(world):
    req = urllib.request.Request(world.base + "/api/status", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
    assert world.seen == []


def test_tokenless_websocket_is_refused(world):
    s = socket.create_connection(world.ws_addr, timeout=5)
    s.sendall(b"GET /api/ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n\r\n")
    assert b" 401 " in s.recv(4096).split(b"\r\n")[0]
    assert world.ws_heads == []
    s.close()


def test_live_header_token_is_swapped_for_the_internal_token(world):
    status, _ = _get(world.base + "/api/sessions/x/messages", {"X-Hermes-Session-Token": LIVE})
    assert status == 200
    assert world.seen[-1]["token"] == INTERNAL
    assert LIVE not in json.dumps(world.seen[-1]["headers"])


def test_dead_token_is_401_and_never_reaches_the_backend(world):
    status, body = _get(world.base + "/api/config", {"X-Hermes-Session-Token": DEAD})
    assert status == 401
    assert b"relaunch from int3" in body
    assert world.seen == []


def test_the_internal_token_itself_is_useless_from_outside(world):
    status, _ = _get(world.base + "/api/config", {"X-Hermes-Session-Token": INTERNAL})
    assert status == 401
    assert world.seen == []


def test_query_token_is_swapped_and_other_params_kept(world):
    status, _ = _get(world.base + f"/api/fs/list?path=%2Fopt%2Fdata&token={LIVE}")
    assert status == 200
    assert world.seen[-1]["path"].startswith("/api/fs/list?")
    assert f"token={INTERNAL}" in world.seen[-1]["path"]
    assert "path=%2Fopt%2Fdata" in world.seen[-1]["path"]
    assert LIVE not in world.seen[-1]["path"]


def test_header_and_query_must_agree(world):
    status, _ = _get(world.base + f"/api/config?token={DEAD}", {"X-Hermes-Session-Token": LIVE})
    assert status == 401
    assert world.seen == []


def test_introspect_gets_the_ingress_secret_and_the_token_in_the_body_not_the_url(world):
    _get(world.base + "/api/config", {"X-Hermes-Session-Token": LIVE})
    call = world.introspect_calls[-1]
    assert call["auth"] == f"Bearer {SECRET}"
    assert call["body"] == {"token": LIVE}
    assert LIVE not in call["path"]


def test_introspect_unreachable_fails_closed(world):
    world.introspect_down = True
    status, _ = _get(world.base + "/api/config", {"X-Hermes-Session-Token": LIVE})
    assert status == 503
    assert world.seen == []


def test_positive_cache_then_revocation_takes_effect_after_expiry(world):
    _get(world.base + "/api/config", {"X-Hermes-Session-Token": LIVE})
    _get(world.base + "/api/config", {"X-Hermes-Session-Token": LIVE})
    assert len(world.introspect_calls) == 1          # second request served from cache
    world.live.discard(LIVE)
    time.sleep(0.4)                                  # > cache_seconds
    status, _ = _get(world.base + "/api/config", {"X-Hermes-Session-Token": LIVE})
    assert status == 401


def _ws_connect(addr, path: str) -> socket.socket:
    s = socket.create_connection(addr, timeout=5)
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += s.recv(4096)
    assert head.startswith(b"HTTP/1.1 101"), head
    return s


def test_websocket_upgrade_carries_the_internal_token_only(world):
    s = _ws_connect(world.ws_addr, f"/api/ws?token={LIVE}")
    s.sendall(b"ping")
    assert s.recv(4096) == b"ping"
    head = world.ws_heads[-1]
    assert f"/api/ws?token={INTERNAL}" in head.splitlines()[0]
    assert LIVE not in head
    s.close()


def test_websocket_with_dead_token_is_refused(world):
    s = socket.create_connection(world.ws_addr, timeout=5)
    s.sendall(f"GET /api/ws?token={DEAD} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n\r\n".encode())
    assert s.recv(4096).startswith(b"HTTP/1.1 401")
    assert world.ws_heads == []
    s.close()


def test_revocation_sweep_closes_an_open_websocket(world):
    s = _ws_connect(world.ws_addr, f"/api/ws?token={LIVE}")
    s.sendall(b"before")
    assert s.recv(4096) == b"before"
    world.live.discard(LIVE)
    assert ia.sweep_once(world.checker, world.sockets) == 1
    s.settimeout(3)
    assert s.recv(4096) == b""                       # proxy closed the client side


def test_sweep_keeps_sockets_open_while_introspect_is_down(world):
    s = _ws_connect(world.ws_addr, f"/api/ws?token={LIVE}")
    world.introspect_down = True
    assert ia.sweep_once(world.checker, world.sockets) == 0
    s.sendall(b"still")
    assert s.recv(4096) == b"still"
    s.close()
