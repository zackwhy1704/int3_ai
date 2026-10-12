#!/usr/bin/env python3
"""Default-deny egress proxy for a per-user Hermes container.

The Hermes container sits on a Docker --internal network: it has no route to
the internet at all. This proxy is the only thing on that network that also has
a leg on an outside network, so it is the container's ONLY way out.

It speaks HTTP CONNECT only (TLS tunnels). A CONNECT to a host:port that is not
on EGRESS_ALLOW gets 403 and no connection is ever opened. Plain-HTTP proxy
requests are refused outright — nothing we allow speaks cleartext.

Every attempt is logged, allowed or denied, so the audit trail shows what the
agent TRIED to reach, not only what it reached.

Deliberately not a general forward proxy: no caching, no rewriting, no plain
HTTP. The rule set is one environment variable.
"""
import os
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOW = {h.strip().lower() for h in os.environ.get("EGRESS_ALLOW", "").split(",") if h.strip()}
LISTEN = int(os.environ.get("EGRESS_PORT", "3128"))
LOG_PATH = os.environ.get("EGRESS_LOG", "/tmp/egress.log")
_lock = threading.Lock()


def log(verdict: str, target: str, peer: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {verdict:<7} {target}  (from {peer})"
    with _lock, open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _deny(self, code: int, why: str) -> None:
        body = why.encode()
        self.send_response(code)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        target = self.path.strip().lower()  # "host:port"
        peer = self.client_address[0]
        if target not in ALLOW:
            log("DENIED", target, peer)
            self._deny(403, f"egress to {target} is not on the allowlist\n")
            return
        host, _, port = target.rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)), timeout=20)
        except OSError as exc:
            log("FAILED", f"{target} ({exc})", peer)
            self._deny(502, f"upstream connect failed: {exc}\n")
            return
        log("ALLOWED", target, peer)
        self.send_response(200, "Connection Established")
        self.end_headers()
        client = self.connection
        sockets = [client, upstream]
        try:
            while True:
                readable, _, broken = select.select(sockets, [], sockets, 300)
                if broken or not readable:
                    break
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    (upstream if s is client else client).sendall(data)
        finally:
            upstream.close()

    def _refuse_plain(self):
        log("DENIED", f"plain-http {self.command} {self.path}", self.client_address[0])
        self._deny(403, "plain HTTP is not proxied; only CONNECT to allowlisted hosts\n")

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _refuse_plain


if __name__ == "__main__":
    print(f"egress_proxy :{LISTEN} allow={sorted(ALLOW) or 'NOTHING'}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", LISTEN), Handler).serve_forever()
