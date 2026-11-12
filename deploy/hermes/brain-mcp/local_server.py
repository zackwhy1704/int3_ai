"""Local mode: ONE multi-user brain MCP endpoint for stock Hermes running on users' own PCs
(docs/LOCAL_MODE_DESIGN.md). The per-user sidecar (server.py) stays for the hosted mode.

Identity: every HTTP request must carry `Authorization: Bearer cb_bt_...`, the user's own brain token
(the launcher writes it into the local Hermes .env; Hermes sends it via the mcp_servers header). This
service holds NO credential of its own: it forwards the caller's token to the brain's /v1 API, which
checks it on every call (revocation, expiry, Hermes-setup report, rate limits) and writes the audit.

Passages only: the brain returns the claims it found plus a retrieval_id; the model on the user's PC
writes the answer and cites claim ids, and validate_citations checks those ids against what this user
actually retrieved. No server-side model is involved.

Capture tools keep their human gate: propose/approve/reject require an MCP elicitation accept.
"""

import contextvars
import os

import httpx
import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel

BRAIN_URL = os.environ["BRAIN_URL"].rstrip("/")
PORT = int(os.environ.get("MCP_PORT", "8200"))
BRAIN_PREFIX = "cb_bt_"
REOPEN = "Your int3 access has expired or was signed out. Reopen int3 to continue."

_caller: contextvars.ContextVar[str] = contextvars.ContextVar("caller_token", default="")

mcp = FastMCP("int3-brain", host="0.0.0.0", port=PORT, stateless_http=True)


def _token(ctx: Context | None) -> str:
    """The calling user's brain token: from the live HTTP request when the SDK exposes it, else the
    value the ASGI gate bound for this request."""
    req = getattr(getattr(ctx, "request_context", None), "request", None) if ctx is not None else None
    auth = req.headers.get("authorization", "") if req is not None else ""
    scheme, _, value = auth.partition(" ")
    value = value.strip() if scheme.lower() == "bearer" else ""
    return value or _caller.get()


def _call(ctx: Context | None, method: str, path: str, *, json: dict | None = None,
          params: dict | None = None) -> dict:
    token = _token(ctx)
    if not token.startswith(BRAIN_PREFIX):
        return {"error": 401, "message": REOPEN}
    try:
        r = httpx.request(method, f"{BRAIN_URL}{path}", json=json, params=params,
                          headers={"Authorization": f"Bearer {token}"}, timeout=120)
    except httpx.HTTPError:
        return {"error": 503, "message": "The int3 brain is unreachable right now. Try again shortly."}
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        message = REOPEN if r.status_code == 401 else (detail or "the int3 brain refused the request")
        return {"error": r.status_code, "message": str(message)[:400]}
    return r.json()


class Confirm(BaseModel):
    """Empty by design: the accept/decline action is the whole signal."""


async def _confirmed(ctx: Context, message: str) -> bool:
    try:
        res = await ctx.elicit(message=message, schema=Confirm)
    except Exception:
        return False
    return getattr(res, "action", None) == "accept"


@mcp.tool()
def search_brain(query: str, ctx: Context, brain: str | None = None) -> dict:
    """Search the company knowledge (int3 brain) for passages about `query`. Answer ONLY from the
    returned claims and cite each fact as [claim:ID]. Then call validate_citations with this
    retrieval_id and the ids you cited. If `results` is empty, say the company knowledge has no
    reliable source for it and do not guess."""
    body = {"query": query, **({"brain_id": brain} if brain else {})}
    data = _call(ctx, "POST", "/v1/search", json=body)
    if "error" in data:
        return data
    data["how_to_cite"] = "Cite facts as [claim:ID] using claimId; validate before answering."
    return data


@mcp.tool()
def validate_citations(retrieval_id: str, claim_ids: list[int], ctx: Context) -> dict:
    """Check the claim ids you are about to cite against what search_brain returned to you. Remove
    any fact whose id comes back in `invalid` before answering."""
    return _call(ctx, "POST", "/v1/validate", json={"retrieval_id": retrieval_id, "claim_ids": claim_ids})


def _propose_message(scope: str, statement: str, quote: str) -> str:
    return (f"Capture into '{scope}' — this exact statement will be stored and searchable:\n\n"
            f"    {statement}\n\nYour evidence (not stored as the searchable text): {quote}\n\nConfirm?")


def _approve_message(d: dict) -> str:
    c = d.get("conflict")
    conflict = ("\n\nCONFLICT — a current claim exists: "
                f"{c['existing_value']} ({c['authority']}, {c.get('doc_title')})") if c else ""
    return (f"Approve into '{d['scope']}' the statement:\n\n    {d['statement']}\n\n"
            f"Evidence: {d.get('quote')}\nProposed by: {d['proposer']} at {d['created_at']}"
            f"{conflict}\n\nConfirm?")


@mcp.tool()
async def propose_claim(scope: str, subject: str, attribute: str, value: str, quote: str,
                        ctx: Context, condition: str | None = None) -> dict:
    """Propose a fact to capture into the company brain, from THIS conversation. Only the scope's
    owner can approve it. `quote` is the user's own words; never content you read elsewhere."""
    pv = _call(ctx, "POST", "/v1/proposal_preview",
               json={"subject": subject, "attribute": attribute, "value": value, "condition": condition})
    if "error" in pv:
        return pv
    if not await _confirmed(ctx, _propose_message(scope, pv["statement"], quote)):
        return {"status": "not_confirmed", "detail": "user did not confirm; nothing was proposed"}
    return _call(ctx, "POST", "/v1/propose_claim",
                 json={"scope": scope, "subject": subject, "attribute": attribute, "value": value,
                       "quote": quote, "condition": condition})


@mcp.tool()
def list_proposals(scope: str, ctx: Context) -> dict:
    """List pending proposals for a scope you OWN, each with any conflicting claim."""
    return _call(ctx, "GET", "/v1/proposals", params={"scope": scope})


@mcp.tool()
async def approve_proposal(proposal_id: str, ctx: Context, supersede_anyway: bool = False) -> dict:
    """Approve a pending proposal (scope owner only), after the user confirms its full detail."""
    detail = _call(ctx, "GET", f"/v1/proposal/{proposal_id}")
    if "error" in detail:
        return detail
    if not await _confirmed(ctx, _approve_message(detail)):
        return {"status": "not_confirmed", "detail": "user did not confirm; nothing was approved"}
    return _call(ctx, "POST", "/v1/approve_proposal",
                 json={"proposal_id": proposal_id, "supersede_anyway": supersede_anyway})


@mcp.tool()
async def reject_proposal(proposal_id: str, ctx: Context) -> dict:
    """Reject a pending proposal (scope owner only), after the user confirms."""
    if not await _confirmed(ctx, f"Reject proposal {proposal_id}?"):
        return {"status": "not_confirmed", "detail": "user did not confirm"}
    return _call(ctx, "POST", "/v1/reject_proposal", json={"proposal_id": proposal_id})


class RequireBrainToken:
    """Pure-ASGI gate: no brain token, no MCP at all (not even the tool list). The token's validity is
    the brain's call; this only refuses requests that carry none, and binds it for the tools."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            scheme, _, value = auth.partition(" ")
            value = value.strip()
            if scheme.lower() != "bearer" or not value.startswith(BRAIN_PREFIX):
                body = (REOPEN + "\n").encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            token = _caller.set(value)
            try:
                await self.app(scope, receive, send)
            finally:
                _caller.reset(token)
            return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(RequireBrainToken(mcp.streamable_http_app()), host="0.0.0.0", port=PORT)
