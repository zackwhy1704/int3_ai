"""The only way a request gets an identity, a tenant database and scopes.

cookie -> session (control db) -> tenant id -> tenant db, as the tenant's own role
       -> active user row -> memberships -> scopes

Nothing the client sends other than the session cookie is consulted. Scopes are
resolved again on every request, so removing a user or a membership takes effect
on their next request.
"""

import secrets
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
from fastapi import Depends, HTTPException, Request

from .. import config
from ..db import control_conn, tenant_conn
from . import service_tokens, sessions


@dataclass
class Principal:
    tenant_id: str
    tenant_name: str
    user_id: str
    email: str
    name: str
    scopes: list[str]
    csrf_token: str
    session_id: str
    conn: psycopg.Connection  # the tenant database, as the tenant's role

    def resolve(self, brain_id: str | None) -> list[str]:
        """Scopes to search: all of the user's, or one brain they belong to.
        A brain they can't open is indistinguishable from one that doesn't exist."""
        if brain_id is None:
            return self.scopes
        if brain_id not in self.scopes:
            raise HTTPException(404, "brain not found")
        return [brain_id]


UNAUTHENTICATED = HTTPException(401, "not signed in")


def _principal_for(
    tenant_id: str,
    tenant_name: str,
    user_id: str,
    *,
    email: str | None,
    session_id: str,
    csrf_token: str,
) -> Iterator[Principal]:
    """Open the tenant db as the tenant's role, load the active user and their
    scopes, and yield a Principal; close the connection on the way out.

    The identity has already been established by the caller (a session cookie,
    or a service token). ``email`` is cross-checked against the user row only on
    the cookie path, where the session carries an email that must still match
    the user_id; the service-token path passes None and takes the row's email.
    Scopes are resolved here on every request, so a revoked membership takes
    effect immediately regardless of how the caller authenticated.
    """
    conn = tenant_conn(tenant_id)
    try:
        if email is None:
            user = conn.execute(
                "SELECT id, name, email FROM users WHERE id = %s AND active",
                (user_id,),
            ).fetchone()
        else:
            user = conn.execute(
                "SELECT id, name, email FROM users WHERE id = %s AND email = %s AND active",
                (user_id, email),
            ).fetchone()
        if user is None:
            raise UNAUTHENTICATED
        scopes = [
            r["scope_id"]
            for r in conn.execute(
                "SELECT scope_id FROM memberships WHERE user_id = %s ORDER BY scope_id",
                (user["id"],),
            )
        ]
        yield Principal(
            tenant_id,
            tenant_name,
            user["id"],
            user["email"],
            user["name"],
            scopes,
            csrf_token,
            session_id,
            conn,
        )
    finally:
        conn.close()


def _tenant_name(tenant_id: str) -> str | None:
    with control_conn() as control:
        row = control.execute(
            "SELECT name FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    return row["name"] if row else None


def principal(request: Request) -> Iterator[Principal]:
    session_id = request.cookies.get(config.SESSION_COOKIE)
    if not session_id:
        raise UNAUTHENTICATED
    with control_conn() as control:
        s = sessions.lookup(control, session_id)
    if not s:
        raise UNAUTHENTICATED
    tenant_name = _tenant_name(s["tenant_id"])
    if tenant_name is None:
        raise UNAUTHENTICATED
    yield from _principal_for(
        s["tenant_id"],
        tenant_name,
        s["user_id"],
        email=s["email"],
        session_id=session_id,
        csrf_token=s["csrf_token"],
    )


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def service_principal(request: Request) -> Iterator[Principal]:
    """Identity from a service bearer token (the /v1/ machine surface).

    No cookie, no CSRF. The token resolves to (tenant, acting user); scopes come
    from that user's memberships. session_id is synthesised from the token hash
    so retrieval binding (/v1/validate) works per token, without a session row.
    """
    token = _bearer(request)
    if token is None:
        raise UNAUTHENTICATED
    with control_conn() as control:
        row = service_tokens.resolve(control, token)
        tenant = (
            row
            and control.execute(
                "SELECT name FROM tenants WHERE id = %s", (row["tenant_id"],)
            ).fetchone()
        )
    if not row or not tenant:
        raise UNAUTHENTICATED
    yield from _principal_for(
        row["tenant_id"],
        tenant["name"],
        row["user_id"],
        email=None,
        session_id="svc:" + service_tokens._hash(token)[:16],
        csrf_token="",
    )


def v1_principal(request: Request) -> Iterator[Principal]:
    """Auth for the /v1/ machine surface.

    Bearer path (the MCP sidecar): a service token, no CSRF — a machine client
    has no cookie and no browser origin, so CSRF is neither available nor
    meaningful; the bearer token itself is the credential.

    Cookie path (a browser hitting /v1/ directly): CSRF IS required, exactly as
    on the browser routes. Dropping it only for the bearer path keeps the
    machine surface usable without weakening cookie-authenticated requests —
    otherwise /v1/ would be a CSRF-exempt hole for any logged-in browser."""
    if _bearer(request) is not None:
        yield from service_principal(request)
        return
    # Cookie-authenticated: enforce the same CSRF gate as csrf_protected
    # (same-origin + matching per-session token) before yielding the principal.
    gen = principal(request)
    p = next(gen)
    try:
        if request.headers.get("origin") != config.APP_ORIGIN:
            raise HTTPException(403, "cross-origin request refused")
        token = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(token, p.csrf_token):
            raise HTTPException(403, "missing or invalid CSRF token")
        yield p
    finally:
        gen.close()


def csrf_protected(request: Request, p: Principal = Depends(principal)) -> Principal:
    """For state-changing requests: same-origin and a matching per-session token."""
    if request.headers.get("origin") != config.APP_ORIGIN:
        raise HTTPException(403, "cross-origin request refused")
    token = request.headers.get("x-csrf-token", "")
    if not secrets.compare_digest(token, p.csrf_token):
        raise HTTPException(403, "missing or invalid CSRF token")
    return p
