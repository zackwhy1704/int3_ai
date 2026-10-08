"""OpenID Connect sign-in: authorization code flow with PKCE, state and nonce.

Only the backend sees tokens. The ID token is verified here (signature against the
provider's JWKS, algorithm RS256 only, iss, aud, exp/nbf/iat, nonce) before any claim
in it is used.

Two provider kinds, with different trust rules for the email claim:
- google:    email must be verified by Google (email_verified). Subject = sub.
- microsoft: Entra's email claim is not guaranteed verified and can be user-editable
             ("nOAuth"), so the identity is bound to tid + oid, and the tenant must
             name the Entra tenant (tid) it accepts. Subject = "<tid>:<oid>".

Phase B bearer token path:
- verify(token, audience) uses authcore.verify_sync (RS256, kid-gated JWKS caching,
  no HS256, no "none"). Maps AuthCoreError -> LoginRejected.
- verify_id_token() is the OIDC code-flow path (authlib); unchanged so that
  test_id_token.py continues to pass unchanged.
"""
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError

from .. import config

# ---------------------------------------------------------------------------
# Phase B: authcore-based bearer token verification (Google RS256 JWTs).
#
# GOOGLE_JWKS_URL and GOOGLE_ISSUERS are the correct values for Google OIDC.
# verify() is the entry point used by principal.py (Phase B); it does NOT
# replace verify_id_token() (which handles the OIDC code flow and is tested
# by test_id_token.py, which must continue to pass unchanged).
# ---------------------------------------------------------------------------
from authcore.verifier import AuthCoreError, verify_sync as _authcore_verify_sync  # noqa: E402

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["accounts.google.com", "https://accounts.google.com"]


def verify(token: str, audience: str) -> dict:
    """Verify a Google RS256 bearer token using authcore.

    Returns the verified claims dict on success.
    Raises LoginRejected on any failure.

    This is the Phase B bearer token path; it uses authcore's kid-gated JWKS
    cache (no HS256, no 'none', RS256 only). The OIDC code-flow path
    (verify_id_token) is separate and unchanged.
    """
    try:
        return _authcore_verify_sync(token, audience, GOOGLE_JWKS_URL, GOOGLE_ISSUERS)
    except AuthCoreError as exc:
        raise LoginRejected("rejected", f"bearer token invalid: {exc.reason}") from exc

JWT = JsonWebToken(["RS256"])
LEEWAY_SECONDS = 60
LOGIN_TTL = timedelta(minutes=10)


class LoginRejected(Exception):
    """reason is a short code shown to the user; detail goes to the server log only."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    kind: str                 # "google" | "microsoft"
    discovery_url: str
    client_id: str
    client_secret: str
    internal_base: str = ""   # rewrite authorize URLs from this base ...
    public_base: str = ""     # ... to this one (test identity provider only)


@dataclass(frozen=True)
class VerifiedIdentity:
    provider: str
    kind: str
    email: str
    subject: str
    hd: str | None = None     # Google hosted domain
    tid: str | None = None    # Microsoft Entra tenant


def providers() -> dict[str, Provider]:
    out = {}
    if config.GOOGLE_CLIENT_ID:
        out["google"] = Provider("google", "Google", "google",
                                 "https://accounts.google.com/.well-known/openid-configuration",
                                 config.GOOGLE_CLIENT_ID, config.GOOGLE_CLIENT_SECRET)
    if config.MS_CLIENT_ID:
        out["microsoft"] = Provider(
            "microsoft", "Microsoft", "microsoft",
            "https://login.microsoftonline.com/organizations/v2.0/.well-known/openid-configuration",
            config.MS_CLIENT_ID, config.MS_CLIENT_SECRET)
    if config.MOCK_OIDC_URL:
        for kind in ("google", "microsoft"):
            out[f"mock-{kind}"] = Provider(
                f"mock-{kind}", f"Test {kind.title()} (dev only)", kind,
                f"{config.MOCK_OIDC_URL}/{kind}/.well-known/openid-configuration",
                "company-brain-dev", "dev-secret",
                config.MOCK_OIDC_URL, config.MOCK_OIDC_PUBLIC_URL or config.MOCK_OIDC_URL)
    return out


def assert_safe_config() -> None:
    """Refuse to start a production server that trusts the test identity provider."""
    if config.ENV == "production" and config.MOCK_OIDC_URL:
        raise RuntimeError("MOCK_OIDC_URL is set with ENV=production; refusing to start")
    if config.ENV == "production" and not config.APP_ORIGIN.startswith("https://"):
        raise RuntimeError("APP_ORIGIN must be https in production")


_cache: dict[str, tuple[float, dict]] = {}


def _get_json(url: str, ttl: int = 600) -> dict:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    data = httpx.get(url, timeout=10).raise_for_status().json()
    _cache[url] = (time.time(), data)
    return data


def redirect_uri(provider: Provider) -> str:
    return f"{config.APP_ORIGIN}/api/auth/callback/{provider.id}"


def start_login(control, provider: Provider) -> tuple[str, str]:
    """Record a single-use login attempt; return (authorize URL, state)."""
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    control.execute("DELETE FROM login_attempts WHERE expires_at < now()")
    control.execute(
        "INSERT INTO login_attempts (state, nonce, code_verifier, provider, expires_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (state, nonce, verifier, provider.id, datetime.now(timezone.utc) + LOGIN_TTL))
    endpoint = _get_json(provider.discovery_url)["authorization_endpoint"]
    if provider.public_base:
        endpoint = endpoint.replace(provider.internal_base, provider.public_base, 1)
    query = urlencode({
        "client_id": provider.client_id, "response_type": "code", "scope": "openid email profile",
        "redirect_uri": redirect_uri(provider), "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
    })
    return f"{endpoint}?{query}", state


def finish_login(control, provider: Provider, code: str, state: str,
                 browser_state: str | None) -> VerifiedIdentity:
    # The state must come back to the same browser that started the login (cookie),
    # and each attempt can be used once, within 10 minutes, for the same provider.
    if not browser_state or not secrets.compare_digest(browser_state, state):
        raise LoginRejected("rejected", "state does not match this browser's login cookie")
    attempt = control.execute(
        "UPDATE login_attempts SET used_at = now() WHERE state = %s AND used_at IS NULL"
        " AND expires_at > now() AND provider = %s RETURNING nonce, code_verifier",
        (state, provider.id)).fetchone()
    if attempt is None:
        raise LoginRejected("rejected", "unknown, expired, reused or cross-provider state")

    meta = _get_json(provider.discovery_url)
    resp = httpx.post(meta["token_endpoint"], timeout=15, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(provider),
        "client_id": provider.client_id, "client_secret": provider.client_secret,
        "code_verifier": attempt["code_verifier"],
    })
    if resp.status_code != 200:
        raise LoginRejected("rejected", f"token endpoint returned {resp.status_code}")
    claims = verify_id_token(provider, meta, resp.json().get("id_token", ""), attempt["nonce"])
    return identity_from_claims(provider, claims)


def verify_id_token(provider: Provider, meta: dict, id_token: str, nonce: str) -> dict:
    try:
        keys = JsonWebKey.import_key_set(_get_json(meta["jwks_uri"]))
        claims = JWT.decode(id_token, keys)
        claims.validate(leeway=LEEWAY_SECONDS)  # exp, nbf, iat
    except (JoseError, ValueError) as e:
        raise LoginRejected("rejected", f"id_token invalid: {type(e).__name__}")

    expected_iss = meta["issuer"]
    if "{tenantid}" in expected_iss:  # Microsoft's multi-tenant discovery document
        expected_iss = expected_iss.replace("{tenantid}", str(claims.get("tid", "")))
    aud = claims.get("aud")
    checks = {
        "iss": claims.get("iss") == expected_iss,
        "aud": aud == provider.client_id or (isinstance(aud, list) and provider.client_id in aud),
        "nonce": secrets.compare_digest(str(claims.get("nonce", "")), nonce),
        "exp": "exp" in claims,
    }
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        raise LoginRejected("rejected", f"id_token failed checks: {failed}")
    return dict(claims)


def identity_from_claims(provider: Provider, c: dict) -> VerifiedIdentity:
    if provider.kind == "google":
        if c.get("email_verified") not in (True, "true"):
            raise LoginRejected("email_not_verified", "google email_verified is not true")
        if not c.get("email") or not c.get("sub"):
            raise LoginRejected("rejected", "google token missing email or sub")
        return VerifiedIdentity(provider.id, "google", c["email"].lower(), c["sub"], hd=c.get("hd"))
    tid, oid = c.get("tid"), c.get("oid")
    email = c.get("email") or c.get("preferred_username")
    if not tid or not oid or not email:
        raise LoginRejected("rejected", "microsoft token missing tid, oid or email")
    return VerifiedIdentity(provider.id, "microsoft", email.lower(), f"{tid}:{oid}", tid=tid)
