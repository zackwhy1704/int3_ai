"""
Real Postgres integration tests for /v1/ endpoints.

These tests require the Docker compose stack with a real Postgres database.
Run with:
  docker compose --profile dev up -d --build
  docker compose run --rm tools pytest -v -m integration

All tests are marked @pytest.mark.integration.

Security invariants tested (real DB, not mocks):
  test_v1_search_cross_tenant_excluded     — tenant A cannot see tenant B search results
  test_v1_claims_cross_tenant_404          — GET /v1/claims/{id} from tenant B returns 404 for tenant A
  test_v1_sources_cross_tenant_excluded    — sources list excludes tenant B docs
  test_v1_brain_ask_cross_tenant           — brain_ask results contain no tenant B canary text
  test_v1_validate_cross_tenant            — retrieval from tenant B is rejected for tenant A
  test_v1_search_cross_scope_real          — user with scope [ops] does not see finance-scoped doc

integration tests: not run — no Docker; will be verified in CI.

FAIL-WITHOUT-FIX comments:
  Each test has a FAIL-WITHOUT-FIX block describing what would happen if the fix
  were removed. These are documentation comments, not CI run results.
  fail-without-fix evidence requires the Docker suite — CI URL will show it.

A5 commit correction:
  A5's commit message said '(all 72 pilot pass)' — this was incorrect; the integration
  tier was not run at that point. The Docker suite is required for that claim; it will
  be verified via CI in S1/S5.
"""

from __future__ import annotations

import pytest

# Hollowmere canary strings (same as test_tenancy.py B_MARKERS)
_B_MARKERS = [
    "HOLLOWMERE-ONLY-7731",
    "HOLLOWMERE-FINANCE-2290",
    "HOLLOWMERE-TREASURY-4410",
]


# ---------------------------------------------------------------------------
# Cross-tenant tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_v1_search_cross_tenant_excluded(client, as_user):
    """Tenant A (brindlewood) search results never contain tenant B (hollowmere) text.

    This test verifies ACTUAL SQL isolation: the WHERE c.scope_id = ANY(%(scopes)s)
    clause resolves scope IDs server-side, scoped to the tenant DB. Hollowmere has
    scopes named 'company-wide' and 'finance' — same names as Brindlewood's — and
    priya is in Brindlewood's 'company-wide'. If the scope filter were broken, hollowmere
    canary text would appear in search results.

    FAIL-WITHOUT-FIX (S2/cross-tenant SQL):
    If scope_id was not filtered at the tenant DB level (or if tenant DB routing
    was broken), hollowmere canary text would appear in results. The test fails
    with AssertionError showing the canary in blob.
    fail-without-fix evidence requires the Docker suite — CI URL will show it.
    """
    # Search with a query that Hollowmere's seed data would answer if the filter broke.
    queries = [
        "treasury cash reserve target",
        "biodegradable pallet wrap roadmap",
        "refund window 60 days Hollowmere",
    ]
    for query in queries:
        resp = client.post(
            "/v1/search",
            json={"query": query},
            headers=as_user("priya"),
        )
        assert resp.status_code == 200, (
            f"Expected 200 for priya, got {resp.status_code}"
        )
        blob = " ".join(
            r.get("content", "") + " " + r.get("sourceTitle", "")
            for r in resp.json()["results"]
        )
        for marker in _B_MARKERS:
            assert marker not in blob, (
                f"Hollowmere canary '{marker}' appeared in tenant A search results. "
                "Cross-tenant SQL isolation is broken."
            )


@pytest.mark.integration
def test_v1_claims_cross_tenant_404(client, as_user):
    """GET /v1/claims/{id} where the claim exists in tenant B returns 404 for tenant A.

    The claim lookup uses cl.scope_id = ANY(%(scopes)s) — the principal's scopes
    from the tenant DB. A claim in hollowmere's DB is simply absent from brindlewood's
    DB, so the query returns no row and the endpoint returns 404.

    FAIL-WITHOUT-FIX: if the scope filter in _CLAIM_SQL were removed, the endpoint
    would query brindlewood's DB with no filter and still find nothing (different DB),
    but the test ensures the 404 is returned regardless.
    fail-without-fix evidence requires the Docker suite — CI URL will show it.
    """
    # Get a claim ID that exists in hollowmere's DB by querying as alex first.
    alex_resp = client.post(
        "/v1/search",
        json={"query": "treasury reserve target"},
        headers=as_user("alex"),
    )
    assert alex_resp.status_code == 200
    alex_results = alex_resp.json()["results"]
    if not alex_results:
        pytest.skip("No search results for alex; seed may not include treasury content")

    hollowmere_claim_id = alex_results[0]["claimId"]

    # Now try to fetch it as priya (brindlewood) — should get 404.
    priya_resp = client.get(
        f"/v1/claims/{hollowmere_claim_id}",
        headers=as_user("priya"),
    )
    assert priya_resp.status_code == 404, (
        f"Expected 404 for tenant A accessing tenant B claim {hollowmere_claim_id}, "
        f"got {priya_resp.status_code}. Cross-tenant claim isolation is broken."
    )


@pytest.mark.integration
def test_v1_sources_cross_tenant_excluded(client, as_user):
    """GET /v1/sources for tenant A does not include tenant B documents.

    Hollowmere has documents with IDs like 'treasury-cash', 'research-roadmap'.
    Brindlewood should never see these in its sources list.

    FAIL-WITHOUT-FIX: if the WHERE d.scope_id = ANY(%(scopes)s) were tenant-unaware,
    the response would include hollowmere documents. The test fails with AssertionError.
    fail-without-fix evidence requires the Docker suite — CI URL will show it.
    """
    resp = client.get("/v1/sources", headers=as_user("priya"))
    assert resp.status_code == 200
    sources = resp.json()
    source_ids = [s["id"] for s in sources]
    source_titles = " ".join(s.get("title", "") for s in sources)

    # Hollowmere-specific document IDs should not appear.
    hollowmere_doc_ids = ["treasury-cash", "research-roadmap"]
    for doc_id in hollowmere_doc_ids:
        assert doc_id not in source_ids, (
            f"Hollowmere document '{doc_id}' appeared in brindlewood sources list. "
            "Cross-tenant sources isolation is broken."
        )

    # Hollowmere canary text should not appear in titles.
    for marker in _B_MARKERS:
        assert marker not in source_titles, (
            f"Hollowmere canary '{marker}' appeared in source titles for tenant A."
        )


@pytest.mark.integration
@pytest.mark.llm
def test_v1_brain_ask_cross_tenant(client, as_user):
    """brain_ask by tenant A principal returns no results that mention tenant B content.

    The scopes passed to answer.ask() are resolved from the brindlewood DB;
    hollowmere content is in a separate DB and cannot be reached.

    FAIL-WITHOUT-FIX: if the DB routing were broken and both tenants shared a DB,
    hollowmere canary text could appear in answers. The test fails with AssertionError.
    fail-without-fix evidence requires the Docker suite — CI URL will show it.
    """
    resp = client.post(
        "/v1/brain_ask",
        json={"question": "What is the treasury cash reserve target?"},
        headers=as_user("priya"),
    )
    assert resp.status_code == 200
    body = resp.json()

    response_text = (
        str(body.get("answer", ""))
        + str(body.get("facts", ""))
        + str(body.get("citations", ""))
    ).lower()

    for marker in _B_MARKERS:
        assert marker.lower() not in response_text, (
            f"Hollowmere canary '{marker}' appeared in tenant A brain_ask response. "
            "Cross-tenant brain_ask isolation is broken."
        )


@pytest.mark.integration
def test_v1_validate_cross_tenant(client, as_user):
    """A retrieval_id from tenant B (alex) is invalid for tenant A (priya).

    The retrievals table uses session_id + tenant_id as the key. Alex's retrieval
    is bound to tenant 'hollowmere'; when priya tries to validate against it, the
    WHERE tenant_id = %(tenant_id)s clause rejects it.

    FAIL-WITHOUT-FIX (S4 CSRF + retrieval binding):
    If the validate endpoint did not check tenant_id, alex's retrieval_id could be
    used by priya to validate arbitrary claim IDs. The test fails because the retrieval
    is found (wrong tenant) and claim IDs are returned as valid.
    fail-without-fix evidence requires the Docker suite — CI URL will show it.
    """
    # First, alex does a search and gets a retrieval_id.
    alex_search = client.post(
        "/v1/search",
        json={"query": "treasury reserve"},
        headers=as_user("alex"),
    )
    assert alex_search.status_code == 200
    alex_retrieval_id = alex_search.json()["retrieval_id"]
    alex_claim_ids = [r["claimId"] for r in alex_search.json()["results"]]

    if not alex_claim_ids:
        pytest.skip("No results for alex; seed may not have matching content")

    # Now priya tries to validate alex's retrieval_id — must be all-invalid.
    priya_validate = client.post(
        "/v1/validate",
        json={"retrieval_id": alex_retrieval_id, "claim_ids": alex_claim_ids[:2]},
        headers=as_user("priya"),
    )
    assert priya_validate.status_code == 200
    data = priya_validate.json()
    assert data["valid"] == [], (
        f"Expected no valid claims for priya using alex's retrieval_id, "
        f"got valid={data['valid']}. Cross-tenant retrieval validation is broken."
    )
    assert set(data["invalid"]) == set(alex_claim_ids[:2]), (
        f"Expected all invalid for cross-tenant retrieval, got invalid={data['invalid']}."
    )


# ---------------------------------------------------------------------------
# Cross-scope tests (real DB)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_v1_retrieval_chunks_all_in_scope(client, as_user):
    """Every chunk_id in a retrieval record belongs to the principal's scopes.

    A finance-related query may match finance-scoped chunks in the embedding
    space. The WHERE c.scope_id = ANY(%(scopes)s) clause must exclude them
    before reranking, so they never enter the retrieval record.

    This test reads the control DB retrieval row and verifies each chunk_id
    has a scope_id in priya's scope list.

    FAIL-WITHOUT-FIX: neutralising the /v1/search WHERE clause lets
    finance-scoped chunks enter reranking and the retrieval record, which
    flips refused→not-refused for a finance-only query (side channel).
    """
    from app.db import control_conn, tenant_conn

    resp = client.post(
        "/v1/search",
        json={"query": "payment terms finance annual contract"},
        headers=as_user("priya"),
    )
    assert resp.status_code == 200
    rid = resp.json()["retrieval_id"]

    with control_conn() as conn:
        row = conn.execute(
            "SELECT chunk_ids FROM retrievals WHERE id = %s", (rid,)
        ).fetchone()
    assert row is not None, f"retrieval {rid} not found in control DB"
    chunk_ids = row["chunk_ids"]

    if not chunk_ids:
        return

    priya_scopes_resp = client.get("/v1/sources", headers=as_user("priya"))
    priya_scope_ids = {s["scope"] for s in priya_scopes_resp.json()}

    with tenant_conn("brindlewood") as tconn:
        rows = tconn.execute(
            "SELECT id, scope_id FROM chunks WHERE id = ANY(%s)", (chunk_ids,)
        ).fetchall()

    for r in rows:
        assert r["scope_id"] in priya_scope_ids, (
            f"Chunk {r['id']} has scope_id={r['scope_id']} which is not in "
            f"priya's scopes {priya_scope_ids}. Out-of-scope chunks entered "
            "reranking/retrieval — the WHERE clause filter is broken."
        )


@pytest.mark.integration
def test_v1_search_cross_scope_real(client, as_user):
    """User with scope [company-wide, operations] does not see finance-scoped content.

    Priya is in [company-wide, operations], NOT in finance. A document in the finance
    scope with matching text must NOT appear in her search results.

    This test verifies ACTUAL SQL isolation: WHERE c.scope_id = ANY(%(scopes)s) with
    priya's actual scope list. Marcus is in finance; priya is not.

    FAIL-WITHOUT-FIX (S4 scope_id fix):
    Before S4 added cl.scope_id AS scope_id to the SELECT, the scope field in
    chunk_hits_list used row.get('scope_id', '') which returned empty string
    (column not selected). The scope field in results was wrong (empty), but the
    SQL WHERE clause was still correct. The test verifies the SQL WHERE clause
    correctly excludes finance content for priya.
    fail-without-fix evidence requires the Docker suite — CI URL will show it.
    """
    # Finance-specific query — marcus can answer this, priya cannot.
    resp = client.post(
        "/v1/search",
        json={"query": "payment terms finance annual contract"},
        headers=as_user("priya"),
    )
    assert resp.status_code == 200

    # Get the same query for marcus (finance scope).
    marcus_resp = client.post(
        "/v1/search",
        json={"query": "payment terms finance annual contract"},
        headers=as_user("marcus"),
    )
    assert marcus_resp.status_code == 200

    # Any claim IDs marcus sees from the finance scope should NOT appear for priya.
    marcus_results = marcus_resp.json()["results"]
    priya_results = resp.json()["results"]

    # Filter marcus results to finance-only claims (sourceId containing 'finance').
    finance_claim_ids = {
        r["claimId"]
        for r in marcus_results
        if "finance" in r.get("sourceId", "").lower()
    }

    priya_claim_ids = {r["claimId"] for r in priya_results}

    if not finance_claim_ids:
        pytest.skip("No finance-scoped claims in marcus search results; seed may vary")

    overlap = finance_claim_ids & priya_claim_ids
    assert not overlap, (
        f"Finance-scoped claims {overlap} appeared in priya's search results. "
        "Cross-scope SQL isolation is broken (priya is not in finance scope)."
    )
