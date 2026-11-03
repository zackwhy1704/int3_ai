#!/usr/bin/env python3
"""Ingress auth proxy in front of ONE user's gated Hermes backend (launcher mode).

Stock Hermes Desktop authenticates to a remote backend with a static session token: an
``X-Hermes-Session-Token`` header on HTTP and ``?token=`` on the WebSocket upgrade
(hermes-agent apps/desktop/electron/connection-config.ts:13-14, 88). The backend reads that
token once at start from HERMES_DASHBOARD_SESSION_TOKEN (hermes_cli/web_server.py:224 at
v0.22.6), so it cannot rotate.

This proxy makes the client-side token short-lived and revocable WITHOUT modifying Hermes:

  * The token the desktop holds is a launcher DESKTOP token (12 h, one device, one backend).
  * For every request that carries a token (header and/or ?token=), the proxy asks the
    launcher API (/launcher/introspect, authenticated with this ingress's own secret, which
    also pins the backend) whether it is live. Live -> the token is REPLACED by the backend's
    internal token and forwarded. Anything else -> 401, and the backend never sees it.
  * A request with NO token is forwarded untouched: Hermes itself answers its public routes
    (the desktop's /api/status auth-mode probe sends no token) and 401s the rest. The internal
    token is never injected into a request that did not present a live desktop token.
  * Open WebSockets are re-checked every SWEEP_SECONDS; a revoked or expired token's sockets
    are closed.

Positive introspection results are cached for CACHE_SECONDS. If the launcher API cannot be
reached, new requests fail closed (503); already-open sockets are left open until a check
succeeds (a transient API outage must not drop every chat).

The internal token is held only by this process and the backend. Tokens are never logged.
"""
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("INGRESS_UPSTREAM", "http://127.0.0.1:9122")
LISTEN_PORT = int(os.environ.get("INGRESS_PORT", "9131"))
INTERNAL_TOKEN = os.environ.get("INGRESS_INTERNAL_TOKEN", "")
INTROSPECT_URL = os.environ.get("INGRESS_INTROSPECT_URL", "")
INGRESS_SECRET = os.environ.get("INGRESS_SECRET", "")
CACHE_SECONDS = float(os.environ.get("INGRESS_CACHE_SECONDS", "30"))
SWEEP_SECONDS = float(os.environ.get("INGRESS_SWEEP_SECONDS", "30"))

HEADER = "x-hermes-session-token"
# Hop-by-hop headers must not be forwarded; Host is rewritten by urllib.
_SKIP_REQ = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade", HEADER}
_SKIP_RESP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}


class IntrospectUnavailable(Exception):
    pass


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Checker:
    """Introspection with a short positive cache. Negative answers are never cached."""

    def __init__(self, url: str, secret: str, cache_seconds: float) -> None:
        self.url, self.secret, self.cache_seconds = url, secret, cache_seconds
        self._cache: dict[str, float] = {}
        self._lock = threading.Lock()

    def live(self, token: str, *, fresh: bool = False) -> bool:
        k = _key(token)
        now = time.monotonic()
        if not fresh:
            with self._lock:
                until = self._cache.get(k)
            if until is not None and until > now:
                return True
        req = urllib.request.Request(
            self.url,
            data=json.dumps({"token": token}).encode(),
            method="POST",
            headers={"content-type": "application/json", "authorization": f"Bearer {self.secret}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                active = bool(json.loads(resp.read()).get("active"))
        except urllib.error.HTTPError as exc:
            raise IntrospectUnavailable(f"introspect HTTP {exc.code}") from None
        except Exception as exc:
            raise IntrospectUnavailable(f"introspect unreachable: {type(exc).__name__}") from None
        with self._lock:
            if active:
                if len(self._cache) > 4096:
                    self._cache.clear()
                self._cache[k] = now + self.cache_seconds
            else:
                self._cache.pop(k, None)
        return active


class Sockets:
    """Open WebSocket splices per presented token, for the revocation sweep."""

    def __init__(self) -> None:
        self._by_token: dict[str, tuple[str, set]] = {}
        self._lock = threading.Lock()

    def add(self, token: str, pair) -> None:
        with self._lock:
            self._by_token.setdefault(_key(token), (token, set()))[1].add(pair)

    def remove(self, token: str, pair) -> None:
        with self._lock:
            entry = self._by_token.get(_key(token))
            if entry:
                entry[1].discard(pair)
                if not entry[1]:
                    del self._by_token[_key(token)]

    def snapshot(self) -> list[tuple[str, list]]:
        with self._lock:
            return [(tok, list(pairs)) for tok, pairs in self._by_token.values()]


def _close(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def sweep_once(checker: Checker, sockets: Sockets) -> int:
    """Close every socket whose token is no longer live. Returns how many were closed."""
    closed = 0
    for token, pairs in sockets.snapshot():
        try:
            live = checker.live(token, fresh=True)
        except IntrospectUnavailable:
            continue  # keep open on a transient outage; re-checked next sweep
        if not live:
            for client, upstream in pairs:
                _close(client)
                _close(upstream)
                closed += 1
    return closed


def make_handler(checker: Checker, sockets: Sockets, upstream: str, internal_token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # never log request lines: they may carry ?token=
            return

        def _reply(self, status: int, detail: str) -> None:
            body = json.dumps({"detail": detail}).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _presented(self) -> tuple[list[str], urllib.parse.SplitResult, list]:
            parts = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            found = [v for v in self.headers.get_all(HEADER) or []]
            found += [v for k, v in query if k == "token"]
            return found, parts, query

        def _authorize(self) -> tuple[str, str | None] | None:
            """(path to forward, the presented token or None). None = already answered."""
            found, parts, query = self._presented()
            if not found:
                return self.path, None
            token = found[0]
            if any(v != token for v in found):
                self._reply(401, "conflicting session tokens")
                return None
            try:
                ok = checker.live(token)
            except IntrospectUnavailable:
                self._reply(503, "sign-in service unreachable; try again")
                return None
            if not ok:
                self._reply(401, "session expired or revoked; relaunch from Company Brain")
                return None
            if not any(k == "token" for k, _ in query):
                return self.path, token  # header-only: leave the request target byte-identical
            query = [(k, internal_token if k == "token" else v) for k, v in query]
            path = urllib.parse.urlunsplit(("", "", parts.path, urllib.parse.urlencode(query), ""))
            return path, token

        def _proxy(self) -> None:
            authorized = self._authorize()
            if authorized is None:
                return
            path, token = authorized
            if self.headers.get("upgrade", "").lower() == "websocket":
                self._splice_websocket(path, token)
                return
            length = int(self.headers.get("content-length") or 0)
            payload = self.rfile.read(length) if length else None
            req = urllib.request.Request(upstream + path, data=payload, method=self.command)
            for name, value in self.headers.items():
                if name.lower() not in _SKIP_REQ:
                    req.add_header(name, value)
            if token is not None:
                req.add_header("X-Hermes-Session-Token", internal_token)
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    self._relay(resp.status, resp.headers, resp.read())
            except urllib.error.HTTPError as exc:
                self._relay(exc.code, exc.headers, exc.read())
            except Exception:
                self._reply(502, "backend unreachable")

        def _relay(self, status, headers, data: bytes) -> None:
            self.send_response(status)
            for name, value in headers.items():
                if name.lower() not in _SKIP_RESP:
                    self.send_header(name, value)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _splice_websocket(self, path: str, token: str | None) -> None:
            target = urllib.parse.urlsplit(upstream)
            try:
                up = socket.create_connection((target.hostname, target.port or 80), timeout=30)
            except OSError:
                self._reply(502, "backend unreachable")
                return
            up.settimeout(None)
            head = [f"{self.command} {path} {self.request_version}"]
            head += [f"{n}: {v}" for n, v in self.headers.items() if n.lower() != HEADER]
            if token is not None:
                head.append(f"X-Hermes-Session-Token: {internal_token}")
            up.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
            client = self.connection
            client.settimeout(None)
            pair = (client, up)
            if token is not None:
                sockets.add(token, pair)

            def pump(src: socket.socket, dst: socket.socket) -> None:
                try:
                    while chunk := src.recv(65536):
                        dst.sendall(chunk)
                except OSError:
                    pass
                finally:
                    _close(src)
                    _close(dst)

            back = threading.Thread(target=pump, args=(up, client), daemon=True)
            back.start()
            pump(client, up)
            back.join()
            if token is not None:
                sockets.remove(token, pair)
            self.close_connection = True

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _proxy

    return Handler


def serve(port: int, checker: Checker, sockets: Sockets, upstream: str, internal_token: str,
          sweep_seconds: float) -> ThreadingHTTPServer:
    """Start serving in background threads; returns the server (tests call .shutdown())."""
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(checker, sockets, upstream, internal_token))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def sweeper() -> None:
        while True:
            time.sleep(sweep_seconds)
            sweep_once(checker, sockets)

    threading.Thread(target=sweeper, daemon=True).start()
    return server


if __name__ == "__main__":
    missing = [n for n, v in (("INGRESS_INTERNAL_TOKEN", INTERNAL_TOKEN),
                              ("INGRESS_INTROSPECT_URL", INTROSPECT_URL),
                              ("INGRESS_SECRET", INGRESS_SECRET)) if not v]
    if missing:
        sys.exit(f"ingress_auth: missing {', '.join(missing)} (refusing to start)")
    serve(LISTEN_PORT, Checker(INTROSPECT_URL, INGRESS_SECRET, CACHE_SECONDS), Sockets(),
          UPSTREAM, INTERNAL_TOKEN, SWEEP_SECONDS)
    print(f"ingress_auth :{LISTEN_PORT} -> {UPSTREAM} (introspect {INTROSPECT_URL})",
          file=sys.stderr, flush=True)
    threading.Event().wait()
