"""Phase 2 capture: propose a fact in a workflow, owner approves, it becomes a
claim — via a WORKFLOW RECORD so the passage-first retrieval path (answer.py)
finds and cites it unchanged.

Hardening (Phase 2 audit):
  * the embedded/retrieved chunk text is built SERVER-SIDE from the confirmed
    fields (build_statement), never the free-form quote; quote is evidence only,
    capped at MAX_QUOTE and never embedded;
  * self-approval is denied unless the scope opts in (scopes.allow_self_approval);
  * the approve WRITE goes through the SECURITY DEFINER function
    approve_workflow_proposal (runtime role has EXECUTE only — no direct write on
    documents/chunks/claims). The admin operator CLI writes directly via
    _write_workflow_record.

Provenance is server-derived: proposer_user_id and timestamps come from the
principal / server clock. session_label is model-supplied DISPLAY-ONLY text.
"""

import datetime
import logging

import psycopg
from fastapi import HTTPException

from . import config
from .embed import embed_passages

log = logging.getLogger("capture")

MAX_QUOTE = 500  # evidence only; capped so a huge free-form blob can't be stored

# Map the SECURITY DEFINER function's custom SQLSTATEs to HTTP.
_ERRCODE_HTTP = {"CB404": 404, "CB403": 403, "CBSLF": 403, "CB409": 409}


def build_statement(subject: str, attribute: str, condition, value: str) -> str:
    """The exact text embedded, retrieved and shown at confirmation — from the
    CONFIRMED fields, never the quote. Must match the SQL in
    approve_workflow_proposal byte-for-byte."""
    cond = f" ({condition})" if condition else ""
    return f"{subject} — {attribute}{cond}: {value}"


def _owner_of(p, scope: str) -> str | None:
    row = p.conn.execute("SELECT owner_user_id FROM scopes WHERE id = %s", (scope,)).fetchone()
    return row["owner_user_id"] if row else None


def _allow_self(p, scope: str) -> bool:
    row = p.conn.execute("SELECT allow_self_approval FROM scopes WHERE id = %s", (scope,)).fetchone()
    return bool(row and row["allow_self_approval"])


def _active_claim_for_key(p, scope, subject, attribute, condition) -> dict | None:
    return p.conn.execute(
        "SELECT cl.id, cl.value, d.authority, d.title AS doc_title FROM claims cl"
        " JOIN chunks c ON c.id = cl.source_chunk_id JOIN documents d ON d.id = c.document_id"
        " WHERE cl.scope_id=%s AND cl.subject=%s AND cl.attribute=%s"
        "   AND cl.condition IS NOT DISTINCT FROM %s AND cl.superseded_by IS NULL",
        (scope, subject, attribute, condition),
    ).fetchone()


def propose(p, *, scope, subject, attribute, value, quote, condition=None, session_label=None) -> dict:
    """Record a pending proposal. 403 if not a member of `scope`; 429 over the
    per-user hourly rate limit. quote is capped (evidence only)."""
    if scope not in p.scopes:
        raise HTTPException(403, f"not a member of scope {scope!r}")
    limit = config.PROPOSAL_RATE_PER_HOUR
    if limit > 0:
        recent = p.conn.execute(
            "SELECT count(*) AS n FROM proposals WHERE proposer_user_id = %s"
            " AND created_at > now() - interval '1 hour'", (p.user_id,),
        ).fetchone()["n"]
        if recent >= limit:
            raise HTTPException(429, f"proposal rate limit of {limit}/hour reached")
    quote = (quote or "")[:MAX_QUOTE]
    row = p.conn.execute(
        "INSERT INTO proposals (scope_id, subject, attribute, condition, value, quote,"
        " proposer_user_id, session_label) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (scope, subject, attribute, condition, value, quote, p.user_id, session_label),
    ).fetchone()
    return {"proposal_id": str(row["id"]), "status": "pending",
            "statement": build_statement(subject, attribute, condition, value)}


def get_proposal(p, *, proposal_id: str) -> dict:
    """One proposal the caller OWNS, with the exact stored statement, evidence,
    proposer, timestamp and any conflicting active claim — for the approve
    elicitation (so the owner confirms on full detail, not an opaque id)."""
    r = p.conn.execute("SELECT * FROM proposals WHERE id = %s", (proposal_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "no such proposal")
    _require_owner(p, r["scope_id"])
    conflict = _active_claim_for_key(p, r["scope_id"], r["subject"], r["attribute"], r["condition"])
    return {
        "proposal_id": str(r["id"]), "scope": r["scope_id"], "status": r["status"],
        "statement": build_statement(r["subject"], r["attribute"], r["condition"], r["value"]),
        "quote": r["quote"], "proposer": r["proposer_user_id"],
        "created_at": r["created_at"].isoformat(),
        "conflict": None if not conflict else {
            "existing_value": conflict["value"], "authority": conflict["authority"],
            "doc_title": conflict["doc_title"]},
    }


def _require_owner(p, scope: str) -> None:
    if _owner_of(p, scope) != p.user_id:
        raise HTTPException(403, "only the scope owner may review proposals")


def list_proposals(p, *, scope) -> list[dict]:
    _require_owner(p, scope)
    out = []
    for r in p.conn.execute(
        "SELECT id, subject, attribute, condition, value, quote, proposer_user_id,"
        " session_label, created_at FROM proposals WHERE scope_id=%s AND status='pending'"
        " ORDER BY created_at", (scope,),
    ).fetchall():
        conflict = _active_claim_for_key(p, scope, r["subject"], r["attribute"], r["condition"])
        out.append({
            "proposal_id": str(r["id"]),
            "statement": build_statement(r["subject"], r["attribute"], r["condition"], r["value"]),
            "value": r["value"], "quote": r["quote"], "proposer": r["proposer_user_id"],
            "session_label": r["session_label"], "created_at": r["created_at"].isoformat(),
            "conflict": None if not conflict else {
                "existing_value": conflict["value"], "authority": conflict["authority"],
                "doc_title": conflict["doc_title"]},
        })
    return out


def _load_pending(p, proposal_id: str) -> dict:
    row = p.conn.execute("SELECT * FROM proposals WHERE id=%s AND status='pending'", (proposal_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such pending proposal")
    return row


def _deny_non_owner_or_self(p, pr: dict) -> None:
    _require_owner(p, pr["scope_id"])
    if p.user_id == pr["proposer_user_id"] and not _allow_self(p, pr["scope_id"]):
        raise HTTPException(403, "self-approval is not allowed for this scope")


def approve(p, *, proposal_id: str, supersede_anyway: bool = False) -> dict:
    """Owner approves (service-principal). App-level checks give clean HTTP codes;
    the SECURITY DEFINER function re-checks owner/pending/self/409 in the DB and
    does the write. The runtime role can only EXECUTE that function."""
    pr = _load_pending(p, proposal_id)
    _deny_non_owner_or_self(p, pr)
    existing = _active_claim_for_key(p, pr["scope_id"], pr["subject"], pr["attribute"], pr["condition"])
    if existing and existing["authority"] == "document" and not supersede_anyway:
        raise HTTPException(409, "a document-authority claim exists for this key; "
                                 "re-approve with supersede_anyway=true to override")
    statement = build_statement(pr["subject"], pr["attribute"], pr["condition"], pr["value"])
    vec = embed_passages([statement])[0]
    try:
        row = p.conn.execute(
            "SELECT * FROM approve_workflow_proposal(%s,%s,%s,%s)",
            (proposal_id, p.user_id, supersede_anyway, vec),
        ).fetchone()
    except psycopg.errors.Error as e:
        code = _ERRCODE_HTTP.get(getattr(e, "sqlstate", None))
        if code:
            raise HTTPException(code, str(e).splitlines()[0]) from e
        raise
    return {"status": "approved", "claim_id": row["claim_id"], "document_id": row["document_id"],
            "title": row["title"], "statement": row["statement"],
            "superseded_claim_id": row["superseded_claim_id"]}


def reject(p, *, proposal_id: str) -> dict:
    pr = _load_pending(p, proposal_id)
    _deny_non_owner_or_self(p, pr)
    p.conn.execute("UPDATE proposals SET status='rejected', decided_by=%s, decided_at=now() WHERE id=%s",
                   (p.user_id, proposal_id))
    return {"status": "rejected", "proposal_id": proposal_id}


def _write_workflow_record(conn, pr: dict, *, decided_by: str, supersede_anyway: bool) -> dict:
    """Direct write for the ADMIN operator CLI only (admin conn; the runtime role
    cannot do this). Mirrors approve_workflow_proposal but bypasses the owner/self
    checks (an operator override) — still enforces the document-authority 409 and
    builds the statement server-side. Records decided_by honestly ('operator:<name>')."""
    scope, subject, attribute, condition = pr["scope_id"], pr["subject"], pr["attribute"], pr["condition"]
    existing = conn.execute(
        "SELECT cl.id, d.authority FROM claims cl JOIN chunks c ON c.id=cl.source_chunk_id"
        " JOIN documents d ON d.id=c.document_id WHERE cl.scope_id=%s AND cl.subject=%s AND cl.attribute=%s"
        "   AND cl.condition IS NOT DISTINCT FROM %s AND cl.superseded_by IS NULL",
        (scope, subject, attribute, condition),
    ).fetchone()
    if existing and existing["authority"] == "document" and not supersede_anyway:
        raise HTTPException(409, "a document-authority claim exists; use supersede_anyway")
    proposer = conn.execute("SELECT name FROM users WHERE id=%s", (pr["proposer_user_id"],)).fetchone()
    proposer_name = proposer["name"] if proposer else pr["proposer_user_id"]
    statement = build_statement(subject, attribute, condition, pr["value"])
    vec = embed_passages([statement])[0]
    today = datetime.date.today()
    doc_id = f"wf-{pr['id']}"
    title = f"{scope} workflow — {today.isoformat()} — {proposer_name}"
    with conn.transaction():
        conn.execute("INSERT INTO documents (id, scope_id, title, source, owner, effective_date, body, authority)"
                     " VALUES (%s,%s,%s,'workflow',%s,%s,%s,'workflow')",
                     (doc_id, scope, title, proposer_name, today, statement))
        chunk_id = conn.execute("INSERT INTO chunks (document_id, scope_id, ord, text, embedding)"
                                " VALUES (%s,%s,0,%s,%s) RETURNING id", (doc_id, scope, statement, vec)).fetchone()["id"]
        claim_id = conn.execute("INSERT INTO claims (subject, attribute, condition, value, quote, valid_from,"
                                " source_chunk_id, scope_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                                (subject, attribute, condition, pr["value"], pr["quote"], today, chunk_id, scope)).fetchone()["id"]
        if existing:
            conn.execute("UPDATE claims SET superseded_by=%s WHERE id=%s", (claim_id, existing["id"]))
        conn.execute("UPDATE proposals SET status='approved', decided_by=%s, decided_at=now(),"
                     " resulting_claim_id=%s WHERE id=%s", (decided_by, claim_id, pr["id"]))
    return {"status": "approved", "claim_id": claim_id, "document_id": doc_id, "title": title}
