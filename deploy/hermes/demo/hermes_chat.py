#!/usr/bin/env python3
"""Drive the REAL stock Hermes agent exactly as the desktop does — over /api/ws.

Connection path is the desktop's: host forwarder port -> route proxy -> GET
/api/ws?token=<session_token from the connect file>. Protocol is the desktop gateway's
JSON-RPC 2.0, verified against hermes-agent v0.21.7:

  * client->server requests (have our integer id, await a response):
      - client.capabilities {"server_requests": true}   (tui_gateway/methods_voice.py:442-450)
        REQUIRED FIRST: a WS client that never sends it has every server->client approval
        request dropped and the agent waits the whole deadline (server_requests.py:30-34, #112548).
      - session.create {}                                (tui_gateway/methods_session.py:492)
      - prompt.submit {session_id, text}                 (tui_gateway/methods_prompt.py:735)
        returns {"status":"streaming"} immediately — the turn then STREAMS as events.

  * server->STREAM events are JSON-RPC notifications wrapped in an "event" envelope
    (tui_gateway/server.py:701-704):
        {"jsonrpc":"2.0","method":"event","params":{"type":<name>,"session_id","payload":{...}}}
    - message.delta   payload.text   (contracts/events.py:117,126)
    - message.complete payload.{text,status}  THE TURN ENDED: final text + outcome
        (contracts/events.py:213; prompt_turn.py:947). status 'complete' | 'error'.
    - session.info    payload.running  (contracts/events.py:374; utils.ts:122-132) — running=false
        is the gateway's authoritative turn-end heartbeat; our backstop if message.complete is missed.
    - tool.start / tool.complete  (contracts/events.py:280,299) — tool.complete carries only a UI
        preview (tool_progress.py:257), NOT the full ask_brain result, so refused/citations are read
        server-side from the sidecar's CB_ASK log, not from here.

  * server->CLIENT requests (an approval prompt) have a STRING id "srq-..." AND a method that is
    NOT "event" (server.py:690); method "approval", params {command,description,request_id,choices}
    (server.py:819-848; gateway-event/server-requests.ts:384-408). The desktop answers with a
    JSON-RPC RESPONSE to that id carrying {choice, all} — NOT a *.respond method
    (server_requests.py:1-13; store/prompts.ts:363-369 answerApproval -> respondToServerRequest).
    choice 'once' == the desktop "Run", 'deny' == "Deny" (server.py:844-845).

We NEVER call the sidecar or the gateway directly — only /api/ws. If a step can't be driven over
/api/ws we stop and say so (exit 3). A turn that times out FAILS loudly (prints TIMEOUT, exit 4);
it never passes silently.

Usage:
  hermes_chat.py --connect <connect.json> --say "<prompt>" [--approve once|deny|none]
                 [--expect-approval] [--turn-timeout N] [--verbose]
Prints a readable transcript (incl. the frame types seen) and a final one-line JSON summary:
  __HERMES__ {...}
"""
import argparse, asyncio, json, sys
from collections import Counter
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
        self._pending = {}           # our int id -> Future (client->server requests)
        self.deltas = []             # accumulated message.delta text
        self.final_text = None       # message.complete payload.text (authoritative)
        self.status = None           # message.complete payload.status ('complete'|'error')
        self.tools = []              # tool names seen (tool.start)
        self.brain_preview = None    # best-effort ask_brain tool.complete preview (UI only)
        self.approval_prompts = []   # [{text, command, description, request_id, choice}]
        self.frame_types = Counter() # every event type / 'server_request:<method>' seen
        self._running_seen_true = False
        self._done = asyncio.Event()
        self.timed_out = False
        self.session_id = ""

    async def _reader(self, approve):
        async for raw in self.ws:
            try:
                f = json.loads(raw)
            except Exception:
                continue
            if self.verbose:
                print(f"  « {json.dumps(f)[:500]}", file=sys.stderr)
            fid = f.get("id")
            method = f.get("method")
            # 1) a response to one of OUR requests (our int id + result/error)
            if fid in self._pending and ("result" in f or "error" in f):
                fut = self._pending.pop(fid)
                if not fut.done():
                    fut.set_result(f)
                continue
            # 2) a stream event: method=="event", wrapped {type, session_id, payload}
            if method == "event":
                p = f.get("params") or {}
                await self._on_event(p.get("type", ""), p.get("payload") or {})
                continue
            # 3) a server->client REQUEST (string "srq-" id + a non-event method): an approval
            if method and isinstance(fid, str):
                self.frame_types[f"server_request:{method}"] += 1
                await self._on_server_request(fid, method, f.get("params") or {}, approve)
                continue
            # anything else: record the shape but ignore
            self.frame_types[f"other:{method or ('response' if fid is not None else '?')}"] += 1

    async def _on_event(self, etype, payload):
        self.frame_types[etype] += 1
        if etype == "message.delta":
            t = payload.get("text")
            if isinstance(t, str):
                self.deltas.append(t)
        elif etype == "message.complete":
            t = payload.get("text")
            if isinstance(t, str):
                self.final_text = t
            self.status = payload.get("status") or self.status
            self._done.set()                              # THE turn-end signal
        elif etype == "session.info":
            running = payload.get("running")
            if running is True:
                self._running_seen_true = True
            elif running is False and self._running_seen_true:
                self._done.set()                          # authoritative backstop
        elif etype == "tool.start":
            name = payload.get("name") or payload.get("tool") or ""
            if name:
                self.tools.append(name)
        elif etype == "tool.complete":
            name = str(payload.get("name") or payload.get("tool") or "")
            if "ask_brain" in name:
                res = payload.get("result") or payload.get("output") or payload.get("parsed")
                if isinstance(res, str):
                    try:
                        res = json.loads(res)
                    except Exception:
                        res = None
                if isinstance(res, dict):
                    self.brain_preview = res
        elif etype == "error":
            self.status = "error"
            self._done.set()

    async def _on_server_request(self, srq_id, method, params, approve):
        # An approval prompt. Answer with a JSON-RPC RESPONSE to this id carrying {choice, all}
        # (exactly what the desktop's answerApproval does).
        command = params.get("command") or ""
        description = params.get("description") or ""
        request_id = params.get("request_id") or params.get("requestId") or ""
        text = (command + " " + description).strip() or command or description
        # --approve none must not hang the turn if an approval unexpectedly arrives: auto-deny.
        choice = approve if approve in ("once", "deny") else "deny"
        self.approval_prompts.append({
            "text": text, "command": command, "description": description,
            "request_id": request_id, "choice": choice,
        })
        await self._send({"jsonrpc": "2.0", "id": srq_id, "result": {"choice": choice, "all": False}})

    async def _send(self, obj):
        await self.ws.send(json.dumps(obj))

    async def request(self, method, params, timeout=60):
        self._id += 1
        rid = self._id
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        f = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in f:
            raise RuntimeError(f"{method} error: {json.dumps(f['error'])[:300]}")
        return f.get("result") or {}

    async def wait_turn(self, timeout):
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.timed_out = True
            print(f"TIMEOUT: no message.complete / session.info(running=false) within {timeout}s", file=sys.stderr)
            return
        await asyncio.sleep(1.0)  # drain any trailing deltas


async def run(connect_file, say, approve, expect_approval, turn_timeout, verbose):
    data = json.loads(Path(connect_file).read_text())
    url, token, label = data["url"], data["session_token"], data.get("name", connect_file)
    ws_url = _ws_url(url, token)
    print(f"[connect] {label}  ->  {url}/api/ws  (desktop path: forwarder -> route proxy -> /api/ws)")
    h = None
    try:
        async with websockets.connect(ws_url, max_size=None, open_timeout=30) as ws:
            h = Hermes(ws, verbose=verbose)
            reader = asyncio.create_task(h._reader(approve))
            try:
                # REQUIRED first, or approval requests are silently dropped (#112548).
                await h.request("client.capabilities", {"server_requests": True})
                res = await h.request("session.create", {})
                h.session_id = res.get("session_id") or res.get("stored_session_id") or ""
                if not h.session_id:
                    print("STOP: session.create returned no session_id over /api/ws", file=sys.stderr)
                    sys.exit(3)
                print(f"[session] {h.session_id}")
                print(f"[{label}] asks: {say}")
                ack = await h.request("prompt.submit", {"session_id": h.session_id, "text": say})
                if h.verbose:
                    print(f"  [prompt.submit ack] {json.dumps(ack)[:200]}", file=sys.stderr)
                await h.wait_turn(turn_timeout)
            finally:
                reader.cancel()
    except SystemExit:
        raise
    except Exception as e:
        print(f"STOP: /api/ws transport error: {e}", file=sys.stderr)
        sys.exit(3)

    answer = (h.final_text if h.final_text is not None else "".join(h.deltas)).strip()
    cites = [c.get("doc_title") for c in ((h.brain_preview or {}).get("citations") or []) if isinstance(c, dict)]
    print(f"[Hermes] {answer}")
    for a in h.approval_prompts:
        print(f"[approval] {a['text']!r}  -> choice: {a['choice']}  (request_id={a['request_id']})")
    if cites:
        print(f"[citations(preview)] {cites}")
    print(f"[frames] {dict(h.frame_types)}  status={h.status}  timed_out={h.timed_out}")
    print("__HERMES__ " + json.dumps({
        "answer": answer,
        "status": h.status,
        "timed_out": h.timed_out,
        # best-effort UI preview only; authoritative refused/answered_from/titles come from the
        # sidecar's CB_ASK log (the demo reads that), never from these.
        "refused_preview": (h.brain_preview or {}).get("refused"),
        "answered_from_preview": (h.brain_preview or {}).get("answered_from"),
        "citations_preview": cites,
        "tools": h.tools,
        "approvals": h.approval_prompts,
        "frame_types": dict(h.frame_types),
    }))
    if h.timed_out:
        sys.exit(4)
    if expect_approval and not h.approval_prompts:
        print("STOP: expected an approval prompt over /api/ws, none arrived", file=sys.stderr)
        sys.exit(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--connect", required=True)
    ap.add_argument("--say", required=True)
    ap.add_argument("--approve", choices=["once", "deny", "none"], default="none")
    ap.add_argument("--expect-approval", action="store_true")
    ap.add_argument("--turn-timeout", type=int, default=120)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(a.connect, a.say, a.approve, a.expect_approval, a.turn_timeout, a.verbose))


if __name__ == "__main__":
    main()
