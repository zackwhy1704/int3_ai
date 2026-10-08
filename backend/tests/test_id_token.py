"""ID token verification, with tokens signed by a key this test controls, so every
bad-token shape can be produced (the test provider won't mint most of them)."""

import time

import pytest
from authlib.jose import JsonWebKey, jwt

from app.auth import oidc

ISS, AUD, NONCE = "https://issuer.example", "client-1", "nonce-1"
KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True)
OTHER_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True)
PROVIDER = oidc.Provider(
    "t", "T", "google", "https://issuer.example/.well-known", AUD, "s"
)
META = {"issuer": ISS, "jwks_uri": "https://issuer.example/jwks"}


@pytest.fixture(autouse=True)
def jwks(monkeypatch):
    public = {"keys": [KEY.as_dict(is_private=False) | {"kid": "k1"}]}
    monkeypatch.setattr(oidc, "_get_json", lambda url, ttl=600: public)


def token(key=KEY, alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISS,
        "aud": AUD,
        "sub": "s1",
        "nonce": NONCE,
        "iat": now,
        "exp": now + 300,
        "email": "a@b.example",
        "email_verified": True,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode({"alg": alg, "kid": "k1"}, claims, key).decode()


def test_valid_token_passes():
    assert oidc.verify_id_token(PROVIDER, META, token(), NONCE)["sub"] == "s1"


@pytest.mark.parametrize(
    "bad",
    [
        dict(iss="https://evil.example"),
        dict(aud="someone-else"),
        dict(nonce="other-nonce"),
        dict(exp=int(time.time()) - 3600),
        dict(exp=None),
    ],
)
def test_bad_claims_are_rejected(bad):
    with pytest.raises(oidc.LoginRejected):
        oidc.verify_id_token(PROVIDER, META, token(**bad), NONCE)


def test_token_signed_by_another_key_is_rejected():
    with pytest.raises(oidc.LoginRejected):
        oidc.verify_id_token(PROVIDER, META, token(key=OTHER_KEY), NONCE)


def _b64(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_hs256_token_using_the_public_key_as_secret_is_rejected():
    # Algorithm confusion: sign with HMAC, using the provider's public key as the secret.
    # Built by hand because authlib refuses to mint it.
    import hashlib
    import hmac
    import json

    header = _b64(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
    payload = _b64(
        json.dumps(
            {
                "iss": ISS,
                "aud": AUD,
                "sub": "s1",
                "nonce": NONCE,
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
            }
        ).encode()
    )
    secret = KEY.as_pem(is_private=False)
    sig = _b64(
        hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    with pytest.raises(oidc.LoginRejected):
        oidc.verify_id_token(PROVIDER, META, f"{header}.{payload}.{sig}", NONCE)


def test_unsigned_token_is_rejected():
    import base64
    import json

    h = (
        base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode())
        .rstrip(b"=")
        .decode()
    )
    p = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"iss": ISS, "aud": AUD, "nonce": NONCE, "exp": int(time.time()) + 300}
            ).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(oidc.LoginRejected):
        oidc.verify_id_token(PROVIDER, META, f"{h}.{p}.", NONCE)


def test_microsoft_issuer_is_checked_against_the_token_tid():
    ms = oidc.Provider("m", "M", "microsoft", "x", AUD, "s")
    meta = {
        "issuer": "https://login.microsoftonline.com/{tenantid}/v2.0",
        "jwks_uri": "x",
    }
    good = token(iss="https://login.microsoftonline.com/tid-1/v2.0", tid="tid-1")
    assert oidc.verify_id_token(ms, meta, good, NONCE)["tid"] == "tid-1"
    mismatched = token(iss="https://login.microsoftonline.com/tid-2/v2.0", tid="tid-1")
    with pytest.raises(oidc.LoginRejected):
        oidc.verify_id_token(ms, meta, mismatched, NONCE)
