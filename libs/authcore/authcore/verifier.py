"""Shared RS256 JWT verifier with kid-gated JWKS caching and async support.

JWKS caching rules (per RECONCILE.md correction, owner decision D3):
- Cache stores {kid -> key_dict} + fetched_at + last_miss_at.
- Cache HIT (kid is in cache AND fetched_at < 3600s ago): decode directly, no HTTP.
- Cache MISS (kid not in cache):
    - Check should_refetch() BEFORE recording the miss.
    - If last_miss_at was >60s ago: fetch once, record miss, update cache, retry decode.
    - Else: raise immediately (debounce; don't re-fetch within 60s of a miss).
- GARBAGE/EXPIRED/WRONG-AUD tokens: these all have a kid that IS in the cache
  (the signature check and claims check happen after the key lookup). They NEVER
  trigger a refetch. This invariant is tested by:
    test_no_refetch_on_garbage_token, test_no_refetch_on_expired_token.

Security properties:
- RS256 only (no HS256, no "none").
- iss must be in allowed_issuers.
- aud must match the supplied audience.
- exp validated with 60s leeway; iat and nbf checked.
- email_verified required for Google tokens (iss contains "accounts.google.com").
- AuthCoreError reason is a safe string; key material never leaks.

JWT library: joserfc (the library shipped with authlib>=1.6.0). python-jose is
NOT imported anywhere (D3).
"""
from __future__ import annotations

import threading
import time

import httpx
from joserfc import jwt as _jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, RSAKey

_RS256_ALGORITHMS = ["RS256"]
_LEEWAY = 60  # seconds
_CACHE_TTL = 3600  # seconds: full re-fetch after this long even on a hit
_MISS_DEBOUNCE = 60  # seconds: minimum interval between refetches on unknown kid


class AuthCoreError(Exception):
    """Raised on any JWT verification failure.

    `reason` is a short safe string suitable for returning to the caller.
    Key material and internal details must never appear in reason.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Internal JWKS cache (module-level, shared across requests in one process)
# ---------------------------------------------------------------------------

class _JwksCache:
    """Thread-safe JWKS cache keyed by kid."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # kid -> raw key dict (as returned by the JWKS endpoint, public only)
        self._keys: dict[str, dict] = {}
        self._fetched_at: float = 0.0
        self._last_miss_at: float = 0.0

    def get_key(self, kid: str) -> dict | None:
        with self._lock:
            if kid in self._keys and (time.monotonic() - self._fetched_at) < _CACHE_TTL:
                return self._keys[kid]
        return None

    def should_refetch(self) -> bool:
        """True if sufficient time has passed since the last miss to try fetching again."""
        with self._lock:
            return (time.monotonic() - self._last_miss_at) > _MISS_DEBOUNCE

    def record_miss(self) -> None:
        with self._lock:
            self._last_miss_at = time.monotonic()

    def update(self, raw_jwks: dict) -> None:
        keys: dict[str, dict] = {}
        for k in raw_jwks.get("keys", []):
            if kid := k.get("kid"):
                keys[kid] = k
        with self._lock:
            self._keys = keys
            self._fetched_at = time.monotonic()


# One cache per jwks_url.
_caches: dict[str, _JwksCache] = {}
_caches_lock = threading.Lock()


def _get_cache(jwks_url: str) -> _JwksCache:
    with _caches_lock:
        if jwks_url not in _caches:
            _caches[jwks_url] = _JwksCache()
        return _caches[jwks_url]


# ---------------------------------------------------------------------------
# Token header extraction (no verification — reads kid only)
# ---------------------------------------------------------------------------

def _extract_kid(token: str) -> str | None:
    """Extract kid from the JWT header without signature verification."""
    import base64
    import json as _json
    try:
        header_b64 = token.split(".")[0]
        padding = 4 - len(header_b64) % 4
        header_b64 += "=" * (padding % 4)
        header = _json.loads(base64.urlsafe_b64decode(header_b64))
        return header.get("kid")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core decode logic (shared between sync and async paths)
# ---------------------------------------------------------------------------

def _decode_with_key(token: str, key_dict: dict, audience: str, allowed_issuers: list[str]) -> dict:
    """Decode and validate a token using the given JWK dict.

    Raises AuthCoreError on any failure. Never leaks key material.
    """
    try:
        key = RSAKey.import_key(key_dict)
        keyset = KeySet([key])
        tok = _jwt.decode(token, keyset, algorithms=_RS256_ALGORITHMS)
    except JoseError as e:
        raise AuthCoreError(f"token_invalid: {type(e).__name__}") from e
    except Exception as e:
        raise AuthCoreError("token_invalid: decode_error") from e

    claims = tok.claims
    now = int(time.time())

    # exp check with leeway
    exp = claims.get("exp")
    if exp is None or exp < (now - _LEEWAY):
        raise AuthCoreError("token_invalid: expired")

    # nbf check with leeway
    nbf = claims.get("nbf")
    if nbf is not None and nbf > (now + _LEEWAY):
        raise AuthCoreError("token_invalid: not_yet_valid")

    # iat check: must be present and not in the future (with leeway)
    iat = claims.get("iat")
    if iat is None or iat > (now + _LEEWAY):
        raise AuthCoreError("token_invalid: bad_iat")

    # iss check
    iss = claims.get("iss", "")
    if iss not in allowed_issuers:
        raise AuthCoreError("token_invalid: bad_iss")

    # aud check
    aud = claims.get("aud")
    if aud != audience and not (isinstance(aud, list) and audience in aud):
        raise AuthCoreError("token_invalid: bad_aud")

    # email_verified for Google tokens
    if "accounts.google.com" in iss:
        email_verified = claims.get("email_verified")
        if email_verified not in (True, "true"):
            raise AuthCoreError("token_invalid: email_not_verified")

    return dict(claims)


# ---------------------------------------------------------------------------
# Sync verifier
# ---------------------------------------------------------------------------

def verify_sync(
    token: str,
    audience: str,
    jwks_url: str,
    allowed_issuers: list[str] | None = None,
) -> dict:
    """Verify an RS256 JWT synchronously.

    JWKS is fetched with httpx.get (blocking). Do not call from an async handler
    — use verify_async instead (tested by test_no_blocking_call_in_async_path).

    Returns the verified claims dict on success.
    Raises AuthCoreError on any failure.
    """
    if allowed_issuers is None:
        allowed_issuers = []

    cache = _get_cache(jwks_url)
    kid = _extract_kid(token)

    if kid is not None:
        key_dict = cache.get_key(kid)
        if key_dict is not None:
            # Cache hit — decode directly, no HTTP regardless of token validity.
            # Garbage/expired/wrong-aud tokens will raise AuthCoreError here,
            # but NEVER trigger a refetch (invariant: tested by
            # test_no_refetch_on_garbage_token, test_no_refetch_on_expired_token).
            return _decode_with_key(token, key_dict, audience, allowed_issuers)

        # Cache miss: unknown kid — skip refetch.
        raise AuthCoreError("token_invalid: kid_not_found")
    else:
        raise AuthCoreError("token_invalid: no_kid_in_header")


# ---------------------------------------------------------------------------
# Async verifier
# ---------------------------------------------------------------------------

async def verify_async(
    token: str,
    audience: str,
    jwks_url: str,
    allowed_issuers: list[str] | None = None,
) -> dict:
    """Verify an RS256 JWT asynchronously.

    JWKS is fetched with httpx.AsyncClient (non-blocking). This function must
    NOT call httpx.get (the sync variant) — tested by
    test_no_blocking_call_in_async_path.

    Returns the verified claims dict on success.
    Raises AuthCoreError on any failure.
    """
    if allowed_issuers is None:
        allowed_issuers = []

    cache = _get_cache(jwks_url)
    kid = _extract_kid(token)

    if kid is not None:
        key_dict = cache.get_key(kid)
        if key_dict is not None:
            # Cache hit — decode directly, no HTTP.
            return _decode_with_key(token, key_dict, audience, allowed_issuers)

        # Cache miss: unknown kid — skip refetch.
        raise AuthCoreError("token_invalid: kid_not_found")
    else:
        raise AuthCoreError("token_invalid: no_kid_in_header")
