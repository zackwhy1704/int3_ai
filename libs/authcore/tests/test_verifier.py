"""Unit tests for authcore.verifier.

Security invariants tested:
- test_no_refetch_on_garbage_token: garbage signature → AuthCoreError, no extra JWKS fetch
- test_no_refetch_on_expired_token: expired token → AuthCoreError, no extra JWKS fetch
- test_refetch_on_unknown_kid: unknown kid → exactly one extra JWKS fetch
- test_debounce_60s: two consecutive unknown-kid tokens within 60s → one extra fetch total
- test_no_blocking_call_in_async_path: verify_async must not call httpx.get (sync)

These run with `pytest -m unit` — no Docker, no network, no LLM.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from joserfc import jwt as _jwt
from joserfc.jwk import RSAKey, KeySet

from authcore.verifier import (
    AuthCoreError,
    _JwksCache,
    _caches,
    verify_async,
    verify_sync,
    _get_cache,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Key material for tests
# ---------------------------------------------------------------------------

def _make_rsa_key(kid: str) -> RSAKey:
    """Generate an RSA key with the given kid embedded."""
    key = RSAKey.generate_key(2048, private=True)
    params = key.as_dict(private=True)
    params["kid"] = kid
    return RSAKey.import_key(params)


KEY = _make_rsa_key("test-kid-1")
OTHER_KEY = _make_rsa_key("test-kid-2")
KID = "test-kid-1"
OTHER_KID = "test-kid-2"

ISS = "https://accounts.google.com"
AUD = "test-audience"


def _public_jwks(key: RSAKey = KEY, kid: str = KID) -> dict:
    pub = key.as_dict(private=False)
    pub["kid"] = kid
    return {"keys": [pub]}


def _make_token(
    key: RSAKey = KEY,
    kid: str = KID,
    iss: str = ISS,
    aud: str = AUD,
    exp_offset: int = 300,
    email_verified: bool | str = True,
    **extra,
) -> str:
    now = int(time.time())
    claims: dict = {
        "iss": iss,
        "aud": aud,
        "sub": "test-sub",
        "iat": now,
        "exp": now + exp_offset,
        "email": "test@example.com",
        "email_verified": email_verified,
    }
    claims.update(extra)
    return _jwt.encode({"alg": "RS256", "kid": kid}, claims, key)


def _fresh_cache(jwks_url: str, kid: str = KID, key: RSAKey = KEY) -> _JwksCache:
    """Return a freshly populated cache with the given kid."""
    cache = _JwksCache()
    cache.update(_public_jwks(key=key, kid=kid))
    return cache


def _inject_cache(jwks_url: str, cache: _JwksCache) -> None:
    """Replace the module-level cache for a given URL (test isolation)."""
    _caches[jwks_url] = cache


# ---------------------------------------------------------------------------
# Helper: count httpx.get calls
# ---------------------------------------------------------------------------

class _FetchCounter:
    def __init__(self, response: dict):
        self.calls = 0
        self.response = response

    def sync_get(self, url, **kwargs):
        self.calls += 1
        mock = MagicMock()
        mock.raise_for_status.return_value = mock
        mock.json.return_value = self.response
        return mock


# ---------------------------------------------------------------------------
# test_no_refetch_on_garbage_token
#
# Security invariant: a token whose kid IS in the cache but whose signature is
# garbage must raise AuthCoreError without triggering an extra JWKS fetch.
# (Invariant: refetch gate is kid-based, not error-based.)
# ---------------------------------------------------------------------------

def test_no_refetch_on_garbage_token():
    url = "https://jwks.example/garbage"
    cache = _fresh_cache(url, KID, KEY)
    _inject_cache(url, cache)

    # Build a token with the right kid but a corrupted signature.
    good = _make_token(key=KEY, kid=KID)
    parts = good.split(".")
    corrupted = parts[0] + "." + parts[1] + ".AAAAAAAAAAAAA"

    counter = _FetchCounter(_public_jwks())
    with patch("authcore.verifier.httpx.get", side_effect=counter.sync_get):
        with pytest.raises(AuthCoreError):
            verify_sync(corrupted, AUD, url, [ISS])

    assert counter.calls == 0, (
        "JWKS was re-fetched for a token with a known kid but garbage signature. "
        "Refetch must only happen on cache-miss (unknown kid)."
    )


# ---------------------------------------------------------------------------
# test_no_refetch_on_expired_token
#
# Security invariant: a well-formed expired token whose kid IS in the cache
# must raise AuthCoreError without triggering an extra JWKS fetch.
# ---------------------------------------------------------------------------

def test_no_refetch_on_expired_token():
    url = "https://jwks.example/expired"
    cache = _fresh_cache(url, KID, KEY)
    _inject_cache(url, cache)

    expired = _make_token(key=KEY, kid=KID, exp_offset=-3600)

    counter = _FetchCounter(_public_jwks())
    with patch("authcore.verifier.httpx.get", side_effect=counter.sync_get):
        with pytest.raises(AuthCoreError):
            verify_sync(expired, AUD, url, [ISS])

    assert counter.calls == 0, (
        "JWKS was re-fetched for an expired token with a known kid. "
        "Expired tokens are not a cache miss — they must never trigger a refetch."
    )


# ---------------------------------------------------------------------------
# test_refetch_on_unknown_kid
#
# Security invariant: a token with an unknown kid triggers exactly one extra
# JWKS fetch, then succeeds if the new JWKS contains the kid.
# ---------------------------------------------------------------------------

def test_refetch_on_unknown_kid():
    url = "https://jwks.example/unknown-kid"
    # Populate cache with OTHER_KID only; last_miss_at = 0 allows a refetch.
    cache = _fresh_cache(url, OTHER_KID, OTHER_KEY)
    cache._last_miss_at = 0.0
    _inject_cache(url, cache)

    token = _make_token(key=KEY, kid=KID)
    new_jwks = _public_jwks(KEY, KID)

    counter = _FetchCounter(new_jwks)
    with patch("authcore.verifier.httpx.get", side_effect=counter.sync_get):
        result = verify_sync(token, AUD, url, [ISS])

    assert result["sub"] == "test-sub"
    assert counter.calls == 1, (
        f"Expected exactly 1 JWKS fetch for unknown kid, got {counter.calls}."
    )


# ---------------------------------------------------------------------------
# test_debounce_60s
#
# Security invariant: two consecutive unknown-kid tokens within 60s must
# trigger only one extra JWKS fetch total (debounce).
# ---------------------------------------------------------------------------

def test_debounce_60s():
    url = "https://jwks.example/debounce"
    # Empty cache, last_miss_at = 0 (allows first fetch).
    cache = _JwksCache()
    cache._last_miss_at = 0.0
    _inject_cache(url, cache)

    token = _make_token(key=KEY, kid=KID)
    new_jwks = _public_jwks(KEY, KID)

    counter = _FetchCounter(new_jwks)
    with patch("authcore.verifier.httpx.get", side_effect=counter.sync_get):
        # First call: cache miss, refetch allowed → fetch happens, decode succeeds.
        result1 = verify_sync(token, AUD, url, [ISS])
        assert result1["sub"] == "test-sub"
        assert counter.calls == 1

        # Second call with a DIFFERENT unknown kid within the debounce window.
        # should_refetch() returns False because last_miss_at was just set.
        token2 = _make_token(key=OTHER_KEY, kid=OTHER_KID)
        with pytest.raises(AuthCoreError):
            verify_sync(token2, AUD, url, [ISS])

        assert counter.calls == 1, (
            "A second unknown-kid token within 60s triggered an extra JWKS fetch. "
            "The debounce window must prevent this."
        )


# ---------------------------------------------------------------------------
# test_no_blocking_call_in_async_path
#
# Security/performance invariant: verify_async must not call httpx.get (sync).
# It must use httpx.AsyncClient.get instead.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_blocking_call_in_async_path():
    url = "https://jwks.example/async"
    cache = _JwksCache()
    cache._last_miss_at = 0.0
    _inject_cache(url, cache)

    token = _make_token(key=KEY, kid=KID)
    new_jwks = _public_jwks(KEY, KID)

    sync_counter = _FetchCounter(new_jwks)

    async_response = MagicMock()
    async_response.raise_for_status.return_value = async_response
    async_response.json.return_value = new_jwks
    async_client_mock = AsyncMock()
    async_client_mock.__aenter__ = AsyncMock(return_value=async_client_mock)
    async_client_mock.__aexit__ = AsyncMock(return_value=False)
    async_client_mock.get = AsyncMock(return_value=async_response)

    with patch("authcore.verifier.httpx.get", side_effect=sync_counter.sync_get):
        with patch("authcore.verifier.httpx.AsyncClient", return_value=async_client_mock):
            result = await verify_async(token, AUD, url, [ISS])

    assert result["sub"] == "test-sub"
    assert sync_counter.calls == 0, (
        "verify_async called httpx.get (blocking). "
        "The async path must use httpx.AsyncClient.get only."
    )
    assert async_client_mock.get.called, "verify_async did not call AsyncClient.get"


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

def test_valid_token_passes_sync():
    url = "https://jwks.example/happy"
    cache = _fresh_cache(url, KID, KEY)
    _inject_cache(url, cache)
    token = _make_token()
    result = verify_sync(token, AUD, url, [ISS])
    assert result["sub"] == "test-sub"
    assert result["iss"] == ISS


@pytest.mark.asyncio
async def test_valid_token_passes_async():
    url = "https://jwks.example/happy-async"
    cache = _fresh_cache(url, KID, KEY)
    _inject_cache(url, cache)
    token = _make_token()
    result = await verify_async(token, AUD, url, [ISS])
    assert result["sub"] == "test-sub"


def test_wrong_audience_raises():
    url = "https://jwks.example/wrong-aud"
    cache = _fresh_cache(url, KID, KEY)
    _inject_cache(url, cache)
    token = _make_token(aud="wrong-audience")
    with pytest.raises(AuthCoreError) as exc:
        verify_sync(token, AUD, url, [ISS])
    assert "bad_aud" in exc.value.reason


def test_wrong_issuer_raises():
    url = "https://jwks.example/wrong-iss"
    cache = _fresh_cache(url, KID, KEY)
    _inject_cache(url, cache)
    token = _make_token(iss="https://evil.example")
    with pytest.raises(AuthCoreError) as exc:
        verify_sync(token, AUD, url, [ISS])
    assert "bad_iss" in exc.value.reason


def test_email_not_verified_raises():
    url = "https://jwks.example/email-unverified"
    cache = _fresh_cache(url, KID, KEY)
    _inject_cache(url, cache)
    token = _make_token(email_verified=False)
    with pytest.raises(AuthCoreError) as exc:
        verify_sync(token, AUD, url, [ISS])
    assert "email_not_verified" in exc.value.reason


def test_wrong_key_raises():
    url = "https://jwks.example/wrong-key"
    cache = _fresh_cache(url, KID, KEY)
    _inject_cache(url, cache)
    # Token signed with OTHER_KEY but kid header says KID (the cached key)
    token = _make_token(key=OTHER_KEY, kid=KID)
    with pytest.raises(AuthCoreError):
        verify_sync(token, AUD, url, [ISS])
