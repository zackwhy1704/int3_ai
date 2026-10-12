"""MCP sidecar exposing the Company Brain as a single tool, PER USER.

One sidecar runs per signed-in user. Its identity is carried entirely by the
brain service token in BRAIN_SERVICE_TOKEN (the brain resolves it to
tenant/user and scopes every retrieval to that user's memberships). The token
is injected only here, in the user's own sidecar — it is NEVER placed in the
Hermes container (provision-user.sh asserts this).

Two layers protect against another user reaching this sidecar:
  * NETWORK: the sidecar shares a per-user internal network only with that one
    user's Hermes backend (provision-user.sh); no other user's container can
    resolve or route to it.
  * AUTH: every request must carry X-MCP-Secret equal to this sidecar's
    per-user MCP_SECRET (constant-time compare), else 401. provision-user.sh
    writes the same secret into that user's Hermes mcp_servers.<name>.headers.
    This is defence in depth: even if the network boundary were bypassed, a
    caller without the secret gets 401 instead of an answer.

Transport: streamable-HTTP. ask_brain(question) -> POST {BRAIN_URL}/v1/brain_ask.
"""

import hmac
import os

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP

BRAIN_URL = os.environ["BRAIN_URL"].rstrip("/")
BRAIN_TOKEN = os.environ["BRAIN_SERVICE_TOKEN"]
MCP_SECRET = os.environ["MCP_SECRET"].encode()        # per-user; required
PORT = int(os.environ.get("MCP_PORT", "9000"))

mcp = FastMCP("company-brain", host="0.0.0.0", port=PORT)


@mcp.tool()
def ask_brain(question: str) -> dict:
    """Answer a question strictly from the company knowledge base.

    Returns the grounded answer with structured citations. If the knowledge
    base does not support an answer, `refused` is true and `answer` is null —
    in that case, tell the user it is not in the knowledge base; do NOT answer
    from your own knowledge. Always preserve the citations exactly as returned.
    """
    resp = httpx.post(
        f"{BRAIN_URL}/v1/brain_ask",
        json={"question": question},
        headers={"Authorization": f"Bearer {BRAIN_TOKEN}"},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "refused": data.get("refused", False),
        "answer": data.get("answer"),
        "message": data.get("message"),
        "citations": data.get("citations"),
        "answered_from": data.get("answered_from"),
        "retrieval_id": data.get("retrieval_id"),
    }


class RequireSecret:
    """Pure-ASGI gate: every HTTP request must present X-MCP-Secret == MCP_SECRET
    (constant-time). Pure ASGI (not BaseHTTPMiddleware) so SSE streaming and the
    session-manager lifespan pass through untouched."""

    def __init__(self, app, secret: bytes):
        self.app = app
        self.secret = secret

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"x-mcp-secret", b"")
            if not hmac.compare_digest(got, self.secret):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body",
                            "body": b"unauthorized: missing or invalid X-MCP-Secret\n"})
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    app = RequireSecret(mcp.streamable_http_app(), MCP_SECRET)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
