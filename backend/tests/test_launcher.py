"""Launcher API (M0): invites, device sessions, the brain list, launch, introspect, BYOK link.

Real Postgres integration tests, run in the tools container like the rest of the suite:
  docker compose run --rm tools pytest -v backend/tests/test_launcher.py

Invariants proven here:
  - an invite is single use and expires; it opens exactly one device session
  - the brain list is the user's memberships, by id + name only (no content fields)
  - refresh tokens rotate; presenting a USED one revokes the whole device
  - a desktop token works only at the ingress of the backend it was minted for
  - revoking a device, deactivating the user or removing their identity kills every token
  - launcher, service and desktop tokens never stand in for one another
  - BYOK link and admin flag: admin only
"""

from __future__ import annotations

import pytest

from app import config
from app.auth import service_tokens
from app.db import control_conn
from app.launcher import tokens
from app.tenants.admin import admin_conn, admin_tenant_conn
from .conftest import _sessions

INGRESS = {"priya": "cb_ing_test-priya", "ada": "cb_ing_test-ada", "alex": "cb_ing_test-alex"}
TENANT_OF = {"priya": "brindlewood", "ada": "brindlewood", "marcus": "brindlewood",
             "alex": "hollowmere"}


def _invite(user: str, ttl_hours: float = 1.0) -> str:
    with admin_conn(config.CONTROL_DB) as c:
        return tokens.mint_invite(c, TENANT_OF[user], user, ttl_hours, "test")


def _backend(user: str) -> None:
    with admin_conn(config.CONTROL_DB) as c:
        c.execute(
            "INSERT INTO user_backends (tenant_id, user_id, backend_id, url, hermes_release,"
            " ingress_secret_hash) VALUES (%s, %s, %s, %s, 'v0.22.6', %s)"
            " ON CONFLICT (tenant_id, user_id) DO NOTHING",
            (TENANT_OF[user], user, f"hermes-test-{user}", f"http://127.0.0.1:9{len(user)}31",
             tokens.hash_token(INGRESS[user])),
        )


def _sign_in(client, user: str, label: str = "test laptop") -> dict:
    r = client.post("/launcher/auth/invite",
                    json={"invite": _invite(user), "label": label, "platform": "windows"})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _introspect(client, user_ingress: str, token: str) -> dict:
    r = client.post("/launcher/introspect", json={"token": token},
                    headers=_bearer(INGRESS[user_ingress]))
    assert r.status_code == 200, r.text
    return r.json()


# ── invites ─────────────────────────────────────────────────────────────────


def test_invite_is_single_use(client):
    code = _invite("priya")
    body = {"invite": code, "label": "laptop", "platform": "windows"}
    assert client.post("/launcher/auth/invite", json=body).status_code == 200
    assert client.post("/launcher/auth/invite", json=body).status_code == 401


def test_expired_and_unknown_invites_are_refused(client):
    expired = _invite("priya", ttl_hours=0)
    for code in (expired, "cb_inv_not-a-real-code", ""):
        r = client.post("/launcher/auth/invite",
                        json={"invite": code, "label": "laptop", "platform": "windows"})
        assert r.status_code == 401


def test_invite_for_unknown_identity_cannot_be_minted(client):
    with admin_conn(config.CONTROL_DB) as c, pytest.raises(ValueError):
        tokens.mint_invite(c, "brindlewood", "nobody", 1, "test")


def test_api_role_cannot_mint_invites(client):
    """Only the operator mints invites: the API role has no INSERT on launcher_invites."""
    with control_conn() as c, pytest.raises(Exception):
        tokens.mint_invite(c, "brindlewood", "priya", 1, "test")


# ── library ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("user,brains,admin", [
    ("priya", {"company-wide", "operations"}, False),
    ("ada", {"company-wide", "leadership", "operations"}, True),
    ("marcus", {"company-wide", "finance"}, False),
    ("alex", {"company-wide", "finance", "treasury"}, False),
])
def test_me_lists_memberships_by_name_only(client, user, brains, admin):
    s = _sign_in(client, user)
    r = client.get("/launcher/me", headers=_bearer(s["access_token"]))
    assert r.status_code == 200
    me = r.json()
    assert me["user"]["id"] == user and me["user"]["is_admin"] is admin
    assert me["tenant"]["id"] == TENANT_OF[user]
    assert {b["id"] for b in me["brains"]} == brains
    assert all(set(b) == {"id", "name"} for b in me["brains"])   # names only, no content


def test_no_or_garbage_bearer_is_401(client):
    assert client.get("/launcher/me").status_code == 401
    assert client.get("/launcher/me", headers=_bearer("cb_la_garbage")).status_code == 401


def test_browser_session_cookie_does_not_open_the_launcher_api(client, as_user):
    h = {k: v for k, v in as_user("priya").items()}
    assert client.get("/launcher/me", headers=h).status_code == 401


# ── token kinds never cross ─────────────────────────────────────────────────


def test_token_kinds_do_not_cross(client):
    s = _sign_in(client, "priya")
    _backend("priya")
    desktop = client.post("/launcher/launch", headers=_bearer(s["access_token"])).json()["desktop_token"]
    # desktop and refresh tokens are not launcher access tokens
    for t in (desktop, s["refresh_token"]):
        assert client.get("/launcher/me", headers=_bearer(t)).status_code == 401
    # a launcher access token is not a /v1/ service token
    r = client.post("/v1/search", json={"query": "refund window"}, headers=_bearer(s["access_token"]))
    assert r.status_code == 401
    # a service token is not a launcher access token, nor a desktop token
    with control_conn() as c:
        svc = service_tokens.mint(c, "brindlewood", "priya", label="test")
    assert client.get("/launcher/me", headers=_bearer(svc)).status_code == 401
    assert _introspect(client, "priya", svc) == {"active": False}


# ── refresh rotation + reuse detection ──────────────────────────────────────


def test_refresh_rotates_and_reuse_revokes_the_device(client):
    s = _sign_in(client, "priya")
    r1 = client.post("/launcher/auth/refresh", json={"refresh_token": s["refresh_token"]})
    assert r1.status_code == 200
    s2 = r1.json()
    assert s2["refresh_token"] != s["refresh_token"] and s2["device_id"] == s["device_id"]
    assert client.get("/launcher/me", headers=_bearer(s2["access_token"])).status_code == 200
    # the OLD refresh token presented again = it was copied -> whole device revoked
    assert client.post("/launcher/auth/refresh",
                       json={"refresh_token": s["refresh_token"]}).status_code == 401
    assert client.get("/launcher/me", headers=_bearer(s2["access_token"])).status_code == 401
    assert client.post("/launcher/auth/refresh",
                       json={"refresh_token": s2["refresh_token"]}).status_code == 401


# ── launch + introspect ─────────────────────────────────────────────────────


def test_launch_without_a_backend_is_409(client):
    s = _sign_in(client, "marcus")   # never given a user_backends row in this suite
    assert client.post("/launcher/launch", headers=_bearer(s["access_token"])).status_code == 409


def test_launch_returns_backend_token_and_compat(client):
    _backend("priya")
    s = _sign_in(client, "priya")
    r = client.post("/launcher/launch", headers=_bearer(s["access_token"]))
    assert r.status_code == 200
    out = r.json()
    assert out["backend"]["id"] == "hermes-test-priya"
    assert out["desktop_token"].startswith("cb_dt_")
    assert out["hermes"]["backend_release"] == "v0.22.6"
    states = {x["release"]: x["state"] for x in out["hermes"]["releases"]}
    assert states["v0.22.6"] == "supported"
    assert _introspect(client, "priya", out["desktop_token"])["active"] is True


def test_relaunch_revokes_the_previous_desktop_token(client):
    _backend("priya")
    s = _sign_in(client, "priya")
    first = client.post("/launcher/launch", headers=_bearer(s["access_token"])).json()["desktop_token"]
    second = client.post("/launcher/launch", headers=_bearer(s["access_token"])).json()["desktop_token"]
    assert _introspect(client, "priya", first) == {"active": False}
    assert _introspect(client, "priya", second)["active"] is True


def test_desktop_token_only_works_at_its_own_backends_ingress(client):
    _backend("priya")
    _backend("ada")
    s = _sign_in(client, "ada")
    ada_token = client.post("/launcher/launch", headers=_bearer(s["access_token"])).json()["desktop_token"]
    assert _introspect(client, "ada", ada_token)["active"] is True
    assert _introspect(client, "priya", ada_token) == {"active": False}


def test_unknown_ingress_secret_is_401(client):
    r = client.post("/launcher/introspect", json={"token": "x"}, headers=_bearer("cb_ing_wrong"))
    assert r.status_code == 401
    assert client.post("/launcher/introspect", json={"token": "x"}).status_code == 401


def test_cross_tenant_ingress_cannot_validate(client):
    _backend("alex")
    _backend("priya")
    s = _sign_in(client, "priya")
    t = client.post("/launcher/launch", headers=_bearer(s["access_token"])).json()["desktop_token"]
    assert _introspect(client, "alex", t) == {"active": False}


# ── revocation paths ────────────────────────────────────────────────────────


def test_revoke_this_device_kills_access_refresh_and_desktop(client):
    _backend("priya")
    s = _sign_in(client, "priya")
    desktop = client.post("/launcher/launch", headers=_bearer(s["access_token"])).json()["desktop_token"]
    assert client.post("/launcher/auth/revoke", headers=_bearer(s["access_token"])).status_code == 200
    assert client.get("/launcher/me", headers=_bearer(s["access_token"])).status_code == 401
    assert client.post("/launcher/auth/refresh",
                       json={"refresh_token": s["refresh_token"]}).status_code == 401
    assert _introspect(client, "priya", desktop) == {"active": False}


def test_user_can_revoke_own_other_device_but_not_someone_elses(client):
    a = _sign_in(client, "priya", "laptop A")
    b = _sign_in(client, "priya", "laptop B")
    other = _sign_in(client, "ada", "ada laptop")
    listed = client.get("/launcher/devices", headers=_bearer(a["access_token"])).json()
    ids = {d["id"] for d in listed}
    assert a["device_id"] in ids and b["device_id"] in ids and other["device_id"] not in ids
    assert [d["current"] for d in listed if d["id"] == a["device_id"]] == [True]
    h = _bearer(a["access_token"])
    assert client.post(f"/launcher/devices/{other['device_id']}/revoke", headers=h).status_code == 404
    assert client.post("/launcher/devices/not-a-uuid/revoke", headers=h).status_code == 404
    assert client.post(f"/launcher/devices/{b['device_id']}/revoke", headers=h).status_code == 200
    assert client.get("/launcher/me", headers=_bearer(b["access_token"])).status_code == 401
    assert client.get("/launcher/me", headers=_bearer(other["access_token"])).status_code == 200


def test_deactivated_user_loses_launcher_and_desktop_tokens(client):
    _backend("priya")
    s = _sign_in(client, "priya")
    desktop = client.post("/launcher/launch", headers=_bearer(s["access_token"])).json()["desktop_token"]
    with admin_tenant_conn("brindlewood", vector=False) as t:
        t.execute("UPDATE users SET active = false WHERE id = 'priya'")
    try:
        assert client.get("/launcher/me", headers=_bearer(s["access_token"])).status_code == 401
        assert _introspect(client, "priya", desktop) == {"active": False}
    finally:
        with admin_tenant_conn("brindlewood", vector=False) as t:
            t.execute("UPDATE users SET active = true WHERE id = 'priya'")


def test_removed_identity_loses_every_launcher_credential(client):
    _backend("priya")
    s = _sign_in(client, "priya")
    desktop = client.post("/launcher/launch", headers=_bearer(s["access_token"])).json()["desktop_token"]
    with admin_conn(config.CONTROL_DB) as c:
        row = c.execute("SELECT * FROM identities WHERE email = 'priya@brindlewood.example'").fetchone()
        c.execute("DELETE FROM sessions WHERE email = 'priya@brindlewood.example'")
        c.execute("DELETE FROM identities WHERE email = 'priya@brindlewood.example'")
    try:
        assert client.get("/launcher/me", headers=_bearer(s["access_token"])).status_code == 401
        assert client.post("/launcher/auth/refresh",
                           json={"refresh_token": s["refresh_token"]}).status_code == 401
        assert _introspect(client, "priya", desktop) == {"active": False}
    finally:
        with admin_conn(config.CONTROL_DB) as c:
            c.execute(
                "INSERT INTO identities (email, tenant_id, user_id, google_sub, ms_subject)"
                " VALUES (%s, %s, %s, %s, %s)",
                (row["email"], row["tenant_id"], row["user_id"], row["google_sub"], row["ms_subject"]),
            )
        # Deleting the identity cascaded to priya's browser session; drop conftest's cached
        # cookie so later test files sign her in afresh instead of reusing a dead session.
        _sessions.pop("priya", None)


# ── BYOK link (admin only) ──────────────────────────────────────────────────


def _provider(key_owner: str) -> None:
    with admin_conn(config.CONTROL_DB) as c:
        c.execute(
            "INSERT INTO tenant_providers (tenant_id, provider, base_url, key_owner, daily_cap_usd)"
            " VALUES ('brindlewood', 'anthropic', 'https://api.anthropic.com/v1', %s, 5)"
            " ON CONFLICT (tenant_id, provider) DO UPDATE SET key_owner = EXCLUDED.key_owner",
            (key_owner,),
        )


def test_byok_link_is_admin_only(client, monkeypatch):
    monkeypatch.setattr(config, "KEY_INTAKE_BASE_URL", "https://keys.example")
    _provider("theirs")
    s = _sign_in(client, "priya")
    r = client.post("/launcher/byok/link", json={}, headers=_bearer(s["access_token"]))
    assert r.status_code == 403


def test_byok_link_for_admin_is_a_single_use_intake_url(client, monkeypatch):
    monkeypatch.setattr(config, "KEY_INTAKE_BASE_URL", "https://keys.example")
    _provider("theirs")
    s = _sign_in(client, "ada")
    r = client.post("/launcher/byok/link", json={}, headers=_bearer(s["access_token"]))
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith("https://keys.example/key/cb_keylink_")
    link_hash = tokens.hash_token(url.rsplit("/", 1)[1])
    with admin_conn(config.CONTROL_DB) as c:
        row = c.execute("SELECT tenant_id, provider, used_at, created_by FROM key_entry_links"
                        " WHERE token_hash = %s", (link_hash,)).fetchone()
    assert row == {"tenant_id": "brindlewood", "provider": "anthropic", "used_at": None,
                   "created_by": "launcher:ada"}


def test_byok_link_refused_when_we_hold_the_key(client, monkeypatch):
    monkeypatch.setattr(config, "KEY_INTAKE_BASE_URL", "https://keys.example")
    _provider("ours")
    try:
        s = _sign_in(client, "ada")
        r = client.post("/launcher/byok/link", json={}, headers=_bearer(s["access_token"]))
        assert r.status_code == 409
    finally:
        _provider("theirs")


def test_byok_link_unconfigured_intake_is_503(client, monkeypatch):
    monkeypatch.setattr(config, "KEY_INTAKE_BASE_URL", "")
    s = _sign_in(client, "ada")
    assert client.post("/launcher/byok/link", json={},
                       headers=_bearer(s["access_token"])).status_code == 503
