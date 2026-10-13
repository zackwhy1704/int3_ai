"""Phase 2 capture: propose a fact in a workflow, owner approves, it becomes a
claim — via a WORKFLOW RECORD so the passage-first retrieval path (answer.py)
finds and cites it unchanged.

Two human gates (enforced at the MCP sidecar by elicitation, and here by auth):
  1. the proposing user confirms (sidecar elicitation) before propose() is called;
  2. the scope OWNER approves (owner-only approve()).

Provenance is server-derived: proposer_user_id and timestamps come from the
principal / server clock. session_label is model-supplied DISPLAY-ONLY text.
"""

import datetime
import logging

from fastapi import HTTPException

from . import config
from .embed import embed_passages

log = logging.getLogger("capture")


def _owner_of(p, scope: str) -> str | None:
    row = p.conn.execute("SELECT owner_user_id FROM scopes WHERE id = %s", (scope,)).fetchone()
    return row["owner_user_id"] if row else None


def _require_owner(p, scope: str) -> None:
    if _owner_of(p, scope) != p.user_id:
        raise HTTPException(403, "only the scope owner may review proposals")


def _active_claim_for_key(p, scope, subject, attribute, condition) -> dict | None:
    """The current (non-superseded) claim for this key, with its document authority."""
    return p.conn.execute(
        """
        SELECT cl.id, cl.value, d.authority, d.title AS doc_title
        FROM claims cl
        JOIN chunks c ON c.id = cl.source_chunk_id
        JOIN documents d ON d.id = c.document_id
        WHERE cl.scope_id = %s AND cl.subject = %s AND cl.attribute = %s
          AND cl.condition IS NOT DISTINCT FROM %s AND cl.superseded_by IS NULL
        """,
        (scope, subject, attribute, condition),
    ).fetchone()


def propose(p, *, scope, subject, attribute, value, quote, condition=None, session_label=None) -> dict:
    """Record a pending proposal. 403 if the caller is not a member of `scope`;
    429 if the caller is over the per-user hourly rate limit."""
    if scope not in p.scopes:
        raise HTTPException(403, f"not a member of scope {scope!r}")
    limit = config.PROPOSAL_RATE_PER_HOUR
    if limit > 0:
        recent = p.conn.execute(
            "SELECT count(*) AS n FROM proposals WHERE proposer_user_id = %s"
            " AND created_at > now() - interval '1 hour'",
            (p.user_id,),
        ).fetchone()["n"]
        if recent >= limit:
            raise HTTPException(429, f"proposal rate limit of {limit}/hour reached")
    row = p.conn.execute(
        "INSERT INTO proposals (scope_id, subject, attribute, condition, value, quote,"
        " proposer_user_id, session_label) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (scope, subject, attribute, condition, value, quote, p.user_id, session_label),
    ).fetchone()
    return {"proposal_id": str(row["id"]), "status": "pending"}


def list_proposals(p, *, scope) -> list[dict]:
    """Pending proposals in a scope the caller OWNS, each with any conflicting
    active claim for the same key so the owner can decide on sight."""
    _require_owner(p, scope)
    out = []
    for r in p.conn.execute(
        "SELECT id, subject, attribute, condition, value, quote, proposer_user_id,"
        " session_label, created_at FROM proposals"
        " WHERE scope_id = %s AND status = 'pending' ORDER BY created_at",
        (scope,),
    ).fetchall():
        conflict = _active_claim_for_key(p, scope, r["subject"], r["attribute"], r["condition"])
        out.append({
            "proposal_id": str(r["id"]),
            "subject": r["subject"], "attribute": r["attribute"], "condition": r["condition"],
            "value": r["value"], "quote": r["quote"],
            "proposer": r["proposer_user_id"], "session_label": r["session_label"],
            "created_at": r["created_at"].isoformat(),
            "conflict": None if not conflict else {
                "existing_value": conflict["value"], "authority": conflict["authority"],
                "doc_title": conflict["doc_title"],
            },
        })
    return out


def _load_pending_owned(p, proposal_id: str) -> dict:
    row = p.conn.execute(
        "SELECT * FROM proposals WHERE id = %s AND status = 'pending'", (proposal_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "no such pending proposal")
    _require_owner(p, row["scope_id"])        # 403 if caller does not own the proposal's scope
    return row


def approve(p, *, proposal_id: str, supersede_anyway: bool = False) -> dict:
    """Owner approves (service-principal path). Writes the workflow record and
    marks the proposal approved. Supersede against a document-authority claim is
    never automatic — it needs supersede_anyway (409 otherwise)."""
    pr = _load_pending_owned(p, proposal_id)
    return _approve_write(p.conn, pr, decided_by=p.user_id, supersede_anyway=supersede_anyway)


def _approve_write(conn, pr: dict, *, decided_by: str, supersede_anyway: bool) -> dict:
    """Shared write core for both the owner path and the operator CLI fallback.
    decided_by is recorded honestly (the owner's user id, or 'operator:<name>')."""
    proposal_id = str(pr["id"])
    scope, subject, attribute, condition = pr["scope_id"], pr["subject"], pr["attribute"], pr["condition"]
    existing = conn.execute(
        "SELECT cl.id, d.authority FROM claims cl JOIN chunks c ON c.id = cl.source_chunk_id"
        " JOIN documents d ON d.id = c.document_id WHERE cl.scope_id=%s AND cl.subject=%s"
        " AND cl.attribute=%s AND cl.condition IS NOT DISTINCT FROM %s AND cl.superseded_by IS NULL",
        (scope, subject, attribute, condition),
    ).fetchone()
    if existing and existing["authority"] == "document" and not supersede_anyway:
        raise HTTPException(409, "a document-authority claim exists for this key; "
                                 "re-approve with supersede_anyway=true to override")

    proposer = conn.execute("SELECT name FROM users WHERE id = %s", (pr["proposer_user_id"],)).fetchone()
    proposer_name = proposer["name"] if proposer else pr["proposer_user_id"]
    today = datetime.date.today()
    doc_id = f"wf-{proposal_id}"
    title = f"{scope} workflow — {today.isoformat()} — {proposer_name}"
    vec = embed_passages([f"{title}\n{pr['quote']}"])[0]

    with conn.transaction():
        conn.execute(
            "INSERT INTO documents (id, scope_id, title, source, owner, effective_date, body, authority)"
            " VALUES (%s,%s,%s,'workflow',%s,%s,%s,'workflow')",
            (doc_id, scope, title, proposer_name, today, pr["quote"]),
        )
        chunk_id = conn.execute(
            "INSERT INTO chunks (document_id, scope_id, ord, text, embedding)"
            " VALUES (%s,%s,0,%s,%s) RETURNING id",
            (doc_id, scope, pr["quote"], vec),
        ).fetchone()["id"]
        claim_id = conn.execute(
            "INSERT INTO claims (subject, attribute, condition, value, quote, valid_from,"
            " source_chunk_id, scope_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (subject, attribute, condition, pr["value"], pr["quote"], today, chunk_id, scope),
        ).fetchone()["id"]
        if existing:
            conn.execute("UPDATE claims SET superseded_by = %s WHERE id = %s", (claim_id, existing["id"]))
        conn.execute(
            "UPDATE proposals SET status='approved', decided_by=%s, decided_at=now(),"
            " resulting_claim_id=%s WHERE id=%s",
            (decided_by, claim_id, proposal_id),
        )
    return {"status": "approved", "claim_id": claim_id, "document_id": doc_id,
            "title": title, "superseded_claim_id": existing["id"] if existing else None}


def reject(p, *, proposal_id: str) -> dict:
    pr = _load_pending_owned(p, proposal_id)
    p.conn.execute(
        "UPDATE proposals SET status='rejected', decided_by=%s, decided_at=now() WHERE id=%s",
        (p.user_id, proposal_id),
    )
    return {"status": "rejected", "proposal_id": proposal_id}
