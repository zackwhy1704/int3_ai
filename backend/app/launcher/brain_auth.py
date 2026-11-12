"""Local mode: identity, limits and audit for the brain token (cb_bt_), the credential the user's own
Hermes sends to the brain MCP endpoint, which forwards it to /v1.

On EVERY request: the token must be live (not expired or revoked, device and identity present,
tenant in local mode), the device's latest Hermes-setup report must be ok, and the user must be
under their per-minute and per-day limits. Every call is written to brain_audit.

Brain tokens reach only the passages-only surface below. Server-written answers (/v1/brain_ask)
are a hosted-mode feature: local mode never needs a server-side model key.
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Iterator

from fastapi import HTTPException, Request

from .. import config
from ..auth.principal import Principal, _bearer, _principal_for, _tenant_name
from ..db import control_conn
from . import tokens

log = logging.getLogger("launcher.brain")

# The routes the brain MCP endpoint calls. Anything else with a brain token is refused.
_ALLOWED = (
    ("POST", "/v1/search"), ("POST", "/v1/validate"), ("POST", "/v1/proposal_preview"),
    ("POST", "/v1/propose_claim"), ("GET", "/v1/proposals"), ("GET", "/v1/proposal/"),
    ("POST", "/v1/approve_proposal"), ("POST", "/v1/reject_proposal"),
)
_TOOL = {
    "/v1/search": "search_brain", "/v1/validate": "validate_citations",
    "/v1/proposal_preview": "proposal_preview", "/v1/propose_claim": "propose_claim",
    "/v1/proposals": "list_proposals", "/v1/proposal/": "get_proposal",
    "/v1/approve_proposal": "approve_proposal", "/v1/reject_proposal": "reject_proposal",
}
EXPIRED = HTTPException(401, "Your int3 access has expired or was signed out. Reopen int3 to continue.")

# Per-minute window, in process (one brain worker per deployment; a second worker would enforce its
# own share). The per-day limit counts brain_audit, so it holds across restarts.
_minute: dict[tuple[str, str], deque] = {}
_minute_lock = threading.Lock()


def _route(request: Request) -> str | None:
    path, method = request.url.path, request.method
    for m, prefix in _ALLOWED:
        if method == m and (path == prefix or (prefix.endswith("/") and path.startswith(prefix))):
            return prefix
    return None


def record(tenant_id: str, user_id: str, device_id, tool: str, *, query: str | None = None,
           retrieval_id: str | None = None, claim_ids=(), invalid_ids=(), outcome: str = "ok") -> None:
    """Append one brain_audit row. Never raises into the request: an audit failure is logged loudly
    and the call itself is refused by the caller's own error path only if it was the gate."""
    try:
        with control_conn() as control:
            control.execute(
                "INSERT INTO brain_audit (tenant_id, user_id, device_id, tool, query, retrieval_id,"
                " claim_ids, invalid_ids, outcome) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, user_id, device_id, tool, (query or "")[:2000] or None, retrieval_id,
                 list(claim_ids), list(invalid_ids), outcome),
            )
    except Exception:  # audit must be visible in logs, never silently lost
        log.exception("brain_audit write failed for %s/%s %s", tenant_id, user_id, tool)


def record_for(p: Principal, tool: str, **kw) -> None:
    """Audit a call made by principal ``p`` if it came in on a brain token (local mode)."""
    if p.via == "brain":
        record(p.tenant_id, p.user_id, p.device_id, tool, **kw)


def _check_limits(tenant_id: str, user_id: str) -> str | None:
    now = time.monotonic()
    key = (tenant_id, user_id)
    with _minute_lock:
        q = _minute.setdefault(key, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= config.BRAIN_RATE_PER_MINUTE:
            return f"{config.BRAIN_RATE_PER_MINUTE} brain requests per minute"
        q.append(now)
    with control_conn() as control:
        n = control.execute(
            "SELECT count(*) AS n FROM brain_audit WHERE tenant_id = %s AND user_id = %s"
            " AND outcome IN ('ok', 'empty') AND at > date_trunc('day', now())",
            (tenant_id, user_id),
        ).fetchone()["n"]
    if n >= config.BRAIN_RATE_PER_DAY:
        return f"{config.BRAIN_RATE_PER_DAY} brain requests per day"
    return None


def brain_principal(request: Request) -> Iterator[Principal]:
    token = _bearer(request) or ""
    with control_conn() as control:
        row = tokens.resolve_brain(control, token)
    if row is None:
        raise EXPIRED
    tenant_id, user_id, device_id = row["tenant_id"], row["user_id"], row["device_id"]
    route = _route(request)
    if route is None:
        record(tenant_id, user_id, device_id, request.url.path, outcome="refused")
        raise HTTPException(403, "not available in local mode")
    tool = _TOOL[route]
    if not row["posture_ok"]:
        reasons = "; ".join(row["posture_reasons"])
        record(tenant_id, user_id, device_id, tool, outcome="refused")
        raise HTTPException(403, f"int3 can't verify this Hermes setup ({reasons}). Reopen int3 to fix it.")
    limit = _check_limits(tenant_id, user_id)
    if limit is not None:
        record(tenant_id, user_id, device_id, tool, outcome="rate_limited")
        raise HTTPException(429, f"int3 brain limit reached ({limit}). Try again later.")
    tenant_name = _tenant_name(tenant_id)
    if tenant_name is None:
        raise EXPIRED
    gen = _principal_for(tenant_id, tenant_name, user_id, email=None,
                         session_id="bt:" + tokens.hash_token(token)[:16], csrf_token="")
    p = next(gen)
    p.via, p.device_id = "brain", str(device_id)
    try:
        yield p
    finally:
        gen.close()
