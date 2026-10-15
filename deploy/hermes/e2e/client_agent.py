# E2E chat-turn client (behavioral layer). Runs INSIDE a user's u-net container.
# Drives the BYOK gateway (model) + the user's brain MCP sidecar (ask_brain) — the
# same model + brain path the gated backend's agent uses when the desktop sends a
# turn — and prints one JSON line the orchestrator parses:
#   __E2E__ {"refused":bool,"answer":str,"answered_from":[...],"titles":[...]}
# stdlib only (urllib), so it runs in any python image.
import json, os, urllib.request, urllib.error

GW = os.environ["GATEWAY_URL"].rstrip("/"); GWT = os.environ["GATEWAY_TOKEN"]
SIDECAR = os.environ["SIDECAR_URL"]; SECRET = os.environ["MCP_SECRET"]
MODEL = os.environ.get("MODEL", "claude-sonnet-5-5"); Q = os.environ["Q"]
OPEN = lambda req: urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=90)


def post(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("content-type", "application/json")
    try:
        return OPEN(req).read().decode()
    except urllib.error.HTTPError as e:
        return json.dumps({"error": e.code, "body": e.read().decode()[:200]})


def mcp_ask(q):
    def c(body, sid=None):
        req = urllib.request.Request(SIDECAR, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("X-MCP-Secret", SECRET)
        if sid:
            req.add_header("Mcp-Session-Id", sid)
        r = OPEN(req); nsid = r.headers.get("Mcp-Session-Id") or sid; raw = r.read().decode()
        res = None
        for ln in raw.splitlines():
            if ln.startswith("data: "):
                res = json.loads(ln[6:]); break
        return nsid, res
    sid, _ = c({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "e2e", "version": "0"}}})
    c({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _, res = c({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "ask_brain", "arguments": {"question": q}}}, sid)
    return json.loads(res["result"]["content"][0]["text"])


TOOLS = [{"type": "function", "function": {
    "name": "ask_brain",
    "description": ("Answer strictly from the company knowledge base; if unsupported, refused=true and you "
                    "must say it is not in the knowledge base and not answer from your own knowledge. Preserve citations."),
    "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}}]
msgs = [{"role": "system", "content": "Answer ONLY using ask_brain. If refused=true, say it is not in the knowledge base; do not answer from your own knowledge."},
        {"role": "user", "content": Q}]
brain = None
out_text = ""
for _ in range(4):
    out = json.loads(post(f"{GW}/v1/chat/completions",
                           {"model": MODEL, "messages": msgs, "tools": TOOLS, "tool_choice": "auto", "max_tokens": 1024},
                           {"authorization": f"Bearer {GWT}"}))
    if "error" in out:
        print("__E2E__ " + json.dumps({"gateway_error": out})); raise SystemExit(0)
    m = out["choices"][0]["message"]; calls = m.get("tool_calls") or []
    if not calls:
        out_text = m.get("content") or ""; break
    msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": calls})
    for call in calls:
        args = json.loads(call["function"]["arguments"] or "{}")
        r = mcp_ask(args.get("question", Q))
        if brain is None:
            brain = r
        msgs.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(r)})

b = brain or {}
print("__E2E__ " + json.dumps({
    "refused": b.get("refused"),
    "answer": out_text,
    "answered_from": b.get("answered_from"),
    "titles": [c.get("doc_title") for c in (b.get("citations") or [])],
}))
