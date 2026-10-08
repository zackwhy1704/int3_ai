"""
Bearer token verification for the gateway.

GATEWAY_AUTH=none  — pass-through (dev only; must be set explicitly in compose).
                     The gateway REFUSES to start with GATEWAY_AUTH=none when
                     ENV=production (guard in main.py lifespan).
                     Tested by: test_gateway_auth_none_rejected_in_production.
GATEWAY_AUTH=oidc  — Full RS256 verification via authcore.verify_async (async,
                     non-blocking). OIDC_AUDIENCE must be set.

Any other value — 501 Not Implemented (fail safe, not fail open).

NOTE: python-jose is NOT used here (D3: authlib via libs/authcore is the single
JWT library). See libs/authcore/authcore/verifier.py.
"""

from __future__ import annotations

import logging
import os

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger("auth")

# No default — if the env var is not set the gateway rejects everything.
# docker-compose.yml sets GATEWAY_AUTH=none explicitly for the dev profile.
GATEWAY_AUTH = os.getenv("GATEWAY_AUTH", "")
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "")

# Google JWKS and issuers for Phase B bearer token verification.
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = ["accounts.google.com", "https://accounts.google.com"]

_security = HTTPBearer(auto_error=False)


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    """
    Returns caller identity on success. Raises 401/501 on failure.

    GATEWAY_AUTH=none: dev pass-through (blocked in production by lifespan guard).
    GATEWAY_AUTH=oidc: full RS256 OIDC verification via authcore.verify_async
                       (async, non-blocking — tested by test_no_blocking_call_in_async_path).
    """
    if GATEWAY_AUTH == "none":
        return "dev"

    if GATEWAY_AUTH == "oidc":
        if not credentials:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        from authcore.verifier import AuthCoreError, verify_async

        try:
            claims = await verify_async(
                credentials.credentials,
                OIDC_AUDIENCE,
                _GOOGLE_JWKS_URL,
                _GOOGLE_ISSUERS,
            )
        except AuthCoreError as exc:
            log.warning("bearer token rejected: %s", exc.reason)
            raise HTTPException(status_code=401, detail="Invalid bearer token") from exc
        # Return the subject claim as the caller identity.
        return claims.get("sub", "unknown")

    # Unknown or unset mode — reject everything (fail safe).
    raise HTTPException(
        status_code=501,
        detail=(
            "Gateway auth not configured. "
            "Set GATEWAY_AUTH=none for local dev (dev compose profile only)."
        ),
    )
