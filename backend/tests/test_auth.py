"""Sign-in rules, driven through the full flow against the local test identity provider."""
import json
from urllib.parse import urlsplit

import httpx
import pytest

from app import config
from app.auth import oidc
from app.devtools import mock_login
from app.devtools.mock_login import google_claims, microsoft_claims
from app.tenants.admin import admin_conn

from .conftest import MS_TID_A


def refused(client, provider, subject, claims) -> str:
    """Attempt a sign-in that should fail; return the login_error reason."""
    with pytest.raises(mock_login.LoginFailed) as e:
        mock_login.login(client, provider, subject, claims)
    return str(e.value).rsplit("login_error=", 1)[-1]


def identity_count(email: str) -> int:
    with admin_conn("control") as c:
        return c.execute("SELECT count(*) AS n FROM identities WHERE email = %s", (email,)).fetchone()["n"]


def test_valid_google_sign_in(client):
    s = mock_login.login(client, "mock-google", "google-priya",
                         google_claims("priya@brindlewood.example", "brindlewood.example"))
    assert s["session"]["tenant"]["id"] == "brindlewood"
    assert s["session"]["user"]["id"] == "priya"


def test_uninvited_email_on_a_known_domain_is_refused_and_nothing_is_created(client):
    email = "nobody@brindlewood.example"
    assert refused(client, "mock-google", "google-nobody", google_claims(email, "brindlewood.example")) == "not_invited"
    assert identity_count(email) == 0


def test_unverified_google_email_is_refused(client):
    reason = refused(client, "mock-google", "google-priya",
                     google_claims("priya@brindlewood.example", "brindlewood.example", verified=False))
    assert reason == "email_not_verified"


def test_google_hosted_domain_must_match_the_tenant(client):
    reason = refused(client, "mock-google", "google-priya",
                     google_claims("priya@brindlewood.example", "evil.example"))
    assert reason == "rejected"


def test_google_subject_is_bound_at_first_sign_in(client):
    mock_login.login(client, "mock-google", "google-marcus",
                     google_claims("marcus@brindlewood.example", "brindlewood.example"))
    # Same verified email, different Google account: refused.
    reason = refused(client, "mock-google", "attacker-sub",
                     google_claims("marcus@brindlewood.example", "brindlewood.example"))
    assert reason == "rejected"


def test_microsoft_is_bound_to_tenant_and_object_id_not_email(client):
    # nOAuth: an attacker controls another Entra tenant and sets their email claim to a
    # victim's address. The tid does not match tenant A's, so it is refused.
    reason = refused(client, "mock-microsoft", "attacker",
                     microsoft_claims("ada@brindlewood.example", "ffffffff-0000-4000-8000-00000000000f", "evil-oid"))
    assert reason == "rejected"
    # The real user from the right Entra tenant signs in and binds tid:oid ...
    s = mock_login.login(client, "mock-microsoft", "ada-ms",
                         microsoft_claims("ada@brindlewood.example", MS_TID_A, "oid-ada"))
    assert s["session"]["user"]["id"] == "ada"
    # ... after which another object in the SAME Entra tenant claiming her email is refused.
    reason = refused(client, "mock-microsoft", "other",
                     microsoft_claims("ada@brindlewood.example", MS_TID_A, "oid-someone-else"))
    assert reason == "rejected"


def test_microsoft_is_refused_for_a_tenant_without_an_entra_tenant_configured(client):
    reason = refused(client, "mock-microsoft", "alex-ms",
                     microsoft_claims("alex@hollowmere.example", MS_TID_A, "oid-alex"))
    assert reason == "rejected"


def _start_and_authorize(client, provider="mock-google", subject="google-priya",
                         claims=None) -> tuple[str, str]:
    claims = claims or google_claims("priya@brindlewood.example", "brindlewood.example")
    start = client.get(f"/api/auth/login/{provider}", follow_redirects=False)
    state_cookie = mock_login._set_cookie(start, config.LOGIN_COOKIE)
    authorize = start.headers["location"].replace(config.MOCK_OIDC_PUBLIC_URL, config.MOCK_OIDC_URL, 1)
    form = httpx.post(authorize, data={"username": subject, "claims": json.dumps(claims)},
                      follow_redirects=False)
    back = urlsplit(form.headers["location"])
    return f"{back.path}?{back.query}", state_cookie


def test_callback_without_the_browser_login_cookie_is_refused(client):
    # Login CSRF: an attacker sends a victim their own callback URL.
    callback, _ = _start_and_authorize(client)
    r = client.get(callback, follow_redirects=False)
    assert r.headers["location"] == "/?login_error=rejected"


def test_callback_replay_is_refused(client):
    callback, state = _start_and_authorize(client)
    cookie = {"Cookie": f"{config.LOGIN_COOKIE}={state}"}
    first = client.get(callback, headers=cookie, follow_redirects=False)
    assert first.headers["location"] == "/"
    second = client.get(callback, headers=cookie, follow_redirects=False)
    assert second.headers["location"] == "/?login_error=rejected"


def test_state_from_one_provider_is_refused_at_another(client):
    callback, state = _start_and_authorize(client)
    swapped = callback.replace("/callback/mock-google", "/callback/mock-microsoft")
    r = client.get(swapped, headers={"Cookie": f"{config.LOGIN_COOKIE}={state}"}, follow_redirects=False)
    assert r.headers["location"] == "/?login_error=rejected"


def test_unknown_provider_is_404(client):
    assert client.get("/api/auth/login/evil", follow_redirects=False).status_code == 404


def test_production_refuses_to_trust_the_test_provider(monkeypatch):
    monkeypatch.setattr(config, "ENV", "production")
    monkeypatch.setattr(config, "APP_ORIGIN", "https://brain.example")
    monkeypatch.setattr(config, "MOCK_OIDC_URL", "http://mock-oidc:8080")
    with pytest.raises(RuntimeError, match="refusing to start"):
        oidc.assert_safe_config()
