"""
Google OIDC ID token verification (RS256).

Identical copy of gateway/oidc.py — kept separate so gateway and backend
remain independent deployable services with no shared import path.
Update both files together when changing verification logic.
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
_CACHE_TTL_S = 3600

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
    if not audience:
        raise ValueError("OIDC_AUDIENCE is not configured")
    jwks = _fetch_jwks()
    try:
        payload = _decode(token, jwks, audience)
    except JWTError:
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
    return jwt.decode(token, jwks, algorithms=["RS256"], audience=audience)
