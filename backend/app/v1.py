"""
/v1/ API — used by the desktop MCP server (RemoteBrainProvider).

Auth (Gate 3 development mode):
  Set BRAIN_DEV_USER=<user_id> to accept any Bearer token and treat all
  requests as that user. Gate 4 replaces this with RS256 OIDC verification.
  Without BRAIN_DEV_USER, every /v1/ request returns 401.

Endpoints:
  POST /v1/search           — semantic search returning claim-anchored results
  GET  /v1/claims/{id}      — single claim with supersede metadata
  GET  /v1/sources          — list of document sources accessible to the user
  POST /v1/validate-claims  — batch citation verification (Gate 0 item 2)

Citation format (frozen after Gate 3): [claim:N]
  MCP tools embed this marker in their text output. The LLM repeats the marker
  in its answer. Before the React UI renders any agent response, it extracts
  all [claim:N] patterns and calls POST /v1/validate-claims. IDs that fail
  (not found or out of scope) are shown as unverifiable — never silently dropped.
  This enforces the citation invariant server-side regardless of agent behaviour.
"""
import os
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .db import connect
from .embed import embed_query
from .scopes import resolve, user_scopes

log = logging.getLogger("v1")

router = APIRouter(prefix="/v1")

# ---------------------------------------------------------------------------
# Auth (Gate 3 dev stub — replaced by OIDC in Gate 4)
# ---------------------------------------------------------------------------

_DEV_USER = os.getenv("BRAIN_DEV_USER", "")


def _bearer_user(authorization: str | None = None) -> str:
    """
    Resolve the caller to a user_id.

    Gate 3: BRAIN_DEV_USER must be set; any non-empty Bearer token is accepted
    and maps to that user.  Missing or blank env var → 401.
    Gate 4: replace this function with real RS256 OIDC token verification.
    """
    if not _DEV_USER:
        raise HTTPException(
            401,
            "BRAIN_DEV_USER is not set — /v1/ endpoints are unavailable until "
            "Gate 4 OIDC is implemented or BRAIN_DEV_USER is configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing Bearer token")
    return _DEV_USER


def authed_user(authorization: str | None = None) -> str:
    return _bearer_user(authorization)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    scopes: list[str] = []


class SearchResult(BaseModel):
    claimId: str
    content: str
    sourceTitle: str
    sourceId: str
    score: float
    asOf: str
    supersededBy: str | None = None


class Claim(BaseModel):
    id: str
    subject: str
    attribute: str
    value: str
    condition: str | None
    asOf: str
    supersededBy: str | None
    sourceId: str
    scope: str


class Source(BaseModel):
    id: str
    title: str
    kind: str
    scope: str
    updatedAt: str


# ---------------------------------------------------------------------------
# POST /v1/search
# ---------------------------------------------------------------------------

_SEARCH_SQL = """
    SELECT
        cl.id           AS claim_id,
        cl.value        AS value,
        cl.subject      AS subject,
        cl.attribute    AS attribute,
        cl.condition    AS condition,
        cl.valid_from   AS as_of,
        cl.superseded_by AS superseded_by,
        d.id            AS doc_id,
        d.title         AS doc_title,
        1 - (c.embedding <=> %(vec)s) AS score
    FROM chunks c
    JOIN documents d ON d.id = c.document_id
    JOIN claims cl   ON cl.source_chunk_id = c.id
    WHERE c.scope_id = ANY(%(scopes)s)
      AND cl.scope_id = ANY(%(scopes)s)
    ORDER BY c.embedding <=> %(vec)s
    LIMIT %(k)s
"""


@router.post("/search", response_model=list[SearchResult])
def search(body: SearchRequest, user: str = Depends(authed_user)) -> list[SearchResult]:
    if not body.query.strip():
        raise HTTPException(422, "query must not be blank")

    scopes = resolve(user, None) if not body.scopes else [
        s for s in body.scopes if s in resolve(user, None)
    ]
    if not scopes:
        # Requested scopes don't overlap with user's memberships — return empty.
        return []

    vec = embed_query(body.query)
    with connect() as conn:
        rows = conn.execute(_SEARCH_SQL, {"vec": vec, "scopes": scopes, "k": 20}).fetchall()

    # Deduplicate: if the same claim appears via multiple similar chunks, keep best score.
    seen: dict[int, SearchResult] = {}
    for row in rows:
        cid = row["claim_id"]
        if cid in seen:
            continue
        content = row["value"]
        if row["condition"]:
            content = f"[when: {row['condition']}] {content}"
        seen[cid] = SearchResult(
            claimId=str(cid),
            content=content,
            sourceTitle=row["doc_title"],
            sourceId=row["doc_id"],
            score=round(float(row["score"]), 4),
            asOf=str(row["as_of"]),
            supersededBy=str(row["superseded_by"]) if row["superseded_by"] is not None else None,
        )

    return list(seen.values())


# ---------------------------------------------------------------------------
# GET /v1/claims/{claim_id}
# ---------------------------------------------------------------------------

_CLAIM_SQL = """
    SELECT
        cl.id, cl.subject, cl.attribute, cl.value, cl.condition,
        cl.valid_from, cl.superseded_by, cl.scope_id,
        d.id AS doc_id
    FROM claims cl
    JOIN chunks c ON c.id = cl.source_chunk_id
    JOIN documents d ON d.id = c.document_id
    WHERE cl.id = %(id)s
      AND cl.scope_id = ANY(%(scopes)s)
"""


@router.get("/claims/{claim_id}", response_model=Claim)
def get_claim(claim_id: str, user: str = Depends(authed_user)) -> Claim:
    try:
        cid = int(claim_id)
    except ValueError:
        raise HTTPException(422, "claim_id must be an integer string")

    scopes = user_scopes(user)
    with connect() as conn:
        row = conn.execute(_CLAIM_SQL, {"id": cid, "scopes": scopes}).fetchone()

    if row is None:
        raise HTTPException(404, "claim not found")

    return Claim(
        id=str(row["id"]),
        subject=row["subject"],
        attribute=row["attribute"],
        value=row["value"],
        condition=row["condition"],
        asOf=str(row["valid_from"]),
        supersededBy=str(row["superseded_by"]) if row["superseded_by"] is not None else None,
        sourceId=row["doc_id"],
        scope=row["scope_id"],
    )


# ---------------------------------------------------------------------------
# GET /v1/sources
# ---------------------------------------------------------------------------

_SOURCES_SQL = """
    SELECT
        d.id,
        d.title,
        d.authority    AS kind,
        d.scope_id     AS scope,
        d.effective_date AS updated_at
    FROM documents d
    WHERE d.scope_id = ANY(%(scopes)s)
    ORDER BY d.effective_date DESC, d.title
"""


@router.get("/sources", response_model=list[Source])
def list_sources(
    scope: list[str] = Query(default=[]),
    user: str = Depends(authed_user),
) -> list[Source]:
    all_scopes = user_scopes(user)
    if scope:
        scopes = [s for s in scope if s in all_scopes]
    else:
        scopes = all_scopes

    if not scopes:
        return []

    with connect() as conn:
        rows = conn.execute(_SOURCES_SQL, {"scopes": scopes}).fetchall()

    return [
        Source(
            id=row["id"],
            title=row["title"],
            kind=row["kind"],
            scope=row["scope"],
            updatedAt=str(row["updated_at"]),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# POST /v1/validate-claims  (Gate 0 item 2 — citation invariant enforcement)
# ---------------------------------------------------------------------------

_VALIDATE_SQL = """
    SELECT
        cl.id, cl.subject, cl.attribute, cl.value, cl.condition,
        cl.valid_from, cl.superseded_by, cl.scope_id,
        d.id AS doc_id
    FROM claims cl
    JOIN chunks c ON c.id = cl.source_chunk_id
    JOIN documents d ON d.id = c.document_id
    WHERE cl.id = ANY(%(ids)s)
      AND cl.scope_id = ANY(%(scopes)s)
"""

_MAX_VALIDATE_BATCH = 50  # prevent abuse; a single response won't have more


class ValidateRequest(BaseModel):
    claim_ids: list[str]


class ValidateResponse(BaseModel):
    # claim_id → Claim for every ID that exists and is in scope
    valid: dict[str, Claim]
    # IDs that are missing, out of scope, or not integers — must be flagged
    invalid: list[str]


@router.post("/validate-claims", response_model=ValidateResponse)
def validate_claims(
    body: ValidateRequest,
    user: str = Depends(authed_user),
) -> ValidateResponse:
    """
    Batch-verify claim IDs extracted from an agent response.

    Called by the React UI (Gate 5) before rendering any agent answer that
    contains [claim:N] markers. Any ID not returned in `valid` must be shown
    as an unverifiable citation rather than accepted silently.

    The scope check is identical to GET /v1/claims/{id}: a claim that exists
    in the DB but belongs to a scope the user cannot access returns as invalid.
    """
    if not body.claim_ids:
        return ValidateResponse(valid={}, invalid=[])

    if len(body.claim_ids) > _MAX_VALIDATE_BATCH:
        raise HTTPException(422, f"too many claim IDs (max {_MAX_VALIDATE_BATCH})")

    # Separate parseable integers from non-integer IDs (non-integers are always invalid).
    int_ids: list[int] = []
    non_int: list[str] = []
    for raw in body.claim_ids:
        try:
            int_ids.append(int(raw))
        except ValueError:
            non_int.append(raw)

    scopes = user_scopes(user)
    found: dict[str, Claim] = {}

    if int_ids:
        with connect() as conn:
            rows = conn.execute(_VALIDATE_SQL, {"ids": int_ids, "scopes": scopes}).fetchall()
        for row in rows:
            claim = Claim(
                id=str(row["id"]),
                subject=row["subject"],
                attribute=row["attribute"],
                value=row["value"],
                condition=row["condition"],
                asOf=str(row["valid_from"]),
                supersededBy=(
                    str(row["superseded_by"]) if row["superseded_by"] is not None else None
                ),
                sourceId=row["doc_id"],
                scope=row["scope_id"],
            )
            found[claim.id] = claim

    found_strs = set(found.keys())
    requested_strs = {str(i) for i in int_ids}
    invalid = non_int + sorted(requested_strs - found_strs)

    return ValidateResponse(valid=found, invalid=invalid)
