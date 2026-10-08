"""
Google OIDC ID token verification (RS256).

Shared by gateway/auth.py and backend/app/v1.py (each service copies this
module; do not import across service boundaries).

Caches Google's JWKS for one hour to avoid rate-limiting on every request.
Re-fetches automatically after cache expiry or on key-not-found (handles
Google's rolling key rotation gracefully).
"""
from __future__ import annotations

import logging
import threading
import time

import httpx
from jose import JWTError, jwt

log = logging.getLogger("oidc")

_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_ISSUERS = frozenset(
    {"accounts.google.com", "https://accounts.google.com"}
)
_CACHE_TTL_S = 3600  # 1 hour

_lock = threading.Lock()
_jwks: dict | None = None
_fetched_at: float = 0.0


def _fetch_jwks(force: bool = False) -> dict:
    global _jwks, _fetched_at
    with _lock:
        if not force and _jwks is not None and (time.time() - _fetched_at) < _CACHE_TTL_S:
            return _jwks
        log.debug("fetching Google JWKS")
        resp = httpx.get(_GOOGLE_CERTS_URL, timeout=10)
        resp.raise_for_status()
        _jwks = resp.json()
        _fetched_at = time.time()
        return _jwks


def verify(token: str, audience: str) -> dict:
    """
    Verify a Google OIDC ID token.

    Returns the decoded payload dict on success.
    Raises ValueError with a safe message on any failure (do not expose raw
    jose errors to callers as they may leak key material).
    """
    if not audience:
        raise ValueError("OIDC_AUDIENCE is not configured")

    jwks = _fetch_jwks()
    try:
        payload = _decode(token, jwks, audience)
    except JWTError:
        # Key may have been rotated since last cache; try once with fresh JWKS.
        log.info("JWT decode failed with cached JWKS, retrying with fresh keys")
        jwks = _fetch_jwks(force=True)
        try:
            payload = _decode(token, jwks, audience)
        except JWTError as exc:
            log.warning("OIDC token rejected: %s", exc)
            raise ValueError("invalid or expired token") from exc

    iss = payload.get("iss", "")
    if iss not in _GOOGLE_ISSUERS:
        raise ValueError(f"unexpected issuer: {iss!r}")

    return payload


def _decode(token: str, jwks: dict, audience: str) -> dict:
    return jwt.decode(
        token,
        jwks,
        algorithms=["RS256"],
        audience=audience,
        # python-jose validates exp and aud automatically.
    )
