"""MCP sidecar exposing the Company Brain as a single tool, PER USER.

One sidecar runs per signed-in user. Its identity is carried entirely by the
brain service token in BRAIN_SERVICE_TOKEN (the brain resolves it to
tenant/user and scopes every retrieval to that user's memberships). The token
is injected only here, in the user's own sidecar — it is NEVER placed in the
Hermes container (provision-user.sh asserts this), so a compromised Hermes
backend cannot read the brain as the user or as anyone else.

There is exactly one tool and no catalog. The MCP surface itself is unauthenticated
but reachable only on the internal tenant network (no published port); the brain
behind it still requires the service token.

Transport: streamable-HTTP (Hermes connects via `mcp_servers.<name>.url`).
ask_brain(question) -> POST {BRAIN_URL}/v1/brain_ask with the service token.
Response schema (int3_ai backend/app/api/v1.py): {refused, message,
suggested_owner, answer, citations, answered_from, facts, cached, retrieval_id}.
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP

BRAIN_URL = os.environ["BRAIN_URL"].rstrip("/")          # e.g. http://brain:8000
BRAIN_TOKEN = os.environ["BRAIN_SERVICE_TOKEN"]          # cb_svc_... (this user's)

mcp = FastMCP("company-brain", host="0.0.0.0", port=9000)


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
    # Pass through exactly what the brain returned — the agent must not see a
    # paraphrase. citations carry claim/source ids; retrieval_id binds them.
    return {
        "refused": data.get("refused", False),
        "answer": data.get("answer"),
        "message": data.get("message"),
        "citations": data.get("citations"),
        "answered_from": data.get("answered_from"),
        "retrieval_id": data.get("retrieval_id"),
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
