"""Non-interactive bearer credentials for the /v1/ machine surface.

A service token maps to exactly one (tenant, acting user). It is NOT a session:
there is no cookie, no CSRF, no idle/absolute expiry window, and no session row.
Scopes are still resolved per-request from the user's memberships (in
principal._principal_for), so a token can never read more than its user can.

Only the SHA-256 of the token is stored, as with session ids.
"""

import hashlib
import secrets

_PREFIX = "cb_svc_"  # recognisable in logs/config; the secret is the part after it


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(control, tenant_id: str, user_id: str, label: str) -> str:
    """Create a service token for (tenant, user); return the plaintext ONCE.

    The caller is responsible for delivering it to the machine client and for
    never logging it. Only its hash is persisted.
    """
    token = _PREFIX + secrets.token_urlsafe(32)
    control.execute(
        "INSERT INTO service_tokens (token_hash, tenant_id, user_id, label)"
        " VALUES (%s, %s, %s, %s)",
        (_hash(token), tenant_id, user_id, label),
    )
    return token


def resolve(control, token: str) -> dict | None:
    """(tenant_id, user_id) for a live (non-revoked) token, else None."""
    if not token:
        return None
    return control.execute(
        "SELECT tenant_id, user_id FROM service_tokens"
        " WHERE token_hash = %s AND revoked_at IS NULL",
        (_hash(token),),
    ).fetchone()


def revoke(control, token: str) -> None:
    control.execute(
        "UPDATE service_tokens SET revoked_at = now() WHERE token_hash = %s",
        (_hash(token),),
    )
