#!/usr/bin/env python3
"""External check of the hosted path, from a machine outside the VM. Certificates are verified.

    <invite-link on stdin> python deploy/gcp/smoke-external.py chat "When are invoices payable?"
    <invite-link on stdin> python deploy/gcp/smoke-external.py idle 960

Redeems a single-use invite exactly as the launcher does, gets a desktop token for that user's own
backend, then either drives the real agent through the demo's hermes_chat.py over https/wss, or holds
one wss socket fully idle (no client pings) and proves it still works afterwards. Always revokes the
device at the end. Prints no invite or token. Needs: websockets.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERMES_CHAT = Path(__file__).resolve().parents[1] / "hermes" / "demo" / "hermes_chat.py"


def api(server: str, method: str, path: str, token: str | None = None, body: dict | None = None) -> dict:
    req = urllib.request.Request(server + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def sign_in() -> tuple[str, str, dict]:
    link = sys.stdin.read().strip()
    parts = urllib.parse.urlsplit(link)
    if parts.scheme != "https" or not parts.fragment.startswith("cb_inv_"):
        sys.exit("stdin must be an https invite link")
    server = f"https://{parts.netloc}"
    session = api(server, "POST", "/launcher/auth/invite",
                  body={"invite": parts.fragment, "label": "external smoke", "platform": "windows"})
    access = session["access_token"]
    grant = api(server, "POST", "/launcher/launch", token=access)
    print(f"[smoke] signed in over https; backend {grant['backend']['url']} label {grant['backend']['label']!r}")
    return server, access, grant


def chat(grant: dict, prompt: str) -> int:
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"url": grant["backend"]["url"], "session_token": grant["desktop_token"], "name": "smoke"}, f)
        return subprocess.call([sys.executable, str(HERMES_CHAT), "--connect", path, "--say", prompt,
                                "--turn-timeout", "180"])
    finally:
        os.unlink(path)


async def idle(grant: dict, seconds: int) -> int:
    import websockets
    base = grant["backend"]["url"].replace("https://", "wss://")
    url = f"{base}/api/ws?{urllib.parse.urlencode({'token': grant['desktop_token']})}"

    async def rpc(ws, n: int, method: str, params: dict) -> dict:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 60))
            if msg.get("id") == n:
                return msg

    # ping_interval=None: no client keepalive at all, the worst case for an idle connection.
    async with websockets.connect(url, max_size=None, open_timeout=30, ping_interval=None) as ws:
        print(f"[smoke] wss open ({base}); capabilities: {'result' in await rpc(ws, 1, 'client.capabilities', {'server_requests': True})}")
        start = time.time()
        inbound = 0
        deadline = start + seconds
        try:
            while (left := deadline - time.time()) > 0:
                try:
                    await asyncio.wait_for(ws.recv(), left)   # server frames count; we send nothing
                    inbound += 1
                except asyncio.TimeoutError:
                    break
        except websockets.ConnectionClosed as e:
            print(f"[smoke] FAILED: socket closed after {time.time() - start:.0f}s idle: code {e.code}")
            return 1
        print(f"[smoke] idle {time.time() - start:.0f}s with no client frames; {inbound} server frames seen")
        made = await rpc(ws, 2, "session.create", {})
        ok = "result" in made
        print(f"[smoke] same socket after idle: session.create {'ok' if ok else 'FAILED'}")
        return 0 if ok else 1


def main() -> int:
    mode, arg = sys.argv[1], sys.argv[2]
    server, access, grant = sign_in()
    try:
        return chat(grant, arg) if mode == "chat" else asyncio.run(idle(grant, int(arg)))
    finally:
        api(server, "POST", "/launcher/auth/revoke", token=access)
        print("[smoke] device revoked")


if __name__ == "__main__":
    sys.exit(main())
