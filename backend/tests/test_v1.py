"""
Unit tests for the /v1/ API router (Gate 3).

Marking: pytest.mark.unit — mocks DB, embed, and scopes; runs without
Docker / Postgres / fastembed.

Run: pytest -m unit backend/tests/test_v1.py
"""
import pytest
from unittest.mock import patch, MagicMock

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Patch heavy optional deps before importing the app, so the module can be
# imported without a live Postgres or the fastembed model weights.
# ---------------------------------------------------------------------------
import sys
from types import ModuleType

for _mod in ["psycopg", "pgvector", "pgvector.psycopg",
             "fastembed", "fastembed.rerank", "fastembed.rerank.cross_encoder"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = ModuleType(_mod)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                    # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEV_USER = "user_001"
_SCOPES = ["scope_001"]
VEC = [0.0] * 384

_FAKE_SEARCH_ROW = {
    "claim_id": 1,
    "value": "30 days",
    "subject": "refund",
    "attribute": "window",
    "condition": None,
    "as_of": "2026-01-01",
    "superseded_by": None,
    "doc_id": "doc_001",
    "doc_title": "Refund Policy v2",
    "score": 0.92,
}

_FAKE_CLAIM_ROW = {
    "id": 1,
    "subject": "refund",
    "attribute": "window",
    "value": "30 days",
    "condition": None,
    "valid_from": "2026-01-01",
    "superseded_by": None,
    "scope_id": "scope_001",
    "doc_id": "doc_001",
}

_FAKE_SOURCE_ROW = {
    "id": "doc_001",
    "title": "Refund Policy v2",
    "kind": "document",
    "scope": "scope_001",
    "updated_at": "2026-01-01",
}


def _conn_cm(rows):
    """Context-manager mock: .execute(...).fetchall() → rows."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    cursor.fetchone.return_value = rows[0] if rows else None
    conn = MagicMock()
    conn.execute.return_value = cursor
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=conn)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


TOKEN = "Bearer dev-token"

# Patches that must be active for every /v1/ request.
_BASE_PATCHES = [
    patch("app.v1._DEV_USER", DEV_USER),
    patch("app.scopes.user_scopes", return_value=_SCOPES),
    patch("app.scopes.resolve", return_value=_SCOPES),
    patch("app.embed.embed_query", return_value=VEC),
]


def _apply_base():
    """Return a list of started patches. Caller must stop them."""
    started = [p.start() for p in _BASE_PATCHES]
    return started


def _stop_base(started):
    for p, _ in zip(_BASE_PATCHES, started):
        p.stop()


@pytest.fixture(autouse=True)
def base_patches():
    started = _apply_base()
    yield
    _stop_base(started)


client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /v1/search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_returns_results(self):
        with patch("app.v1.connect", return_value=_conn_cm([_FAKE_SEARCH_ROW])):
            resp = client.post("/v1/search",
                               json={"query": "refund"},
                               headers={"authorization": TOKEN})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["claimId"] == "1"
        assert data[0]["sourceTitle"] == "Refund Policy v2"
        assert abs(data[0]["score"] - 0.92) < 0.001
        assert data[0]["supersededBy"] is None

    def test_condition_prepended_to_content(self):
        row = {**_FAKE_SEARCH_ROW, "condition": "enterprise customers"}
        with patch("app.v1.connect", return_value=_conn_cm([row])):
            resp = client.post("/v1/search",
                               json={"query": "refund"},
                               headers={"authorization": TOKEN})
        assert resp.status_code == 200
        assert "enterprise customers" in resp.json()[0]["content"]

    def test_deduplicates_same_claim_across_chunks(self):
        rows = [_FAKE_SEARCH_ROW, _FAKE_SEARCH_ROW]  # same claim_id twice
        with patch("app.v1.connect", return_value=_conn_cm(rows)):
            resp = client.post("/v1/search",
                               json={"query": "refund"},
                               headers={"authorization": TOKEN})
        assert len(resp.json()) == 1

    def test_superseded_result_carries_superseded_by(self):
        row = {**_FAKE_SEARCH_ROW, "superseded_by": 2}
        with patch("app.v1.connect", return_value=_conn_cm([row])):
            resp = client.post("/v1/search",
                               json={"query": "refund"},
                               headers={"authorization": TOKEN})
        assert resp.json()[0]["supersededBy"] == "2"

    def test_blank_query_returns_422(self):
        with patch("app.v1.connect", return_value=_conn_cm([])):
            resp = client.post("/v1/search",
                               json={"query": "   "},
                               headers={"authorization": TOKEN})
        assert resp.status_code == 422

    def test_no_token_returns_401(self):
        with patch("app.v1.connect", return_value=_conn_cm([])):
            resp = client.post("/v1/search", json={"query": "refund"})
        assert resp.status_code == 401

    def test_no_dev_user_returns_401(self):
        with (
            patch("app.v1._DEV_USER", ""),
            patch("app.v1.connect", return_value=_conn_cm([])),
        ):
            resp = client.post("/v1/search",
                               json={"query": "refund"},
                               headers={"authorization": TOKEN})
        assert resp.status_code == 401

    def test_requested_scopes_outside_membership_returns_empty(self):
        with (
            patch("app.scopes.resolve", return_value=[]),
            patch("app.scopes.user_scopes", return_value=[]),
            patch("app.v1.connect", return_value=_conn_cm([])),
        ):
            resp = client.post("/v1/search",
                               json={"query": "refund", "scopes": ["secret_scope"]},
                               headers={"authorization": TOKEN})
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /v1/claims/{claim_id}
# ---------------------------------------------------------------------------

class TestGetClaim:
    def test_returns_claim(self):
        with patch("app.v1.connect", return_value=_conn_cm([_FAKE_CLAIM_ROW])):
            resp = client.get("/v1/claims/1",
                              headers={"authorization": TOKEN})
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "1"
        assert body["subject"] == "refund"
        assert body["scope"] == "scope_001"
        assert body["supersededBy"] is None

    def test_superseded_claim_carries_superseded_by(self):
        row = {**_FAKE_CLAIM_ROW, "superseded_by": 2}
        with patch("app.v1.connect", return_value=_conn_cm([row])):
            resp = client.get("/v1/claims/1",
                              headers={"authorization": TOKEN})
        assert resp.json()["supersededBy"] == "2"

    def test_condition_included_when_present(self):
        row = {**_FAKE_CLAIM_ROW, "condition": "enterprise customers"}
        with patch("app.v1.connect", return_value=_conn_cm([row])):
            resp = client.get("/v1/claims/1",
                              headers={"authorization": TOKEN})
        assert resp.json()["condition"] == "enterprise customers"

    def test_non_integer_id_returns_422(self):
        resp = client.get("/v1/claims/abc",
                          headers={"authorization": TOKEN})
        assert resp.status_code == 422

    def test_out_of_scope_claim_returns_404(self):
        with patch("app.v1.connect", return_value=_conn_cm([])):
            resp = client.get("/v1/claims/999",
                              headers={"authorization": TOKEN})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /v1/sources
# ---------------------------------------------------------------------------

class TestListSources:
    def test_returns_sources(self):
        with patch("app.v1.connect", return_value=_conn_cm([_FAKE_SOURCE_ROW])):
            resp = client.get("/v1/sources",
                              headers={"authorization": TOKEN})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "Refund Policy v2"
        assert data[0]["kind"] == "document"
        assert data[0]["scope"] == "scope_001"

    def test_scope_filter_restricts_to_authorised_scopes(self):
        # User has scope_001 only; requesting scope_002 → empty.
        with (
            patch("app.scopes.user_scopes", return_value=["scope_001"]),
            patch("app.v1.connect", return_value=_conn_cm([])),
        ):
            resp = client.get("/v1/sources?scope=scope_002",
                              headers={"authorization": TOKEN})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_no_sources_returns_empty_list(self):
        with patch("app.v1.connect", return_value=_conn_cm([])):
            resp = client.get("/v1/sources",
                              headers={"authorization": TOKEN})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_no_token_returns_401(self):
        with patch("app.v1.connect", return_value=_conn_cm([])):
            resp = client.get("/v1/sources")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /v1/validate-claims  (Gate 0 item 2 — citation invariant)
# ---------------------------------------------------------------------------

class TestValidateClaims:
    def test_valid_in_scope_claim_returned_in_valid(self):
        with patch("app.v1.connect", return_value=_conn_cm([_FAKE_CLAIM_ROW])):
            resp = client.post("/v1/validate-claims",
                               json={"claim_ids": ["1"]},
                               headers={"authorization": TOKEN})
        assert resp.status_code == 200
        body = resp.json()
        assert "1" in body["valid"]
        assert body["valid"]["1"]["subject"] == "refund"
        assert body["invalid"] == []

    def test_out_of_scope_id_returned_in_invalid(self):
        # connect returns no rows → claim not found or out of scope
        with patch("app.v1.connect", return_value=_conn_cm([])):
            resp = client.post("/v1/validate-claims",
                               json={"claim_ids": ["999"]},
                               headers={"authorization": TOKEN})
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] == {}
        assert "999" in body["invalid"]

    def test_non_integer_id_immediately_invalid(self):
        with patch("app.v1.connect", return_value=_conn_cm([])):
            resp = client.post("/v1/validate-claims",
                               json={"claim_ids": ["hallucinated-id", "1"]},
                               headers={"authorization": TOKEN})
        assert resp.status_code == 200
        body = resp.json()
        assert "hallucinated-id" in body["invalid"]

    def test_mixed_valid_and_invalid(self):
        with patch("app.v1.connect", return_value=_conn_cm([_FAKE_CLAIM_ROW])):
            # claim 1 → found; claim 99 → not in rows → invalid
            resp = client.post("/v1/validate-claims",
                               json={"claim_ids": ["1", "99"]},
                               headers={"authorization": TOKEN})
        body = resp.json()
        assert "1" in body["valid"]
        assert "99" in body["invalid"]

    def test_empty_list_returns_empty(self):
        resp = client.post("/v1/validate-claims",
                           json={"claim_ids": []},
                           headers={"authorization": TOKEN})
        assert resp.status_code == 200
        assert resp.json() == {"valid": {}, "invalid": []}

    def test_oversized_batch_returns_422(self):
        many_ids = [str(i) for i in range(51)]
        resp = client.post("/v1/validate-claims",
                           json={"claim_ids": many_ids},
                           headers={"authorization": TOKEN})
        assert resp.status_code == 422

    def test_no_token_returns_401(self):
        resp = client.post("/v1/validate-claims", json={"claim_ids": ["1"]})
        assert resp.status_code == 401
