"""
Bearer token verification for the gateway.

Gate 2: GATEWAY_AUTH=none — pass-through, no verification. Local dev only.
Gate 4: GATEWAY_AUTH=oidc — full RS256 OIDC verification against Google's JWKS.

The caller identity string returned here is passed to the route handler for
future metering / audit logging (not yet implemented).
"""

import os
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi import Depends

GATEWAY_AUTH = os.getenv("GATEWAY_AUTH", "oidc")
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "")  # Google OAuth client_id

_security = HTTPBearer(auto_error=False)


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    """
    Returns the caller identity (sub claim or "dev") if the token is valid.
    Raises HTTP 401 if the token is missing or invalid.
    """
    if GATEWAY_AUTH == "none":
        # Development mode — skip verification entirely.
        # Never deploy with GATEWAY_AUTH=none.
        return "dev"

    if not credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = credentials.credentials

    if GATEWAY_AUTH == "oidc":
        return _verify_google_oidc(token)

    raise HTTPException(status_code=500, detail=f"Unknown GATEWAY_AUTH mode: {GATEWAY_AUTH}")


def _verify_google_oidc(token: str) -> str:
    """
    TODO Gate 4: verify Google OIDC ID token.

    Steps:
    1. Fetch Google's JWKS from https://www.googleapis.com/oauth2/v3/certs
       (cache with a short TTL, e.g. 1 hour).
    2. Decode the token header to get the `kid` (key ID).
    3. Find the matching JWK and verify the RS256 signature.
    4. Validate claims: exp, iss (accounts.google.com), aud (OIDC_AUDIENCE).
    5. Return token["sub"] as the caller identity.

    Recommended library: python-jose[cryptography] (already in requirements.txt).

    For now, this is a stub that accepts any non-empty token in dev/test.
    Replace before Gate 4 ships.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")

    # STUB — replace with real verification at Gate 4.
    # Returning "stub-unverified" so it's obvious in logs that this is not real auth.
    return "stub-unverified"
