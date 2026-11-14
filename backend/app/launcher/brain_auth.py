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

# The routes the brain MCP endpoint calls (/v1) and the int3 memory plugin calls (/launcher/local/memory,
# via the launcher edge). Anything else with a brain token is refused.
_ALLOWED = (
    ("POST", "/v1/search"), ("POST", "/v1/validate"), ("POST", "/v1/proposal_preview"),
    ("POST", "/v1/propose_claim"), ("GET", "/v1/proposals"), ("GET", "/v1/proposal/"),
    ("POST", "/v1/approve_proposal"), ("POST", "/v1/reject_proposal"), ("POST", "/v1/session"),
    ("POST", "/launcher/local/memory/save"), ("POST", "/launcher/local/memory/recall"),
    ("POST", "/launcher/local/memory/forget"),
)
_TOOL = {
    "/v1/search": "search_brain", "/v1/validate": "validate_citations",
    "/v1/proposal_preview": "proposal_preview", "/v1/propose_claim": "propose_claim",
    "/v1/proposals": "list_proposals", "/v1/proposal/": "get_proposal",
    "/v1/approve_proposal": "approve_proposal", "/v1/reject_proposal": "reject_proposal",
    "/v1/session": "mcp_session",
    "/launcher/local/memory/save": "memory_save", "/launcher/local/memory/recall": "memory_recall",
    "/launcher/local/memory/forget": "memory_forget",
}
EXPIRED = HTTPException(401, "Your int3 access has expired or was signed out. Reopen int3 to continue.")

# Per-minute window, in process (one brain worker per deployment; a second worker would enforce its
# own share). The per-day limit counts brain_audit, so it holds across restarts. The brain and the
# memory plugin have separate quotas: memory tools are named memory_* in brain_audit.
_minute: dict[tuple[str, str, str], deque] = {}
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
    """Describe the audit entry for a call made on a brain token (local mode). Nothing is written
    here: brain_principal writes ONE row after the handler, with the real outcome (a handler that
    raises after this call is logged refused / not_found / rate_limited / error, never ok). An
    ``outcome`` given here (e.g. "empty") applies only if the handler returns normally."""
    if p.via == "brain":
        p.audit = {"tool": tool, **kw}


def outcome_of(exc: BaseException) -> str:
    """The audit outcome for a handler that raised."""
    if isinstance(exc, HTTPException):
        return {401: "refused", 403: "refused", 404: "not_found", 429: "rate_limited"}.get(
            exc.status_code, "error")
    return "error"


def _limits(kind: str) -> tuple[int, int]:
    return ((config.MEMORY_RATE_PER_MINUTE, config.MEMORY_RATE_PER_DAY) if kind == "memory"
            else (config.BRAIN_RATE_PER_MINUTE, config.BRAIN_RATE_PER_DAY))


def _tick_minute(tenant_id: str, user_id: str, kind: str) -> str | None:
    """Count this call in the user's per-minute window and say whether the window was already full.
    EVERY call a live token makes is counted, whatever happens to it next (refused route, failed
    setup check, refused or not-found in the handler, or this limit itself), so probing is not free:
    a client that keeps calling while limited stays limited until it pauses."""
    per_minute, _ = _limits(kind)
    now = time.monotonic()
    with _minute_lock:
        q = _minute.setdefault((tenant_id, user_id, kind), deque())
        while q and now - q[0] > 60:
            q.popleft()
        full = len(q) >= per_minute
        if len(q) < 4 * per_minute:  # bounded: a hammering client cannot grow this without limit
            q.append(now)
    return f"{per_minute} {kind} requests per minute" if full else None


def _check_daily(tenant_id: str, user_id: str, kind: str = "brain") -> str | None:
    """The per-day limit counts only calls that returned something (ok / empty)."""
    _, per_day = _limits(kind)
    with control_conn() as control:
        n = control.execute(
            "SELECT count(*) AS n FROM brain_audit WHERE tenant_id = %s AND user_id = %s"
            " AND outcome IN ('ok', 'empty') AND at > date_trunc('day', now())"
            " AND (tool LIKE 'memory\\_%%') = %s",
            (tenant_id, user_id, kind == "memory"),
        ).fetchone()["n"]
    if n >= per_day:
        return f"{per_day} {kind} requests per day"
    return None


def brain_principal(request: Request) -> Iterator[Principal]:
    token = _bearer(request) or ""
    with control_conn() as control:
        row = tokens.resolve_brain(control, token)
    if row is None:
        raise EXPIRED
    tenant_id, user_id, device_id = row["tenant_id"], row["user_id"], row["device_id"]
    route = _route(request)
    tool = _TOOL[route] if route is not None else request.url.path
    kind = "memory" if tool.startswith("memory_") else "brain"
    minute_full = _tick_minute(tenant_id, user_id, kind)  # before any refusal: refusals count too
    if route is None:
        record(tenant_id, user_id, device_id, tool, outcome="refused")
        raise HTTPException(403, "not available in local mode")
    if not row["posture_ok"]:
        reasons = "; ".join(row["posture_reasons"])
        record(tenant_id, user_id, device_id, tool, outcome="refused")
        raise HTTPException(403, f"int3 can't verify this Hermes setup ({reasons}). Reopen int3 to fix it.")
    limit = minute_full or _check_daily(tenant_id, user_id, kind)
    if limit is not None:
        record(tenant_id, user_id, device_id, tool, outcome="rate_limited")
        raise HTTPException(429, f"int3 {kind} limit reached ({limit}). Try again later.")
    tenant_name = _tenant_name(tenant_id)
    if tenant_name is None:
        raise EXPIRED
    gen = _principal_for(tenant_id, tenant_name, user_id, email=None,
                         session_id="bt:" + tokens.hash_token(token)[:16], csrf_token="")
    p = next(gen)
    p.via, p.device_id = "brain", str(device_id)
    try:
        try:
            yield p
        except BaseException as exc:  # the handler raised: log what really happened, then re-raise
            entry = {"tool": tool, **(p.audit or {})}
            entry["outcome"] = outcome_of(exc)
            record(tenant_id, user_id, device_id, **entry)
            raise
        record(tenant_id, user_id, device_id, **{"tool": tool, **(p.audit or {})})
    finally:
        gen.close()
