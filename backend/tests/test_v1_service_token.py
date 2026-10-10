"""Service-token auth for the /v1/ machine surface (Task B).

Real Postgres integration tests. Run with:
  docker compose --profile dev up -d
  docker compose run --rm tools pytest -v -m integration backend/tests/test_v1_service_token.py

Invariants:
  test_v1_service_token_no_cookie_succeeds   — a valid service token with NO
      session cookie reaches /v1/ and acts as the token's user.
  test_v1_service_token_tenant_isolation     — a token minted for tenant A
      (brindlewood) never returns tenant B (hollowmere) data; a tenant B token
      does (positive control), proving isolation is by token→tenant binding,
      not by the query happening to fail.
"""

from __future__ import annotations

import pytest

from app.auth import service_tokens
from app.db import control_conn

# Hollowmere (tenant B) canary strings — identical to test_v1_integration.py.
_B_MARKERS = [
    "HOLLOWMERE-ONLY-7731",
    "HOLLOWMERE-FINANCE-2290",
    "HOLLOWMERE-TREASURY-4410",
]

# Queries that hollowmere's own seed docs answer (refund-policy-v3,
# finance-annual-contract-terms, treasury-cash), so a hollowmere principal
# retrieves them and a brindlewood one must not.
_B_QUERIES = [
    "enterprise refund request window",
    "annual contract payment terms",
    "treasury cash reserve target",
]


def _mint(tenant_id: str, user_id: str) -> str:
    with control_conn() as control:
        return service_tokens.mint(control, tenant_id, user_id, label="test")


@pytest.mark.integration
def test_v1_service_token_no_cookie_succeeds(client):
    """A service token authenticates /v1/ with no cookie, no CSRF, no origin.

    FAIL-WITHOUT-FIX: before Task B the /v1/ routes used csrf_protected, which
    requires a session cookie + matching x-csrf-token + same-origin header. A
    bearer-only request would 401 (no cookie -> UNAUTHENTICATED). This asserts
    the machine path exists.
    """
    token = _mint("brindlewood", "priya")

    # Only the bearer header. No Cookie, no X-CSRF-Token, no Origin.
    resp = client.post(
        "/v1/search",
        json={"query": "refund window for enterprise customers"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    # Sanity: it actually ran as a real principal (real search results came
    # back), not a degraded/empty auth stub.
    results = resp.json().get("results")
    assert isinstance(results, list) and len(results) > 0, resp.text

    # And the negative: the SAME request with no auth at all is rejected.
    anon = client.post(
        "/v1/search",
        json={"query": "refund window for enterprise customers"},
    )
    assert anon.status_code == 401, anon.text


@pytest.mark.integration
def test_v1_service_token_tenant_isolation(client):
    """A tenant-A token cannot read tenant-B data; a tenant-B token can.

    The token hardwires the tenant, and the tenant DB connection is opened as
    that tenant's own Postgres role, so an A token physically cannot query B.
    Checked against /v1/sources, whose titles are a clean discriminator: every
    hollowmere document title starts with "Hollowmere"; no brindlewood one does.

    FAIL-WITHOUT-FIX: if the service principal resolved identity/scopes without
    binding the tenant DB to the token's tenant (e.g. trusting a client-supplied
    tenant or reusing a shared connection), the A token's /v1/sources would list
    hollowmere documents.
    """
    a_token = _mint("brindlewood", "priya")   # tenant A
    b_token = _mint("hollowmere", "alex")      # tenant B

    def titles(token: str) -> list[str]:
        resp = client.get("/v1/sources", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        return [s["title"] for s in resp.json()]

    a_titles = titles(a_token)
    b_titles = titles(b_token)

    # A token must not see any hollowmere document.
    assert not any("Hollowmere" in t for t in a_titles), (
        f"tenant A token listed a hollowmere document: {a_titles}"
    )
    # Positive control: the B token DOES reach hollowmere documents, so the
    # check above is real tenant binding, not an empty listing for everyone.
    assert any("Hollowmere" in t for t in b_titles), (
        f"tenant B token listed no hollowmere document — positive control failed: {b_titles}"
    )


# ---------------------------------------------------------------------------
# CSRF on the cookie path (Item 3).
# v1_principal drops CSRF only for the bearer (machine) path; a cookie-
# authenticated browser request to /v1/ must still present Origin + a matching
# x-csrf-token, exactly like the browser routes. Otherwise /v1/ would be a
# CSRF-exempt hole for any logged-in browser.
# ---------------------------------------------------------------------------

_ASK = {"question": "What's our refund window for enterprise customers?"}


@pytest.mark.integration
def test_v1_cookie_without_csrf_is_403(client, as_user):
    # cookie present, X-CSRF-Token stripped -> rejected
    h = {k: v for k, v in as_user("priya").items() if k != "X-CSRF-Token"}
    r = client.post("/v1/brain_ask", json=_ASK, headers=h)
    assert r.status_code == 403, r.text
    assert "CSRF" in r.json()["detail"]


@pytest.mark.integration
def test_v1_cookie_with_valid_csrf_is_200(client, as_user):
    r = client.post("/v1/brain_ask", json=_ASK, headers=as_user("priya"))
    assert r.status_code == 200, r.text


@pytest.mark.integration
def test_v1_bearer_without_csrf_is_200(client):
    # machine path: a service token, no cookie, no CSRF header -> allowed
    token = _mint("brindlewood", "priya")
    r = client.post("/v1/brain_ask", json=_ASK, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
