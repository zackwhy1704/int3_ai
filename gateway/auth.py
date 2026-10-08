"""
Bearer token verification for the gateway.

GATEWAY_AUTH=none  — pass-through (dev only; must be set explicitly).
GATEWAY_AUTH=oidc  — RS256 Google OIDC verification via oidc.py.
"""
from __future__ import annotations

import logging
import os

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from oidc import verify as _oidc_verify

log = logging.getLogger("auth")

GATEWAY_AUTH = os.getenv("GATEWAY_AUTH", "")
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "")

_security = HTTPBearer(auto_error=False)


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    """Returns caller identity (sub). Raises 401/501 on failure."""
    if GATEWAY_AUTH == "none":
        return "dev"

    if GATEWAY_AUTH == "oidc":
        if not credentials:
            raise HTTPException(401, "Missing Authorization header")
        try:
            payload = _oidc_verify(credentials.credentials, OIDC_AUDIENCE)
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        return payload.get("sub", "unknown")

    raise HTTPException(
        501,
        "Gateway auth not configured. "
        "Set GATEWAY_AUTH=none (dev) or GATEWAY_AUTH=oidc (production).",
    )
