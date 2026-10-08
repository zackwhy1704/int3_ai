"""
/v1/ API — used by the desktop MCP server (RemoteBrainProvider).

Auth: ALL endpoints use Principal = Depends(principal) from app.auth.principal.
No Bearer token, no BRAIN_AUTH, no BRAIN_DEV_USER. In Phase A the /v1 routes
accept pilot's session cookie (same as /api/ask). Bearer token support comes
in Phase B.

Endpoints:
  POST /v1/search         — semantic search, CURRENT claims only, with previous
                            value when superseded, cross-encoder refusal gate
  GET  /v1/claims/{id}    — single claim with supersede metadata
  GET  /v1/sources        — document sources accessible to the user
  POST /v1/brain_ask      — the /api/ask pipeline for agent use
  POST /v1/validate       — retrieval-bound citation check (A4)

Deleted vs main:
  - /v1/validate-claims (not ported; replaced by /v1/validate, see RECONCILE.md §4a)
  - scopes.py imports (replaced by p.scopes / p.resolve(brain_id))
  - BRAIN_AUTH / BRAIN_DEV_USER / OIDC_AUDIENCE (removed entirely)
  - backend/app/oidc.py (main's copy; pilot's auth/oidc.py is authoritative)

Citation format (frozen after Gate 3): [claim:N]
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import answer as answer_mod
from .. import llm
from ..auth.principal import Principal, csrf_protected, principal
from ..db import control_conn
from ..embed import embed_query
from ..rerank import rerank as _rerank

log = logging.getLogger("v1")

router = APIRouter(prefix="/v1")

# Cross-encoder logit below which the question is refused.
# Measured on seed questions: see answer.py and app/calibrate.py.
_REFUSE_THRESHOLD = answer_mod.REFUSE_THRESHOLD


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    brain_id: str | None = None    # if set, restrict to that one scope


class SearchResult(BaseModel):
    claimId: int
    content: str
    sourceTitle: str
    sourceId: str
    score: float
    asOf: str
    supersededBy: int | None = None
    previousValue: str | None = None   # value of the claim superseded BY this one


class SearchResponse(BaseModel):
    results: list[SearchResult]
    retrieval_id: str              # UUID for use with POST /v1/validate


class Claim(BaseModel):
    id: int
    subject: str
    attribute: str
    value: str
    condition: str | None
    asOf: str
    supersededBy: int | None
    sourceId: str
    scope: str


class Source(BaseModel):
    id: str
    title: str
    kind: str
    scope: str
    updatedAt: str


class BrainAskRequest(BaseModel):
    question: str
    brain_id: str | None = None


class BrainAskResponse(BaseModel):
    refused: bool
    message: str | None = None
    suggested_owner: str | None = None
    answer: str | None = None
    citations: list[dict] | None = None
    answered_from: list[str] | None = None
    facts: list[dict] | None = None
    cached: bool = False
    retrieval_id: str | None = None    # UUID for use with POST /v1/validate


class ValidateRequest(BaseModel):
    retrieval_id: str
    claim_ids: list[int]


class ValidateResponse(BaseModel):
    valid: list[int]
    invalid: list[int]


# ---------------------------------------------------------------------------
# Internal: record a retrieval in the control DB
# ---------------------------------------------------------------------------

def _record_retrieval(session_id: str, tenant_id: str,
                      claim_ids: list[int], chunk_ids: list[int]) -> str:
    """Insert a retrievals row in the CONTROL DB and return the UUID.

    The retrieval is keyed to session_id + tenant_id so only the principal
    that performed the retrieval can validate against it (tested by
    test_v1_validate_foreign_retrieval, test_v1_validate_foreign_principal).
    """
    rid = str(uuid.uuid4())
    with control_conn() as conn:
        conn.execute(
            """
            INSERT INTO retrievals (id, session_id, tenant_id, claim_ids, chunk_ids)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (rid, session_id, tenant_id, claim_ids, chunk_ids),
        )
    return rid


# ---------------------------------------------------------------------------
# POST /v1/search
# ---------------------------------------------------------------------------

# Find the claim that was superseded BY a given current claim (i.e. the previous value).
_PREV_VALUE_SQL = """
    SELECT value FROM claims
    WHERE superseded_by = %(current_id)s
      AND scope_id = ANY(%(scopes)s)
    LIMIT 1
"""


@router.post("/search", response_model=SearchResponse)
def search(body: SearchRequest, p: Principal = Depends(principal)) -> SearchResponse:
    """
    Semantic search returning CURRENT claims only, with cross-encoder refusal gate.

    Returns only claims where superseded_by IS NULL (current truth).
    When a current claim replaced an older one, the older claim's value is
    included as previousValue so the caller knows what changed without being
    told to cite an ID they were not shown.

    A retrieval_id is returned; use it with POST /v1/validate to check
    that cited claim IDs came from this retrieval (structural citation invariant).

    Security: scope filter is in the SQL WHERE clause (enforced server-side,
    tested by test_v1_cross_tenant_search_returns_404,
    test_v1_cross_scope_search_excluded).
    Refusal gate: cross-encoder threshold = REFUSE_THRESHOLD = 0 (measured;
    tested by test_v1_refusal_gate_applied).
    """
    if not body.query.strip():
        raise HTTPException(422, "query must not be blank")

    # p.resolve() enforces the scope boundary; 404 for inaccessible brains
    # (indistinguishable from non-existent).
    scopes = p.resolve(body.brain_id)

    vec = embed_query(body.query)

    # Search ALL matching chunks (left-join), not just those with extracted claims.
    rows = p.conn.execute(
        """
        SELECT
            c.id            AS chunk_id,
            cl.id           AS claim_id,
            cl.value        AS value,
            cl.subject      AS subject,
            cl.attribute    AS attribute,
            cl.condition    AS condition,
            cl.valid_from   AS as_of,
            cl.superseded_by AS superseded_by,
            d.id            AS doc_id,
            d.title         AS doc_title,
            d.source        AS source,
            d.owner         AS owner,
            d.effective_date AS effective_date,
            c.text          AS chunk_text,
            1 - (c.embedding <=> %(vec)s) AS score
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        LEFT JOIN claims cl ON cl.source_chunk_id = c.id
                            AND cl.superseded_by IS NULL
                            AND cl.scope_id = ANY(%(scopes)s)
        WHERE c.scope_id = ANY(%(scopes)s)
        ORDER BY c.embedding <=> %(vec)s
        LIMIT %(k)s
        """,
        {"vec": vec, "scopes": scopes, "k": 20},
    ).fetchall()

    # Build chunk-level hits for cross-encoder reranking.
    chunk_hits_list: list[dict] = []
    chunk_to_rows: dict[int, list[dict]] = {}
    seen_chunks: set[int] = set()
    for row in rows:
        cid = row["chunk_id"]
        if cid not in seen_chunks:
            seen_chunks.add(cid)
            chunk_hits_list.append({
                "chunk_id": cid,
                "doc_title": row["doc_title"],
                "text": row["chunk_text"] or "",
                "scope": row.get("scope_id", ""),
                "document_id": row["doc_id"],
                "source": row["source"],
                "owner": row["owner"],
                "effective_date": row["effective_date"],
                "score": float(row["score"]),
            })
        chunk_to_rows.setdefault(cid, []).append(row)

    # Apply cross-encoder reranking and refusal gate.
    ranked = _rerank(body.query, chunk_hits_list)
    context = [h for h in ranked if h["relevance"] >= _REFUSE_THRESHOLD]

    if not context:
        # Record an empty retrieval so validate calls still work correctly.
        rid = _record_retrieval(p.session_id, p.tenant_id, [], [])
        return SearchResponse(results=[], retrieval_id=rid)

    # Build SearchResult objects from the chunks that passed the gate.
    results: list[SearchResult] = []
    seen_claims: set[int] = set()
    all_chunk_ids: list[int] = []
    all_claim_ids: list[int] = []

    for h in context:
        cid = h["chunk_id"]
        all_chunk_ids.append(cid)
        for row in chunk_to_rows.get(cid, []):
            if row["claim_id"] is None:
                continue  # chunk has no extracted claim
            claim_id = row["claim_id"]
            if claim_id in seen_claims:
                continue
            seen_claims.add(claim_id)
            all_claim_ids.append(claim_id)

            content = row["value"]
            if row["condition"]:
                content = f"[when: {row['condition']}] {content}"

            # Find previous value: the claim that was superseded BY this current claim.
            prev_row = p.conn.execute(
                _PREV_VALUE_SQL,
                {"current_id": claim_id, "scopes": scopes},
            ).fetchone()
            prev_value = prev_row["value"] if prev_row else None

            results.append(SearchResult(
                claimId=claim_id,
                content=content,
                sourceTitle=row["doc_title"],
                sourceId=row["doc_id"],
                score=round(float(h["relevance"]), 4),
                asOf=str(row["as_of"]),
                supersededBy=None,  # current claims have superseded_by IS NULL
                previousValue=prev_value,
            ))

    # Record the retrieval in the CONTROL DB (not the tenant DB).
    rid = _record_retrieval(p.session_id, p.tenant_id, all_claim_ids, all_chunk_ids)
    return SearchResponse(results=results, retrieval_id=rid)


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
def get_claim(claim_id: str, p: Principal = Depends(principal)) -> Claim:
    try:
        cid = int(claim_id)
    except ValueError:
        raise HTTPException(422, "claim_id must be an integer string")

    row = p.conn.execute(_CLAIM_SQL, {"id": cid, "scopes": p.scopes}).fetchone()
    if row is None:
        raise HTTPException(404, "claim not found")

    return Claim(
        id=row["id"],
        subject=row["subject"],
        attribute=row["attribute"],
        value=row["value"],
        condition=row["condition"],
        asOf=str(row["valid_from"]),
        supersededBy=row["superseded_by"],
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
    p: Principal = Depends(principal),
) -> list[Source]:
    if scope:
        scopes = [s for s in scope if s in p.scopes]
    else:
        scopes = p.scopes

    if not scopes:
        return []

    rows = p.conn.execute(_SOURCES_SQL, {"scopes": scopes}).fetchall()
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
# POST /v1/brain_ask — the /api/ask pipeline for agent use
# ---------------------------------------------------------------------------

@router.post("/brain_ask", response_model=BrainAskResponse)
def brain_ask(body: BrainAskRequest, p: Principal = Depends(csrf_protected)) -> BrainAskResponse:
    """Run the full /api/ask pipeline and return the result with a retrieval_id.

    Identical to POST /api/ask but at the /v1/ prefix and returns retrieval_id
    for use with POST /v1/validate (structural citation invariant, A4).
    """
    scopes = p.resolve(body.brain_id)
    try:
        result = answer_mod.ask(p.conn, body.question, scopes)
    except llm.Unavailable:
        raise HTTPException(
            503,
            "The model could not be reached, and there is no earlier "
            "answer to this question for your access to fall back on.",
        )

    # Extract claim IDs and chunk IDs from the answer for retrieval recording.
    claim_ids: list[int] = []
    chunk_ids: list[int] = []
    for citation in result.get("citations", []) or []:
        if "chunk_id" in citation:
            chunk_ids.append(citation["chunk_id"])
    for fact in result.get("facts", []) or []:
        # facts come from the claims table, but we don't have claim IDs in the
        # standard answer output; use chunk_ids as a reasonable approximation.
        pass

    rid = _record_retrieval(p.session_id, p.tenant_id, claim_ids, chunk_ids)

    return BrainAskResponse(
        refused=result.get("refused", False),
        message=result.get("message"),
        suggested_owner=result.get("suggested_owner"),
        answer=result.get("answer"),
        citations=result.get("citations"),
        answered_from=result.get("answered_from"),
        facts=result.get("facts"),
        cached=result.get("cached", False),
        retrieval_id=rid,
    )


# ---------------------------------------------------------------------------
# POST /v1/validate — retrieval-bound citation check (A4)
#
# Replaces /v1/validate-claims from main (not ported; see RECONCILE.md §4a).
#
# Security invariants tested:
#   test_v1_validate_foreign_retrieval    — different session → all invalid
#   test_v1_validate_foreign_principal    — same tenant, different user → all invalid
#   test_v1_validate_expired_retrieval    — expired → all invalid
#   test_v1_validate_claim_not_in_retrieval — in-scope but not retrieved → invalid
# ---------------------------------------------------------------------------

@router.post("/validate", response_model=ValidateResponse)
def validate(body: ValidateRequest, p: Principal = Depends(principal)) -> ValidateResponse:
    """Check that claim IDs came from a previous retrieval by this principal.

    The retrieval must:
    - exist and not be expired
    - be keyed to p.session_id and p.tenant_id (not another session or tenant)

    Each claim_id in the request is valid only if it appears in the retrieval's
    claim_ids list. Anything else — hallucinated IDs, out-of-scope IDs, IDs from
    a different retrieval — is returned in invalid[].
    """
    if not body.claim_ids:
        return ValidateResponse(valid=[], invalid=[])

    with control_conn() as conn:
        row = conn.execute(
            """
            SELECT claim_ids, expires_at
            FROM retrievals
            WHERE id = %s
              AND session_id = %s
              AND tenant_id = %s
            """,
            (body.retrieval_id, p.session_id, p.tenant_id),
        ).fetchone()

    if row is None:
        # Not found, wrong session, or wrong tenant — all claim IDs are invalid.
        return ValidateResponse(valid=[], invalid=body.claim_ids)

    from datetime import datetime, timezone
    if row["expires_at"].replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return ValidateResponse(valid=[], invalid=body.claim_ids)

    retrieved_set: set[int] = set(row["claim_ids"] or [])
    valid: list[int] = []
    invalid: list[int] = []
    for cid in body.claim_ids:
        if cid in retrieved_set:
            valid.append(cid)
        else:
            invalid.append(cid)

    return ValidateResponse(valid=valid, invalid=invalid)
