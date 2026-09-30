"""Test fixtures. Run in the tools container (it has the superuser credentials needed
to provision fixture tenants; the app under test never uses them):

    docker compose --profile dev up -d
    docker compose run --rm tools pytest -v

Every signed-in request in these tests goes through the real sign-in flow against the
local test identity provider: signed ID tokens, JWKS verification, PKCE, state and
nonce, the tenant policy, subject binding and a server-side session cookie.
"""
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config
from app.devtools import mock_login
from app.main import app
from app.tenants import admin

SEED = Path("/seed")
MS_TID_A = "aaaaaaaa-0000-4000-8000-00000000000a"

# Tenant A (the demo company), B (a different company whose document ids collide with
# A's) and C (a different company that shares A's email domain).
TENANTS = {
    "brindlewood": dict(name="Brindlewood Supply Co.", seed=SEED / "brindlewood",
                        google_domain="brindlewood.example", ms_tenant_id=MS_TID_A),
    "hollowmere": dict(name="Hollowmere Packaging", seed=SEED / "fixtures/hollowmere",
                       google_domain="hollowmere.example", ms_tenant_id=None),
    "brindlewood_labs": dict(name="Brindlewood Labs", seed=SEED / "fixtures/brindlewood_labs",
                             google_domain="brindlewood.example", ms_tenant_id=None),
}
USERS = {  # email local part -> (tenant, email domain)
    "priya": ("brindlewood", "brindlewood.example"),
    "marcus": ("brindlewood", "brindlewood.example"),
    "ada": ("brindlewood", "brindlewood.example"),
    "alex": ("hollowmere", "hollowmere.example"),
    "sam": ("brindlewood_labs", "brindlewood.example"),
}
A_USERS = ["priya", "marcus", "ada"]


def provision_fixtures() -> None:
    for tid, t in TENANTS.items():
        if not admin.exists(tid):
            admin.provision(tid, t["name"], None, "Admin", t["google_domain"], t["ms_tenant_id"], t["seed"])


@pytest.fixture(scope="session")
def client():
    provision_fixtures()
    with TestClient(app, base_url="http://testserver") as c:
        yield c


_sessions: dict[str, dict] = {}


def headers_for(client, user: str) -> dict:
    """Headers for a signed-in request as `user` (session cookie, CSRF token, origin)."""
    if user not in _sessions:
        _, domain = USERS[user]
        s = mock_login.login(client, "mock-google", f"google-{user}",
                             mock_login.google_claims(f"{user}@{domain}", domain))
        _sessions[user] = {"Cookie": s["cookie"], "X-CSRF-Token": s["csrf"],
                           "Origin": config.APP_ORIGIN}
    return _sessions[user]


@pytest.fixture(scope="session")
def as_user(client):
    return lambda user: headers_for(client, user)


@pytest.fixture(scope="session")
def questions():
    return yaml.safe_load((SEED / "brindlewood/questions.yaml").read_text())


@pytest.fixture(scope="session")
def memberships():
    return yaml.safe_load((SEED / "brindlewood/users.yaml").read_text())["memberships"]
