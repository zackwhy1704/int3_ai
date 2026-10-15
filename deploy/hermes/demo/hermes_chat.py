#!/usr/bin/env python3
"""Drive the REAL stock Hermes agent exactly as the desktop does — over /api/ws.

Connection path is the desktop's: host forwarder port -> route proxy -> GET
/api/ws?token=<session_token from the connect file>. Protocol is the desktop gateway's
JSON-RPC 2.0 (tui_gateway/method_ctx.py:_ok -> {"jsonrpc":"2.0","id","result"}):
  * session.create   (tui_gateway/methods_session.py:492)
  * prompt.submit    (tui_gateway/methods_prompt.py:735)  -> {session_id, text}
  * approval.respond (tui_gateway/methods_prompt.py:1258) -> {session_id, choice, request_id}
    with choice 'once' == the desktop "Run" button, 'deny' == "Deny"
    (apps/desktop/.../tool/approval.tsx:385 / sendApproval).
Streamed events are {"type": <name>, "payload": {...}}: message.start/delta/complete,
tool.start/generating/complete (gateway-event/index.ts); the approval prompt arrives as
a server->client frame carrying {command, description, request_id, choices}
(gateway-event/server-requests.ts:387-407).

We NEVER call the sidecar or the gateway directly — only /api/ws. If a step can't be
driven over /api/ws we stop and say so (exit 3).

Usage:
  hermes_chat.py --connect <connect.json> --say "<prompt>" [--approve once|deny|none]
                 [--expect-approval] [--verbose]
Prints a readable transcript and a final one-line JSON summary: __HERMES__ {...}.
"""
import argparse, asyncio, json, sys
from pathlib import Path

try:
    import websockets
except ImportError:
    print("hermes_chat.py needs the 'websockets' package (pip install websockets)", file=sys.stderr)
    sys.exit(2)


def _ws_url(base_url: str, token: str) -> str:
    b = base_url.rstrip("/")
    if b.startswith("https://"):
        b = "wss://" + b[len("https://"):]
    elif b.startswith("http://"):
        b = "ws://" + b[len("http://"):]
    return f"{b}/api/ws?token={token}"


class Hermes:
    def __init__(self, ws, verbose=False):
        self.ws = ws
        self.verbose = verbose
        self._id = 0
        self._pending = {}          # id -> Future (our client->server requests)
        self.answer = []            # accumulated message.delta text
        self.tools = []             # tool names seen
        self.brain = None           # last ask_brain tool result (citations etc.)
        self.approval_prompts = []  # [{text, decision}]
        self._done = asyncio.Event()

    async def _reader(self, approve):
        async for raw in self.ws:
            try:
                f = json.loads(raw)
            except Exception:
                continue
            if self.verbose:
                print(f"  « {json.dumps(f)[:400]}", file=sys.stderr)
            # 1) response to one of our requests (has our id + result/error)
            if "id" in f and ("result" in f or "error" in f) and f["id"] in self._pending:
                fut = self._pending.pop(f["id"])
                if not fut.done():
                    fut.set_result(f)
                continue
            # 2) server->client request (method + id, no result) — e.g. an approval prompt
            method = f.get("method", "")
            params = f.get("params") or {}
            if method:
                await self._on_server_request(f, method, params, approve)
                continue
            # 3) streamed event {type, payload}
            await self._on_event(f.get("type", ""), f.get("payload") or f)

    async def _on_event(self, etype, payload):
        if etype == "message.delta":
            t = payload.get("text") or payload.get("delta") or payload.get("content") or ""
            if isinstance(t, str):
                self.answer.append(t)
        elif etype in ("tool.start", "tool.complete"):
            name = payload.get("name") or payload.get("tool") or ""
            if etype == "tool.start" and name:
                self.tools.append(name)
            if etype == "tool.complete" and "ask_brain" in str(name):
                res = payload.get("result") or payload.get("output")
                if isinstance(res, str):
                    try: res = json.loads(res)
                    except Exception: res = None
                if isinstance(res, dict):
                    self.brain = res
        elif etype in ("message.complete", "run.complete", "turn.complete"):
            self._done.set()
        elif etype == "error":
            self._done.set()

    async def _on_server_request(self, frame, method, params, approve):
        if "approval" in method or params.get("request_id") or "command" in params:
            text = params.get("command") or params.get("description") or ""
            request_id = params.get("request_id") or params.get("requestId")
            decision = approve
            self.approval_prompts.append({"text": text, "decision": decision})
            if decision in ("once", "deny"):
                sid = getattr(self, "session_id", "")
                await self._notify("approval.respond",
                                   {"session_id": sid, "choice": decision, "request_id": request_id})

    async def _send(self, obj):
        await self.ws.send(json.dumps(obj))

    async def _notify(self, method, params):
        self._id += 1
        await self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})

    async def request(self, method, params, timeout=180):
        self._id += 1
        rid = self._id
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        f = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in f:
            raise RuntimeError(f"{method} error: {json.dumps(f['error'])[:300]}")
        return f.get("result") or {}

    async def wait_turn(self, timeout=180):
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(1.5)  # drain trailing deltas


async def run(connect_file, say, approve, expect_approval, verbose):
    data = json.loads(Path(connect_file).read_text())
    url, token, label = data["url"], data["session_token"], data.get("name", connect_file)
    ws_url = _ws_url(url, token)
    print(f"[connect] {label}  ->  {url}/api/ws  (desktop path: forwarder -> route proxy -> /api/ws)")
    async with websockets.connect(ws_url, max_size=None, open_timeout=30) as ws:
        h = Hermes(ws, verbose=verbose)
        reader = asyncio.create_task(h._reader(approve))
        try:
            res = await h.request("session.create", {})
            h.session_id = res.get("session_id") or res.get("stored_session_id")
            if not h.session_id:
                print("STOP: session.create returned no session_id over /api/ws", file=sys.stderr); sys.exit(3)
            print(f"[session] {h.session_id}")
            print(f"[{label}] asks: {say}")
            await h.request("prompt.submit", {"session_id": h.session_id, "text": say})
            await h.wait_turn()
        finally:
            reader.cancel()
    answer = "".join(h.answer).strip()
    cites = [c.get("doc_title") for c in ((h.brain or {}).get("citations") or [])]
    print(f"[Hermes] {answer}")
    if h.approval_prompts:
        for a in h.approval_prompts:
            print(f"[approval] prompt: {a['text']!r}  -> decision: {a['decision']}")
    if cites:
        print(f"[citations] {cites}")
    if expect_approval and not h.approval_prompts:
        print("STOP: expected an approval prompt over /api/ws, none arrived", file=sys.stderr); sys.exit(3)
    print("__HERMES__ " + json.dumps({
        "answer": answer,
        "refused": (h.brain or {}).get("refused"),
        "answered_from": (h.brain or {}).get("answered_from"),
        "citations": cites,
        "tools": h.tools,
        "approvals": h.approval_prompts,
    }))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connect", required=True)
    ap.add_argument("--say", required=True)
    ap.add_argument("--approve", choices=["once", "deny", "none"], default="none")
    ap.add_argument("--expect-approval", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(a.connect, a.say, a.approve, a.expect_approval, a.verbose))


if __name__ == "__main__":
    main()
