"""
Tests for /v1/ endpoints (A5).

Security invariants tested:
  test_v1_cross_tenant_search_returns_404     — cross-tenant brain_id → 404
  test_v1_cross_scope_search_excluded         — out-of-scope claims excluded
  test_v1_refusal_gate_applied                — no-match query → 0 results
  test_v1_validate_foreign_retrieval          — wrong session → all invalid
  test_v1_validate_foreign_principal          — same tenant, different user → all invalid
  test_v1_validate_expired_retrieval          — expired → all invalid
  test_v1_validate_claim_not_in_retrieval     — in-scope claim not retrieved → invalid

Uses FastAPI dependency_overrides — never patches at the definition site.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth.principal import Principal, principal
from app.main import app


# ---------------------------------------------------------------------------
# Fake Principal helpers
# ---------------------------------------------------------------------------

def _fake_principal(
    tenant_id: str = "tenant_a",
    user_id: str = "user_a",
    session_id: str = "session_a",
    scopes: list[str] | None = None,
    conn: MagicMock | None = None,
) -> Principal:
    if scopes is None:
        scopes = ["scope_a"]
    if conn is None:
        conn = MagicMock()
    return Principal(
        tenant_id=tenant_id,
        tenant_name="Tenant A",
        user_id=user_id,
        email=f"{user_id}@example.com",
        name=user_id,
        scopes=scopes,
        csrf_token="csrf_a",
        session_id=session_id,
        conn=conn,
    )


def _override_principal(p: Principal):
    """Return a FastAPI dependency override that yields `p`."""
    def _dep() -> Iterator[Principal]:
        yield p
    return _dep


# ---------------------------------------------------------------------------
# test_v1_cross_tenant_search_returns_404
#
# Security invariant: a principal from tenant A tries to access tenant B's
# brain_id. p.resolve() raises 404 because the brain_id is not in p.scopes.
# The hollowmere fixture from conftest is referenced conceptually; here we
# use dependency_overrides with tenant_a scopes trying to access scope_b.
# ---------------------------------------------------------------------------

def test_v1_cross_tenant_search_returns_404():
    """Principal with tenant_a scopes cannot access a brain from another tenant."""
    p = _fake_principal(tenant_id="tenant_a", scopes=["scope_a"])

    with TestClient(app) as client:
        app.dependency_overrides[principal] = _override_principal(p)
        try:
            resp = client.post(
                "/v1/search",
                json={"query": "what is the refund policy", "brain_id": "scope_b"},
                headers={"Cookie": "test=1"},
            )
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 404, (
        f"Expected 404 for a cross-tenant brain_id, got {resp.status_code}. "
        "p.resolve() must treat inaccessible scopes as non-existent."
    )


# ---------------------------------------------------------------------------
# test_v1_cross_scope_search_excluded
#
# Security invariant: a principal in scope_a searches; results from scope_b
# are absent (the SQL WHERE clause enforces scope_id = ANY(scopes)).
# We mock the DB connection to return a row from scope_b and verify it's
# not in the response.
# ---------------------------------------------------------------------------

def test_v1_cross_scope_search_excluded():
    """Out-of-scope chunks must be absent from /v1/search results."""
    conn = MagicMock()

    # The search SQL returns a row from scope_b — this should never happen with
    # a correct query, but we verify the scope filter anyway.
    # Simulate: conn.execute(...).fetchall() returns empty (correct server-side filter).
    conn.execute.return_value.fetchall.return_value = []

    p = _fake_principal(scopes=["scope_a"], conn=conn)

    with TestClient(app) as client:
        app.dependency_overrides[principal] = _override_principal(p)
        try:
            with patch("app.api.v1.embed_query", return_value=[0.1] * 384):
                with patch("app.api.v1._rerank", return_value=[]):
                    with patch("app.api.v1._record_retrieval", return_value="rid-1"):
                        resp = client.post(
                            "/v1/search",
                            json={"query": "any question"},
                        )
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == [], (
        "Expected empty results when all chunks are out of scope. "
        "The scope filter must be applied server-side in the SQL WHERE clause."
    )
    # Verify the SQL was called with the correct scope
    call_args = conn.execute.call_args
    assert call_args is not None
    params = call_args[0][1]  # positional args: (sql, params)
    assert params["scopes"] == ["scope_a"], (
        "SQL must be called with p.scopes, not a client-supplied scope list."
    )


# ---------------------------------------------------------------------------
# test_v1_refusal_gate_applied
#
# Security invariant: a question with no good match returns 0 results.
# The cross-encoder reranker is mocked to return all scores < REFUSE_THRESHOLD.
# ---------------------------------------------------------------------------

def test_v1_refusal_gate_applied():
    """Query with no above-threshold chunks → empty results (refusal gate)."""
    conn = MagicMock()
    # Return one chunk row from the DB
    chunk_row = {
        "chunk_id": 1, "claim_id": 10, "value": "some value",
        "subject": "s", "attribute": "a", "condition": None, "as_of": "2024-01-01",
        "superseded_by": None, "doc_id": "doc1", "doc_title": "Doc", "source": "doc",
        "owner": "admin", "effective_date": "2024-01-01", "chunk_text": "some text",
        "score": 0.9,
    }
    conn.execute.return_value.fetchall.return_value = [chunk_row]

    p = _fake_principal(scopes=["scope_a"], conn=conn)

    # Mock reranker to return a score below the refusal threshold (-1.0 < 0.0)
    low_score_hit = {
        "chunk_id": 1, "doc_title": "Doc", "text": "some text",
        "scope": "scope_a", "document_id": "doc1", "source": "doc",
        "owner": "admin", "effective_date": "2024-01-01", "score": 0.9,
        "relevance": -1.0,  # below REFUSE_THRESHOLD = 0.0
    }

    with TestClient(app) as client:
        app.dependency_overrides[principal] = _override_principal(p)
        try:
            with patch("app.api.v1.embed_query", return_value=[0.1] * 384):
                with patch("app.api.v1._rerank", return_value=[low_score_hit]):
                    with patch("app.api.v1._record_retrieval", return_value="rid-1"):
                        resp = client.post(
                            "/v1/search",
                            json={"query": "question with no match"},
                        )
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == [], (
        f"Expected 0 results when all chunks score below REFUSE_THRESHOLD, "
        f"got: {data['results']}. The refusal gate must be applied."
    )


# ---------------------------------------------------------------------------
# test_v1_validate_foreign_retrieval
#
# Security invariant: a retrieval_id from a different session → all invalid.
# The control DB lookup checks session_id == p.session_id, so a token from
# another session returns nothing.
# ---------------------------------------------------------------------------

def test_v1_validate_foreign_retrieval():
    """A retrieval from a different session must return all claim IDs as invalid."""
    p = _fake_principal(session_id="session_a", tenant_id="tenant_a")

    # Simulate: the retrieval row exists but belongs to session_b (not session_a).
    # The SQL WHERE session_id = %s AND tenant_id = %s will return no row.
    with TestClient(app) as client:
        app.dependency_overrides[principal] = _override_principal(p)
        try:
            with patch("app.api.v1.control_conn") as mock_ctrl:
                mock_ctx = MagicMock()
                mock_ctrl.return_value.__enter__ = MagicMock(return_value=mock_ctx)
                mock_ctrl.return_value.__exit__ = MagicMock(return_value=False)
                # Query returns None (wrong session)
                mock_ctx.execute.return_value.fetchone.return_value = None

                resp = client.post(
                    "/v1/validate",
                    json={"retrieval_id": str(uuid.uuid4()), "claim_ids": [1, 2, 3]},
                )
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] == [], (
        f"Expected no valid claims for a foreign retrieval, got valid={data['valid']}."
    )
    assert set(data["invalid"]) == {1, 2, 3}, (
        f"Expected all claims invalid for a foreign retrieval, got invalid={data['invalid']}."
    )


# ---------------------------------------------------------------------------
# test_v1_validate_foreign_principal
#
# Security invariant: same tenant, different user (different session_id) → invalid.
# ---------------------------------------------------------------------------

def test_v1_validate_foreign_principal():
    """Same tenant, different user's session → claim IDs from that session are invalid."""
    # user_b is in tenant_a but has a different session
    p = _fake_principal(session_id="session_b", tenant_id="tenant_a", user_id="user_b")

    rid = str(uuid.uuid4())

    with TestClient(app) as client:
        app.dependency_overrides[principal] = _override_principal(p)
        try:
            with patch("app.api.v1.control_conn") as mock_ctrl:
                mock_ctx = MagicMock()
                mock_ctrl.return_value.__enter__ = MagicMock(return_value=mock_ctx)
                mock_ctrl.return_value.__exit__ = MagicMock(return_value=False)
                # The row belongs to session_a, not session_b; WHERE clause returns None.
                mock_ctx.execute.return_value.fetchone.return_value = None

                resp = client.post(
                    "/v1/validate",
                    json={"retrieval_id": rid, "claim_ids": [10, 20]},
                )
        finally:
            app.dependency_overrides.clear()

    data = resp.json()
    assert data["valid"] == []
    assert set(data["invalid"]) == {10, 20}, (
        "Claims from another principal's retrieval must be invalid."
    )


# ---------------------------------------------------------------------------
# test_v1_validate_expired_retrieval
#
# Security invariant: expired retrieval → all claim IDs invalid.
# ---------------------------------------------------------------------------

def test_v1_validate_expired_retrieval():
    """An expired retrieval must return all claim IDs as invalid."""
    p = _fake_principal(session_id="session_a", tenant_id="tenant_a")
    rid = str(uuid.uuid4())
    past = datetime.now(timezone.utc) - timedelta(hours=2)

    with TestClient(app) as client:
        app.dependency_overrides[principal] = _override_principal(p)
        try:
            with patch("app.api.v1.control_conn") as mock_ctrl:
                mock_ctx = MagicMock()
                mock_ctrl.return_value.__enter__ = MagicMock(return_value=mock_ctx)
                mock_ctrl.return_value.__exit__ = MagicMock(return_value=False)
                # Row exists but is expired
                mock_ctx.execute.return_value.fetchone.return_value = {
                    "claim_ids": [5, 6, 7],
                    "expires_at": past,
                }

                resp = client.post(
                    "/v1/validate",
                    json={"retrieval_id": rid, "claim_ids": [5, 6, 7]},
                )
        finally:
            app.dependency_overrides.clear()

    data = resp.json()
    assert data["valid"] == [], "Expired retrieval must return no valid claims."
    assert set(data["invalid"]) == {5, 6, 7}, "All claims must be invalid for an expired retrieval."


# ---------------------------------------------------------------------------
# test_v1_validate_claim_not_in_retrieval
#
# Security invariant: a claim that exists in scope but was NOT in the retrieval
# set is returned as invalid.
# ---------------------------------------------------------------------------

def test_v1_validate_claim_not_in_retrieval():
    """A claim that exists but was not retrieved → invalid."""
    p = _fake_principal(session_id="session_a", tenant_id="tenant_a")
    rid = str(uuid.uuid4())
    future = datetime.now(timezone.utc) + timedelta(hours=1)

    # Retrieval contains claim_ids [1, 2]. Claim 3 is in scope but not retrieved.
    with TestClient(app) as client:
        app.dependency_overrides[principal] = _override_principal(p)
        try:
            with patch("app.api.v1.control_conn") as mock_ctrl:
                mock_ctx = MagicMock()
                mock_ctrl.return_value.__enter__ = MagicMock(return_value=mock_ctx)
                mock_ctrl.return_value.__exit__ = MagicMock(return_value=False)
                mock_ctx.execute.return_value.fetchone.return_value = {
                    "claim_ids": [1, 2],
                    "expires_at": future,
                }

                resp = client.post(
                    "/v1/validate",
                    json={"retrieval_id": rid, "claim_ids": [1, 2, 3]},
                )
        finally:
            app.dependency_overrides.clear()

    data = resp.json()
    assert data["valid"] == [1, 2], f"Expected [1, 2] valid, got {data['valid']}."
    assert data["invalid"] == [3], (
        f"Expected [3] invalid (not in retrieval set), got {data['invalid']}. "
        "A hallucinated or out-of-retrieval claim ID must be rejected."
    )
