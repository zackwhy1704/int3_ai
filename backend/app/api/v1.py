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
  POST /v1/validate       — retrieval-bound citation check (see A4)

Deleted vs main:
  - /v1/validate-claims (not ported; replaced by /v1/validate, see RECONCILE.md §4a)
  - scopes.py imports (replaced by p.scopes / p.resolve(brain_id))
  - BRAIN_AUTH / BRAIN_DEV_USER / OIDC_AUDIENCE (removed entirely)
  - backend/app/oidc.py (main's copy; pilot's auth/oidc.py is authoritative)

Citation format (frozen after Gate 3): [claim:N]
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import answer as answer_mod
from .. import llm
from ..auth.principal import Principal, csrf_protected, principal
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
    previousValue: str | None = None   # value of the claim that superseded this one


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


# ---------------------------------------------------------------------------
# POST /v1/search
# ---------------------------------------------------------------------------

# CURRENT claims only (superseded_by IS NULL).
# Left-join so we search ALL matching chunks, not just those with extracted claims.
# The previous value (what was superseded BY the current claim) is fetched separately.
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
    LEFT JOIN claims cl ON cl.source_chunk_id = c.id
                        AND cl.superseded_by IS NULL
    WHERE c.scope_id = ANY(%(scopes)s)
      AND (cl.scope_id IS NULL OR cl.scope_id = ANY(%(scopes)s))
    ORDER BY c.embedding <=> %(vec)s
    LIMIT %(k)s
"""

# Find the claim that was superseded BY a given current claim (i.e. the previous value).
_PREV_VALUE_SQL = """
    SELECT value FROM claims
    WHERE superseded_by = %(current_id)s
      AND scope_id = ANY(%(scopes)s)
    LIMIT 1
"""


@router.post("/search", response_model=list[SearchResult])
def search(body: SearchRequest, p: Principal = Depends(principal)) -> list[SearchResult]:
    """
    Semantic search returning CURRENT claims only, with cross-encoder refusal gate.

    Returns only claims where superseded_by IS NULL (current truth).
    When a current claim replaced an older one, the older claim's value is
    included as previousValue so the caller knows what changed without being
    told to cite an ID they were not shown.

    Security: scope filter is in the SQL WHERE clause (enforced server-side).
    Refusal gate: same cross-encoder threshold as /api/ask (REFUSE_THRESHOLD=0).
    """
    if not body.query.strip():
        raise HTTPException(422, "query must not be blank")

    # p.resolve() enforces the scope boundary; 404 if brain_id is not in p.scopes.
    scopes = p.resolve(body.brain_id)

    vec = embed_query(body.query)
    rows = p.conn.execute(_SEARCH_SQL, {"vec": vec, "scopes": scopes, "k": 20}).fetchall()

    # Apply cross-encoder refusal gate (same as answer.py:answer_live).
    # Build chunk-level hits for the reranker.
    chunk_hits: list[dict] = []
    seen_chunks: set[int] = set()
    row_by_chunk: dict[int, dict] = {}
    for row in rows:
        if row["doc_id"] is None:
            continue  # chunk has no associated document (shouldn't happen)
        chunk_id_val = row.get("chunk_id")
        # rows from the LEFT JOIN don't have chunk_id directly; use doc_id + score as surrogate
        # Actually the SQL returns chunk c columns via score, we need chunk id.
        # We need chunk id — add it to the query.
        pass

    # Re-run with chunk id included.
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

    # Build chunk-level hits for reranking.
    chunk_hits_list: list[dict] = []
    chunk_to_rows: dict[int, list[dict]] = {}
    for row in rows:
        cid = row["chunk_id"]
        if cid not in chunk_to_rows:
            chunk_hits_list.append({
                "chunk_id": cid,
                "doc_title": row["doc_title"],
                "text": "",  # text not needed for reranking score here
                "scope": row.get("scope_id", ""),
                "document_id": row["doc_id"],
                "source": row["source"],
                "owner": row["owner"],
                "effective_date": row["effective_date"],
                "score": float(row["score"]),
            })
        chunk_to_rows.setdefault(cid, []).append(row)

    # Apply cross-encoder reranking and refusal gate.
    # Load text for chunks that pass the embedding threshold.
    if chunk_hits_list:
        chunk_ids = [h["chunk_id"] for h in chunk_hits_list]
        text_rows = p.conn.execute(
            "SELECT id, text FROM chunks WHERE id = ANY(%s)", (chunk_ids,)
        ).fetchall()
        text_by_id = {r["id"]: r["text"] for r in text_rows}
        for h in chunk_hits_list:
            h["text"] = text_by_id.get(h["chunk_id"], "")

    ranked = _rerank(body.query, chunk_hits_list)
    context = [h for h in ranked if h["relevance"] >= _REFUSE_THRESHOLD]

    if not context:
        return []

    # Build SearchResult objects from the chunks that passed the gate.
    results: list[SearchResult] = []
    seen_claims: set[int] = set()
    for h in context:
        cid = h["chunk_id"]
        for row in chunk_to_rows.get(cid, []):
            if row["claim_id"] is None:
                continue  # chunk has no claim
            claim_id = row["claim_id"]
            if claim_id in seen_claims:
                continue
            seen_claims.add(claim_id)

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

    return results


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

@router.post("/brain_ask")
def brain_ask(body: BrainAskRequest, p: Principal = Depends(csrf_protected)) -> dict:
    """Run the full /api/ask pipeline and return the result.

    Identical to POST /api/ask but at the /v1/ prefix and returns retrieval_id
    (populated in A4).
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
    return result
