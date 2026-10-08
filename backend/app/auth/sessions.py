"""Server-side sessions and the rule that maps a verified identity to a tenant.

A session id is 256 random bits, sent only in an httpOnly __Host- cookie; the
control database stores its SHA-256. Sessions end after 30 minutes idle or 8 hours
in total, on logout, or as soon as the identity behind them is removed.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

import psycopg

from .. import config
from ..db import tenant_conn
from .oidc import LoginRejected, VerifiedIdentity

log = logging.getLogger("auth")


def _hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def email_ref(email: str) -> str:
    """Log-safe reference to an email address."""
    return hashlib.sha256(email.encode()).hexdigest()[:12]


def authorize(control, ident: VerifiedIdentity) -> dict:
    """Invited identity + tenant sign-in policy + subject binding + active user.
    Returns the identity row joined with its tenant, or raises LoginRejected."""
    row = control.execute(
        "SELECT i.*, t.google_domain, t.ms_tenant_id FROM identities i"
        " JOIN tenants t ON t.id = i.tenant_id WHERE i.email = %s", (ident.email,)).fetchone()
    if row is None:
        raise LoginRejected("not_invited", "no identity row for this email")

    if ident.kind == "google":
        if row["google_domain"] and ident.hd != row["google_domain"]:
            raise LoginRejected("rejected", "google hd does not match the tenant's domain")
        column = "google_sub"
    else:
        if not row["ms_tenant_id"] or ident.tid != row["ms_tenant_id"]:
            raise LoginRejected("rejected", "microsoft tid is not the tenant's Entra tenant")
        column = "ms_subject"

    bound = row[column]
    if bound is None:
        try:
            bound = control.execute(
                f"UPDATE identities SET {column} = %s WHERE email = %s AND {column} IS NULL"
                f" RETURNING {column}", (ident.subject, ident.email)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise LoginRejected("rejected", f"{column} already bound to another email") from None
        bound = bound[column] if bound else control.execute(
            f"SELECT {column} FROM identities WHERE email = %s", (ident.email,)).fetchone()[column]
    if bound != ident.subject:
        raise LoginRejected("rejected", f"{column} does not match the identity bound at first sign-in")

    with tenant_conn(row["tenant_id"]) as conn:
        user = conn.execute("SELECT id FROM users WHERE id = %s AND email = %s AND active",
                            (row["user_id"], ident.email)).fetchone()
    if user is None:
        raise LoginRejected("rejected", "user missing or inactive in tenant database")
    return row


def create(control, identity: dict) -> tuple[str, str]:
    """Start a session; returns (session id for the cookie, csrf token)."""
    session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    control.execute("DELETE FROM sessions WHERE expires_at < now()"
                    " OR last_seen < now() - make_interval(mins => %s)", (config.SESSION_IDLE_MINUTES,))
    control.execute(
        "INSERT INTO sessions (id_hash, email, tenant_id, user_id, csrf_token, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (_hash(session_id), identity["email"], identity["tenant_id"], identity["user_id"], csrf,
         datetime.now(timezone.utc) + timedelta(hours=config.SESSION_ABSOLUTE_HOURS)))
    return session_id, csrf


def lookup(control, session_id: str) -> dict | None:
    """The live session for this id, touching last_seen; None if expired, idle, or if
    its identity no longer maps to the same tenant and user."""
    return control.execute(
        "UPDATE sessions s SET last_seen = now() FROM identities i"
        " WHERE s.id_hash = %s AND s.expires_at > now()"
        " AND s.last_seen > now() - make_interval(mins => %s)"
        " AND i.email = s.email AND i.tenant_id = s.tenant_id AND i.user_id = s.user_id"
        " RETURNING s.*", (_hash(session_id), config.SESSION_IDLE_MINUTES)).fetchone()


def delete(control, session_id: str) -> None:
    control.execute("DELETE FROM sessions WHERE id_hash = %s", (_hash(session_id),))
