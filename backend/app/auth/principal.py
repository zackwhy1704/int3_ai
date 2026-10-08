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
from . import sessions


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


def principal(request: Request) -> Iterator[Principal]:
    session_id = request.cookies.get(config.SESSION_COOKIE)
    if not session_id:
        raise UNAUTHENTICATED
    with control_conn() as control:
        s = sessions.lookup(control, session_id)
        tenant = (
            s
            and control.execute(
                "SELECT name FROM tenants WHERE id = %s", (s["tenant_id"],)
            ).fetchone()
        )
    if not s or not tenant:
        raise UNAUTHENTICATED

    conn = tenant_conn(s["tenant_id"])
    try:
        user = conn.execute(
            "SELECT id, name FROM users WHERE id = %s AND email = %s AND active",
            (s["user_id"], s["email"]),
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
            s["tenant_id"],
            tenant["name"],
            user["id"],
            s["email"],
            user["name"],
            scopes,
            s["csrf_token"],
            session_id,
            conn,
        )
    finally:
        conn.close()


def csrf_protected(request: Request, p: Principal = Depends(principal)) -> Principal:
    """For state-changing requests: same-origin and a matching per-session token."""
    if request.headers.get("origin") != config.APP_ORIGIN:
        raise HTTPException(403, "cross-origin request refused")
    token = request.headers.get("x-csrf-token", "")
    if not secrets.compare_digest(token, p.csrf_token):
        raise HTTPException(403, "missing or invalid CSRF token")
    return p
