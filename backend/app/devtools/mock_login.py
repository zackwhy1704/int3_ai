"""Sign in through the local test identity provider, exactly as a browser would.

Development and tests only. It drives the real flow: /api/auth/login -> the test
provider's login form -> /api/auth/callback, with the login cookie, and returns the
session cookie the server set. It works against an in-process TestClient or a live
server (any httpx.Client with a base_url).
"""

import json
from urllib.parse import urlsplit

import httpx

from .. import config


class LoginFailed(Exception):
    pass


def _set_cookie(resp: httpx.Response, name: str) -> str | None:
    for header in resp.headers.get_list("set-cookie"):
        key, _, rest = header.partition("=")
        if key.strip() == name:
            value = rest.split(";", 1)[0]
            return value or None
    return None


def login(client: httpx.Client, provider: str, subject: str, claims: dict) -> dict:
    """Returns {"cookie": "<name>=<value>", "csrf": token, "session": /api/session body}."""
    start = client.get(f"/api/auth/login/{provider}", follow_redirects=False)
    if start.status_code != 302:
        raise LoginFailed(f"login start returned {start.status_code}")
    state_cookie = _set_cookie(start, config.LOGIN_COOKIE)
    authorize = start.headers["location"]
    if config.MOCK_OIDC_PUBLIC_URL:
        authorize = authorize.replace(
            config.MOCK_OIDC_PUBLIC_URL, config.MOCK_OIDC_URL, 1
        )

    form = httpx.post(
        authorize,
        data={"username": subject, "claims": json.dumps(claims)},
        follow_redirects=False,
        timeout=15,
    )
    if form.status_code != 302:
        raise LoginFailed(f"test provider returned {form.status_code}")
    back = urlsplit(form.headers["location"])
    callback = client.get(
        f"{back.path}?{back.query}",
        follow_redirects=False,
        headers={"Cookie": f"{config.LOGIN_COOKIE}={state_cookie}"},
    )
    session_id = _set_cookie(callback, config.SESSION_COOKIE)
    if not session_id:
        raise LoginFailed(f"sign-in refused: {callback.headers.get('location')}")

    cookie = f"{config.SESSION_COOKIE}={session_id}"
    me = client.get("/api/session", headers={"Cookie": cookie})
    return {"cookie": cookie, "csrf": me.json()["csrf_token"], "session": me.json()}


def google_claims(email: str, hd: str | None = None, verified: bool = True) -> dict:
    c = {"email": email, "email_verified": verified}
    if hd:
        c["hd"] = hd
    return c


def microsoft_claims(email: str, tid: str, oid: str) -> dict:
    return {"email": email, "preferred_username": email, "tid": tid, "oid": oid}
