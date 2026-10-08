"""
Bearer token verification for the gateway.

GATEWAY_AUTH=none  — pass-through (dev only; must be set explicitly in compose).
                     The gateway REFUSES to start with GATEWAY_AUTH=none when
                     ENV=production (guard in main.py lifespan).
GATEWAY_AUTH=oidc  — Phase B: full RS256 verification via authcore.verify_async.
                     Not yet wired; rejects every request until Phase B is complete.

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

_security = HTTPBearer(auto_error=False)


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    """
    Returns caller identity on success. Raises 401/501 on failure.

    Phase A: only GATEWAY_AUTH=none is functional (dev compose profile).
    Phase B: GATEWAY_AUTH=oidc will call authcore.verify_async.
    """
    if GATEWAY_AUTH == "none":
        return "dev"

    if GATEWAY_AUTH == "oidc":
        if not credentials:
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        # Phase B: wire authcore.verify_async here.
        raise HTTPException(
            status_code=501,
            detail=(
                "GATEWAY_AUTH=oidc is not yet implemented. "
                "Bearer token support is planned for Phase B."
            ),
        )

    # Unknown or unset mode — reject everything (fail safe).
    raise HTTPException(
        status_code=501,
        detail=(
            "Gateway auth not configured. "
            "Set GATEWAY_AUTH=none for local dev (dev compose profile only)."
        ),
    )
