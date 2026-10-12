#!/usr/bin/env python3
"""Default-deny route proxy in front of the Hermes backend.

Two jobs:

  1. MEASURE (Task H). With ALLOWLIST unset it forwards everything and appends
     "METHOD /path" to the log, so the set of routes the product actually uses
     can be read off a real session instead of guessed from the source.

  2. ENFORCE. With ALLOWLIST set it is DEFAULT-DENY: a request whose path is not
     on the list gets 403 and never reaches the backend. A denylist was rejected
     on purpose — Hermes ships new routes every release, so "block
     /api/fs/write-text" would not survive the release that adds a second write
     route. Allowlisting means an unknown route fails closed.

Matching is on the PATH ONLY (query strings are ignored), against either an
exact path or a "prefix*" entry. Deliberately dumb: the whole point is that the
rule set is auditable in one screen.
"""
import os
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("PROXY_UPSTREAM", "http://127.0.0.1:9119")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "9122"))
LOG_PATH = os.environ.get("PROXY_LOG", "/tmp/routes.log")
ALLOWLIST_FILE = os.environ.get("ALLOWLIST_FILE", "").strip()


def _load_rules(path: str) -> list[tuple[str, str]]:
    """One rule per line: ``METHOD /path`` or ``METHOD /prefix*``; ``#`` comments.

    Method-aware on purpose: path-only matching would let ``POST /api/cron/jobs``
    (create a scheduled job) ride on an allowlisted ``GET /api/cron/jobs``.
    """
    rules = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            method, _, route = line.partition(" ")
            rules.append((method.strip().upper(), route.strip()))
    return rules


ALLOW = _load_rules(ALLOWLIST_FILE) if ALLOWLIST_FILE else []
ENFORCING = bool(ALLOWLIST_FILE)

_log_lock = threading.Lock()
# Hop-by-hop headers must not be forwarded; Host is rewritten by urllib.
_SKIP_REQ = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade"}
_SKIP_RESP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}


def permitted(method: str, path: str) -> bool:
    for rule_method, rule in ALLOW:
        if rule_method not in ("*", method):
            continue
        if rule.endswith("*"):
            if path.startswith(rule[:-1]):
                return True
        elif path == rule:
            return True
    return False


def record(line: str) -> None:
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep stderr clean; we keep our own log
        return

    def _proxy(self) -> None:
        path = self.path.split("?", 1)[0]
        record(f"{self.command} {path}")

        if ENFORCING and not permitted(self.command, path):
            record(f"  DENIED {self.command} {path}")
            body = b'{"detail":"route not on proxy allowlist"}'
            self.send_response(403)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # The desktop's gateway channel is a WebSocket (/api/ws). urllib cannot
        # carry an Upgrade, so an allowlisted upgrade is spliced as raw TCP: the
        # route decision above still applies; only the transport differs.
        if self.headers.get("upgrade", "").lower() == "websocket":
            self._splice_websocket()
            return

        length = int(self.headers.get("content-length") or 0)
        payload = self.rfile.read(length) if length else None
        req = urllib.request.Request(UPSTREAM + self.path, data=payload, method=self.command)
        for name, value in self.headers.items():
            if name.lower() not in _SKIP_REQ:
                req.add_header(name, value)

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for name, value in resp.headers.items():
                    if name.lower() not in _SKIP_RESP:
                        self.send_header(name, value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as exc:
            data = exc.read()
            self.send_response(exc.code)
            for name, value in exc.headers.items():
                if name.lower() not in _SKIP_RESP:
                    self.send_header(name, value)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:  # upstream down / timeout
            body = f'{{"detail":"proxy upstream error: {exc}"}}'.encode()
            self.send_response(502)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _splice_websocket(self) -> None:
        target = urllib.parse.urlsplit(UPSTREAM)
        upstream = socket.create_connection((target.hostname, target.port or 80), timeout=30)
        upstream.settimeout(None)

        head = [f"{self.command} {self.path} {self.request_version}"]
        head += [f"{name}: {value}" for name, value in self.headers.items()]
        upstream.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))

        client = self.connection
        client.settimeout(None)

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while chunk := src.recv(65536):
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        back = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        back.start()
        pump(client, upstream)
        back.join()
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _proxy


if __name__ == "__main__":
    mode = f"ENFORCING ({len(ALLOW)} rules from {ALLOWLIST_FILE})" if ENFORCING else "MEASURING (allow all)"
    print(f"route_proxy {mode} :{LISTEN_PORT} -> {UPSTREAM}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
