"""Cross-tenant tests. Every request here carries a VALID session for a real signed-in
user of some tenant, and reaches for another tenant's data.

Tenant A = brindlewood, B = hollowmere (colliding document ids and scope names),
C = brindlewood_labs (same email domain as A)."""

import pytest

from app.tenants.admin import admin_conn, admin_tenant_conn

B_MARKERS = [
    "HOLLOWMERE-ONLY-7731",
    "HOLLOWMERE-FINANCE-2290",
    "HOLLOWMERE-TREASURY-4410",
]
C_MARKERS = ["LABS-ONLY-5582", "LABS-RESEARCH-9034"]


def text_of(resp) -> str:
    return resp.text


# --- the headline case: a valid tenant-A session reaching for B's and C's data --------


@pytest.mark.parametrize("doc_id", ["treasury-cash", "research-roadmap"])
def test_a_session_cannot_fetch_documents_that_exist_only_in_b_or_c(
    client, as_user, doc_id
):
    r = client.get(f"/api/documents/{doc_id}", headers=as_user("ada"))
    assert r.status_code == 404


@pytest.mark.parametrize("user", ["priya", "marcus", "ada"])
def test_colliding_document_ids_resolve_inside_a_only(client, as_user, user):
    r = client.get("/api/documents/refund-policy-v3", headers=as_user(user))
    assert r.status_code == 200
    assert "Brindlewood Supply Co." in r.text
    for m in B_MARKERS + C_MARKERS:
        assert m not in r.text


def test_colliding_finance_document_is_a_s_own(client, as_user):
    r = client.get(
        "/api/documents/finance-annual-contract-terms", headers=as_user("marcus")
    )
    assert r.status_code == 200 and "45 days" in r.text
    assert "HOLLOWMERE-FINANCE-2290" not in r.text


@pytest.mark.parametrize("brain", ["treasury", "research"])
def test_a_session_cannot_use_scope_names_that_exist_only_in_b_or_c(
    client, as_user, brain
):
    h = as_user("ada")
    assert (
        client.get(
            "/api/search", params={"q": "reserve", "brain_id": brain}, headers=h
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/ask",
            json={"question": "What is the reserve target?", "brain_id": brain},
            headers=h,
        ).status_code
        == 404
    )


def test_same_scope_name_in_b_does_not_widen_a(client, as_user):
    # B has a "finance" scope and priya's tenant has one too; she is in neither.
    r = client.get(
        "/api/search",
        params={"q": "refund", "brain_id": "finance"},
        headers=as_user("priya"),
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    "q",
    [
        "treasury cash reserve target",
        "biodegradable pallet wrap roadmap",
        "refund window 60 days Hollowmere",
    ],
)
def test_a_search_never_returns_b_or_c_text(client, as_user, q):
    for user in ("priya", "marcus", "ada"):
        r = client.get("/api/search", params={"q": q}, headers=as_user(user))
        assert r.status_code == 200
        for m in B_MARKERS + C_MARKERS:
            assert m not in r.text, (user, q, m)


@pytest.mark.parametrize(
    "hint",
    [
        {"params": {"tenant": "hollowmere"}},
        {"params": {"tenant_id": "hollowmere"}},
        {"headers": {"X-Tenant": "hollowmere", "X-Tenant-Id": "hollowmere"}},
        {"headers": {"X-User-Id": "alex"}},
    ],
)
def test_tenant_hints_in_the_request_are_ignored(client, as_user, hint):
    headers = {**as_user("priya"), **hint.get("headers", {})}
    r = client.get(
        "/api/documents/refund-policy-v3", params=hint.get("params"), headers=headers
    )
    assert (
        r.status_code == 200
        and "HOLLOWMERE" not in r.text
        and "Brindlewood Supply" in r.text
    )
    r = client.get("/api/session", params=hint.get("params"), headers=headers)
    assert r.json()["tenant"]["id"] == "brindlewood"


# --- the same checks from B and C ---------------------------------------------------------


def test_b_session_sees_only_b(client, as_user):
    h = as_user("alex")
    r = client.get("/api/documents/refund-policy-v3", headers=h)
    assert r.status_code == 200 and "HOLLOWMERE-ONLY-7731" in r.text
    assert (
        client.get("/api/documents/refund-policy-v2", headers=h).status_code == 404
    )  # A-only id
    assert (
        client.get(
            "/api/search", params={"q": "x", "brain_id": "operations"}, headers=h
        ).status_code
        == 404
    )


def test_c_session_with_a_s_email_domain_sees_only_c(client, as_user):
    h = as_user("sam")
    assert (
        client.get("/api/session", headers=h).json()["tenant"]["id"]
        == "brindlewood_labs"
    )
    r = client.get("/api/documents/refund-policy-v3", headers=h)
    assert (
        r.status_code == 200
        and "LABS-ONLY-5582" in r.text
        and "Brindlewood Supply" not in r.text
    )
    for doc in (
        "refund-policy-v2",
        "leadership-acquisition-memo",
        "finance-annual-contract-terms",
    ):
        assert client.get(f"/api/documents/{doc}", headers=h).status_code == 404
    r = client.get("/api/search", params={"q": "enterprise refund window"}, headers=h)
    assert all("Brindlewood Supply" not in hit["text"] for hit in r.json()["results"])


# --- sessions that should no longer work ---------------------------------------------------


def test_tampered_cookie_is_401(client, as_user):
    name, value = as_user("priya")["Cookie"].split("=", 1)
    flipped = value[:-1] + ("A" if value[-1] != "A" else "B")
    assert (
        client.get("/api/session", headers={"Cookie": f"{name}={flipped}"}).status_code
        == 401
    )


@pytest.fixture
def temp_user(client):
    """A throwaway user in tenant A, so the tests below can deactivate and delete it."""
    with admin_tenant_conn("brindlewood") as t, admin_conn("control") as c:
        t.execute(
            "INSERT INTO users (id, name, title, email) VALUES ('temp', 'Temp', 'Temp',"
            " 'temp@brindlewood.example') ON CONFLICT (id) DO UPDATE SET active = true"
        )
        t.execute(
            "INSERT INTO memberships VALUES ('temp', 'company-wide') ON CONFLICT DO NOTHING"
        )
        c.execute("DELETE FROM identities WHERE email = 'temp@brindlewood.example'")
        c.execute(
            "INSERT INTO identities (email, tenant_id, user_id) VALUES"
            " ('temp@brindlewood.example', 'brindlewood', 'temp')"
        )
    from app.devtools import mock_login
    from app import config

    s = mock_login.login(
        client,
        "mock-google",
        "google-temp",
        mock_login.google_claims("temp@brindlewood.example", "brindlewood.example"),
    )
    yield {
        "Cookie": s["cookie"],
        "X-CSRF-Token": s["csrf"],
        "Origin": config.APP_ORIGIN,
    }
    with admin_tenant_conn("brindlewood") as t, admin_conn("control") as c:
        c.execute("DELETE FROM identities WHERE email = 'temp@brindlewood.example'")
        t.execute("DELETE FROM memberships WHERE user_id = 'temp'")
        t.execute("DELETE FROM users WHERE id = 'temp'")


def test_deactivated_user_is_401_on_next_request(client, temp_user):
    assert client.get("/api/session", headers=temp_user).status_code == 200
    with admin_tenant_conn("brindlewood") as t:
        t.execute("UPDATE users SET active = false WHERE id = 'temp'")
    assert client.get("/api/session", headers=temp_user).status_code == 401


def test_removed_identity_is_401_on_next_request(client, temp_user):
    assert client.get("/api/session", headers=temp_user).status_code == 200
    with admin_conn("control") as c:
        c.execute("DELETE FROM identities WHERE email = 'temp@brindlewood.example'")
    assert client.get("/api/session", headers=temp_user).status_code == 401


def test_removed_membership_applies_on_next_request(client, temp_user):
    r = client.get("/api/documents/refund-policy-v3", headers=temp_user)
    assert r.status_code == 200
    with admin_tenant_conn("brindlewood") as t:
        t.execute("DELETE FROM memberships WHERE user_id = 'temp'")
    assert (
        client.get("/api/documents/refund-policy-v3", headers=temp_user).status_code
        == 404
    )


def test_logout_ends_the_session(client, temp_user):
    assert client.post("/api/auth/logout", headers=temp_user).status_code == 200
    assert client.get("/api/session", headers=temp_user).status_code == 401
