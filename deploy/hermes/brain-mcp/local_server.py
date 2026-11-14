"""Local mode: ONE multi-user brain MCP endpoint for stock Hermes running on users' own PCs
(docs/LOCAL_MODE_DESIGN.md). The per-user sidecar (server.py) stays for the hosted mode.

Identity: every HTTP request must carry `Authorization: Bearer cb_bt_...`, the user's own brain token
(the launcher writes it into the local Hermes .env; Hermes sends it via the mcp_servers header). This
service holds NO credential of its own: it forwards the caller's token to the brain's /v1 API, which
checks it on every call (revocation, expiry, Hermes-setup report, rate limits) and writes the audit.

Passages only: the brain returns the claims it found plus a retrieval_id; the model on the user's PC
writes the answer and cites claim ids, and validate_citations checks those ids against what this user
actually retrieved. No server-side model is involved.

Capture tools keep their human gate: propose/approve/reject require an MCP elicitation accept. The
user's answer to that card arrives as a SEPARATE HTTP request, so the server must keep the session that
asked (stateful Streamable HTTP). Hence:
  - each session is bound to the brain token that opened it: the same session id under another token
    answers 404, exactly like an unknown session (SessionGate);
  - a session is opened only after the brain accepts the token (/v1/session), and one user holds at
    most MCP_SESSIONS_PER_USER open sessions (the least recently used is closed to make room);
  - idle sessions close after MCP_SESSION_IDLE_SECONDS (the SDK's idle timer);
  - sessions live in this process's memory, so it runs as EXACTLY ONE process (no uvicorn workers;
    compose pins one replica). Scaling out needs sticky routing by Mcp-Session-Id or a shared session
    store; see docs/LOCAL_MODE_DEPLOY_PLAN.md.
  - on SIGTERM a pending confirmation is answered at once with "int3 restarted, please ask again"
    instead of leaving Hermes to wait out its call timeout.
"""

import asyncio
import hashlib
import json
import logging
import os
import time

import httpx
import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

BRAIN_URL = os.environ["BRAIN_URL"].rstrip("/")
PORT = int(os.environ.get("MCP_PORT", "8200"))
BRAIN_PREFIX = "cb_bt_"
REOPEN = "Your int3 access has expired or was signed out. Reopen int3 to continue."

# How long a confirmation card waits for the user. The card sits in the chat the user just typed into,
# so they are present: 90 s is ample to read three lines and click, and a user who walked away gets a
# clear "nothing was saved, ask again" instead of a hang. Hermes's own call timeout for this server
# (local_policy: mcp_servers.int3_brain.timeout = 135) must stay above
# APPROVAL_SECONDS + 2 * BRAIN_TIMEOUT_SECONDS (90 + 2*15 = 120; 135 leaves 15 s margin), so this
# answer, not Hermes's generic timeout, is what the user sees.
APPROVAL_SECONDS = float(os.environ.get("MCP_APPROVAL_SECONDS", "90"))
BRAIN_TIMEOUT_SECONDS = float(os.environ.get("MCP_BRAIN_TIMEOUT_SECONDS", "15"))
IDLE_SECONDS = float(os.environ.get("MCP_SESSION_IDLE_SECONDS", "1800"))
SESSIONS_PER_USER = int(os.environ.get("MCP_SESSIONS_PER_USER", "5"))
MAX_SESSIONS = int(os.environ.get("MCP_MAX_SESSIONS", "1000"))
GRACEFUL_SECONDS = 5  # under Docker's 10 s stop grace

RESTARTED = {"error": 503, "message": "int3 restarted while waiting for the user's confirmation, so "
                                      "nothing was saved. Please ask again."}

log = logging.getLogger("int3.mcp")

mcp = FastMCP("int3-brain", host="0.0.0.0", port=PORT,
              session_idle_timeout=IDLE_SECONDS, max_sessions=MAX_SESSIONS)

_stopping = asyncio.Event()  # set when the process is told to stop; pending confirmations end at once


def _bearer(value: str) -> str:
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _token(ctx: Context | None) -> str:
    """The calling user's brain token, from the HTTP request that carried this tool call."""
    req = getattr(getattr(ctx, "request_context", None), "request", None) if ctx is not None else None
    return _bearer(req.headers.get("authorization", "")) if req is not None else ""


async def _brain(token: str, method: str, path: str, *, json: dict | None = None,
                 params: dict | None = None) -> dict:
    if not token.startswith(BRAIN_PREFIX):
        return {"error": 401, "message": REOPEN}
    try:
        async with httpx.AsyncClient(timeout=BRAIN_TIMEOUT_SECONDS) as client:
            r = await client.request(method, f"{BRAIN_URL}{path}", json=json, params=params,
                                     headers={"Authorization": f"Bearer {token}"})
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


async def _call(ctx: Context | None, method: str, path: str, **kw) -> dict:
    return await _brain(_token(ctx), method, path, **kw)


class Confirm(BaseModel):
    """Empty by design: the accept/decline action is the whole signal."""


async def _ask(ctx: Context, message: str) -> str:
    """'accept', 'decline', 'timeout' (no answer within APPROVAL_SECONDS) or 'restart' (stopping)."""
    async def answer() -> str:
        try:
            res = await ctx.elicit(message=message, schema=Confirm)
        except Exception:
            return "decline"
        return "accept" if getattr(res, "action", None) == "accept" else "decline"

    ans, stop = asyncio.create_task(answer()), asyncio.create_task(_stopping.wait())
    try:
        done, _ = await asyncio.wait({ans, stop}, timeout=APPROVAL_SECONDS,
                                     return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (ans, stop):
            if not t.done():
                t.cancel()
    if ans in done:
        return ans.result()
    return "restart" if stop in done else "timeout"


def _not_confirmed(outcome: str, what: str) -> dict:
    if outcome == "restart":
        return RESTARTED
    if outcome == "timeout":
        return {"status": "not_confirmed",
                "detail": f"No answer within {int(APPROVAL_SECONDS)} seconds, so nothing was {what}. "
                          "If the user still wants it, ask them again."}
    return {"status": "not_confirmed", "detail": f"user did not confirm; nothing was {what}"}


READ_ONLY = ToolAnnotations(readOnlyHint=True)  # lets Hermes retry these once after a session reconnect


@mcp.tool(annotations=READ_ONLY)
async def search_brain(query: str, ctx: Context, brain: str | None = None) -> dict:
    """Search the company knowledge (int3 brain) for passages about `query`. Answer ONLY from the
    returned claims and cite each fact as [claim:ID]. Then call validate_citations with this
    retrieval_id and the ids you cited. If `results` is empty, say the company knowledge has no
    reliable source for it and do not guess."""
    body = {"query": query, **({"brain_id": brain} if brain else {})}
    data = await _call(ctx, "POST", "/v1/search", json=body)
    if "error" in data:
        return data
    data["how_to_cite"] = "Cite facts as [claim:ID] using claimId; validate before answering."
    return data


@mcp.tool(annotations=READ_ONLY)
async def validate_citations(retrieval_id: str, claim_ids: list[int], ctx: Context) -> dict:
    """Check the claim ids you are about to cite against what search_brain returned to you. Remove
    any fact whose id comes back in `invalid` before answering."""
    return await _call(ctx, "POST", "/v1/validate", json={"retrieval_id": retrieval_id, "claim_ids": claim_ids})


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
    pv = await _call(ctx, "POST", "/v1/proposal_preview",
                     json={"subject": subject, "attribute": attribute, "value": value, "condition": condition})
    if "error" in pv:
        return pv
    outcome = await _ask(ctx, _propose_message(scope, pv["statement"], quote))
    if outcome != "accept":
        return _not_confirmed(outcome, "proposed")
    return await _call(ctx, "POST", "/v1/propose_claim",
                       json={"scope": scope, "subject": subject, "attribute": attribute, "value": value,
                             "quote": quote, "condition": condition})


@mcp.tool(annotations=READ_ONLY)
async def list_proposals(scope: str, ctx: Context) -> dict:
    """List pending proposals for a scope you OWN, each with any conflicting claim."""
    return await _call(ctx, "GET", "/v1/proposals", params={"scope": scope})


@mcp.tool()
async def approve_proposal(proposal_id: str, ctx: Context, supersede_anyway: bool = False) -> dict:
    """Approve a pending proposal (scope owner only), after the user confirms its full detail."""
    detail = await _call(ctx, "GET", f"/v1/proposal/{proposal_id}")
    if "error" in detail:
        return detail
    outcome = await _ask(ctx, _approve_message(detail))
    if outcome != "accept":
        return _not_confirmed(outcome, "approved")
    return await _call(ctx, "POST", "/v1/approve_proposal",
                       json={"proposal_id": proposal_id, "supersede_anyway": supersede_anyway})


@mcp.tool()
async def reject_proposal(proposal_id: str, ctx: Context) -> dict:
    """Reject a pending proposal (scope owner only), after the user confirms."""
    outcome = await _ask(ctx, f"Reject proposal {proposal_id}?")
    if outcome != "accept":
        return _not_confirmed(outcome, "rejected")
    return await _call(ctx, "POST", "/v1/reject_proposal", json={"proposal_id": proposal_id})


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _respond(send, status: int, message: str) -> None:
    """A JSON-RPC-shaped error, like the SDK's own (Hermes reads 'Session not found' as: reconnect)."""
    body = json.dumps({"jsonrpc": "2.0", "id": "server-error",
                       "error": {"code": -32600, "message": message}}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class SessionGate:
    """Pure-ASGI gate in front of the SDK's stateful session manager.

    - No brain token, no MCP at all (not even the tool list).
    - A new session (a request without Mcp-Session-Id) opens only if the brain accepts the token
      (/v1/session); the brain's answer names the user (an opaque key) for the per-user cap.
    - Every later request must carry the token that opened the session. Anything else answers 404
      "Session not found", indistinguishable from an unknown id. No raw token is kept: only its hash.
    """

    def __init__(self, app, manager):
        self.app = app
        self.manager = manager
        self.owner: dict[str, tuple[str, str]] = {}  # session id -> (token hash, user key)
        self.last: dict[str, float] = {}             # session id -> last request start (monotonic)
        self.lock = asyncio.Lock()

    def _live(self) -> dict:
        return self.manager._server_instances  # mcp 1.30 private internal; see check_sdk_internals

    def _prune(self) -> None:
        live = self._live()
        for sid in [s for s in self.owner if s not in live]:
            self.owner.pop(sid, None)
            self.last.pop(sid, None)

    async def _register(self, sid: str, token_hash: str, user_key: str) -> None:
        """Record a new session's owner AND enforce the per-user cap in one critical section, so two
        sessions opened at the same moment cannot both slip under the cap. The new session is kept;
        the user's least recently used others are closed until the cap holds."""
        async with self.lock:
            self.owner[sid] = (token_hash, user_key)
            self.last[sid] = time.monotonic()
            await self._enforce_cap(user_key, keep=sid)

    async def _enforce_cap(self, user_key: str, keep: str) -> None:
        self._prune()
        mine = sorted((s for s, (_, u) in self.owner.items() if u == user_key and s != keep),
                      key=lambda s: self.last.get(s, 0))
        while len(mine) >= SESSIONS_PER_USER:
            sid = mine.pop(0)
            transport = self._live().get(sid)
            self.owner.pop(sid, None)
            self.last.pop(sid, None)
            if transport is not None:
                log.info("closing session %s… to stay under %d per user", sid[:8], SESSIONS_PER_USER)
                await self.manager._discard_session(sid, transport)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        token = _bearer(headers.get(b"authorization", b"").decode("latin-1"))
        if not token.startswith(BRAIN_PREFIX):
            return await _respond(send, 401, REOPEN)
        sid = headers.get(b"mcp-session-id", b"").decode("latin-1") or None

        if sid is not None:
            owner = self.owner.get(sid)
            if owner is None or owner[0] != _hash(token):
                if owner is not None:
                    log.warning("refusing session %s…: not the token that opened it", sid[:8])
                return await _respond(send, 404, "Session not found")
            self.last[sid] = time.monotonic()
            await self.app(scope, receive, send)
            if sid not in self._live():
                self.owner.pop(sid, None)
                self.last.pop(sid, None)
            return

        if scope["method"] != "POST":  # GET/DELETE without a session: let the SDK refuse it
            return await self.app(scope, receive, send)
        who = await _brain(token, "POST", "/v1/session")
        if "error" in who:
            status = who["error"] if who["error"] in (401, 403, 429) else 503
            return await _respond(send, status, who["message"])
        user_key = str(who["user_key"])
        th = _hash(token)

        async def send_and_register(message):
            if message["type"] == "http.response.start":
                for k, v in message.get("headers") or []:
                    if k.lower() == b"mcp-session-id":
                        await self._register(v.decode("latin-1"), th, user_key)
            await send(message)

        await self.app(scope, receive, send_and_register)


def begin_shutdown() -> None:
    """End every pending confirmation now (they answer RESTARTED)."""
    _stopping.set()


class _Server(uvicorn.Server):
    def handle_exit(self, sig, frame):
        loop = getattr(self, "int3_loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(begin_shutdown)
        super().handle_exit(sig, frame)


def check_sdk_internals(manager) -> None:
    """SessionGate relies on two PRIVATE parts of mcp's StreamableHTTPSessionManager (mcp==1.30.0,
    requirements.txt). Fail at startup, loudly, if a different SDK changed them, rather than run
    with sessions that are never capped or never closed."""
    import inspect

    from mcp.server.streamable_http import StreamableHTTPServerTransport

    live = getattr(manager, "_server_instances", None)
    if not isinstance(live, dict):
        raise RuntimeError("mcp SDK changed: StreamableHTTPSessionManager._server_instances is "
                           f"{type(live).__name__}, not dict. Re-check local_server.SessionGate.")
    if any(not isinstance(t, StreamableHTTPServerTransport) for t in live.values()):
        raise RuntimeError("mcp SDK changed: _server_instances values are not StreamableHTTPServerTransport")
    discard = getattr(manager, "_discard_session", None)
    if not inspect.iscoroutinefunction(discard):
        raise RuntimeError("mcp SDK changed: StreamableHTTPSessionManager._discard_session is missing "
                           "or not async. Re-check local_server.SessionGate.")
    if list(inspect.signature(discard).parameters) != ["session_id", "transport"]:
        raise RuntimeError("mcp SDK changed: _discard_session(session_id, transport) has a new signature")


def build_app():
    app = mcp.streamable_http_app()
    check_sdk_internals(mcp.session_manager)
    return SessionGate(app, mcp.session_manager)


async def serve(port: int = PORT) -> None:
    # EXACTLY ONE process: sessions live in this process's memory. Do not add uvicorn workers.
    server = _Server(uvicorn.Config(build_app(), host="0.0.0.0", port=port, workers=1,
                                    timeout_graceful_shutdown=GRACEFUL_SECONDS, log_level="info"))
    server.int3_loop = asyncio.get_running_loop()
    await server.serve()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
