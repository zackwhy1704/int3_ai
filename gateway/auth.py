"""
Bearer token verification for the gateway.

GATEWAY_AUTH=none  — pass-through (must be set explicitly; default rejects).
GATEWAY_AUTH=oidc  — Gate 4: full RS256 OIDC verification (stub rejects until implemented).

Gate 0 item 5 fix: auth no longer fails open. Any mode that is not explicitly
"none" will reject requests until the verification is implemented.
"""

from __future__ import annotations

import os

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# No default — if the env var is not set the gateway rejects everything.
# docker-compose.yml sets GATEWAY_AUTH=none explicitly for local dev.
GATEWAY_AUTH = os.getenv("GATEWAY_AUTH", "")
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "")

_security = HTTPBearer(auto_error=False)


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    """
    Returns caller identity on success. Raises 401/501 on failure.
    """
    if GATEWAY_AUTH == "none":
        return "dev"

    if GATEWAY_AUTH == "oidc":
        if not credentials:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        return _verify_google_oidc(credentials.credentials)

    # Unknown or unset mode — reject everything (fail safe).
    raise HTTPException(
        status_code=501,
        detail=(
            "Gateway auth not configured. "
            "Set GATEWAY_AUTH=none for local dev or GATEWAY_AUTH=oidc for production."
        ),
    )


def _verify_google_oidc(token: str) -> str:
    """
    TODO Gate 4: verify Google OIDC ID token (RS256).

    Implementation steps:
    1. GET https://www.googleapis.com/oauth2/v3/certs — cache 1 hour.
    2. Decode token header → kid → matching JWK.
    3. Verify RS256 signature with python-jose.
    4. Assert claims: exp > now, iss in {"accounts.google.com",
       "https://accounts.google.com"}, aud == OIDC_AUDIENCE.
    5. Return token["sub"].

    Until this is implemented, GATEWAY_AUTH=oidc rejects every request.
    """
    raise HTTPException(
        status_code=501,
        detail="OIDC verification not yet implemented. Use GATEWAY_AUTH=none for local dev.",
    )
