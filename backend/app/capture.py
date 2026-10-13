"""Phase 2 capture: propose a fact in a workflow, owner approves, it becomes a
claim — via a WORKFLOW RECORD so the passage-first retrieval path (answer.py)
finds and cites it unchanged.

Hardening (Phase 2 audits):
  * ONE statement builder: the SQL function capture_statement. The confirmation
    preview, the stored chunk text, the embedded text and the retrieval text are
    all its output — there is no Python copy. condition is normalised (blank ->
    NULL) at propose time so an empty-string condition can't fork the claim key;
    quote is evidence only, capped at MAX_QUOTE, never embedded.
  * self-approval is denied unless the scope opts in (scopes.allow_self_approval);
  * the approve WRITE goes through the SECURITY DEFINER function
    approve_workflow_proposal (runtime role has EXECUTE only). Those in-DB
    owner/self checks rely on p_decided_by passed by the runtime role: they bound
    damage to pending, member-confirmed proposals and make the write atomic, but
    are NOT an identity check independent of the application layer (the app
    authenticates the principal and supplies its user id). The admin operator CLI
    writes directly via _write_workflow_record.

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

MAX_QUOTE = 500
_ERRCODE_HTTP = {"CB404": 404, "CB403": 403, "CBSLF": 403, "CB409": 409}


def _norm_condition(c):
    """Blank condition -> NULL so '', '  ' and None all mean the default rule
    (same claim key) rather than three distinct keys."""
    if c is None:
        return None
    c = c.strip()
    return c or None


def _statement(conn, subject, attribute, condition, value) -> str:
    """The one statement builder: the SQL capture_statement function."""
    return conn.execute(
        "SELECT capture_statement(%s, %s, %s, %s) AS s", (subject, attribute, condition, value)
    ).fetchone()["s"]


def preview(p, *, subject, attribute, value, condition=None) -> dict:
    """Exactly the text that propose would store/embed for these fields (condition
    normalised). Shown by the sidecar at propose-time so the human confirms the
    real stored statement."""
    return {"statement": _statement(p.conn, subject, attribute, _norm_condition(condition), value)}


def _owner_of(p, scope):
    row = p.conn.execute("SELECT owner_user_id FROM scopes WHERE id = %s", (scope,)).fetchone()
    return row["owner_user_id"] if row else None


def _allow_self(p, scope):
    row = p.conn.execute("SELECT allow_self_approval FROM scopes WHERE id = %s", (scope,)).fetchone()
    return bool(row and row["allow_self_approval"])


def _active_claim_for_key(p, scope, subject, attribute, condition):
    return p.conn.execute(
        "SELECT cl.id, cl.value, d.authority, d.title AS doc_title FROM claims cl"
        " JOIN chunks c ON c.id = cl.source_chunk_id JOIN documents d ON d.id = c.document_id"
        " WHERE cl.scope_id=%s AND cl.subject=%s AND cl.attribute=%s"
        "   AND cl.condition IS NOT DISTINCT FROM %s AND cl.superseded_by IS NULL",
        (scope, subject, attribute, condition),
    ).fetchone()


def propose(p, *, scope, subject, attribute, value, quote, condition=None, session_label=None) -> dict:
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
    condition = _norm_condition(condition)
    quote = (quote or "")[:MAX_QUOTE]
    row = p.conn.execute(
        "INSERT INTO proposals (scope_id, subject, attribute, condition, value, quote,"
        " proposer_user_id, session_label) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (scope, subject, attribute, condition, value, quote, p.user_id, session_label),
    ).fetchone()
    return {"proposal_id": str(row["id"]), "status": "pending",
            "statement": _statement(p.conn, subject, attribute, condition, value)}


def get_proposal(p, *, proposal_id) -> dict:
    r = p.conn.execute(
        "SELECT *, capture_statement(subject, attribute, condition, value) AS statement"
        " FROM proposals WHERE id = %s", (proposal_id,)).fetchone()
    if r is None:
        raise HTTPException(404, "no such proposal")
    _require_owner(p, r["scope_id"])
    conflict = _active_claim_for_key(p, r["scope_id"], r["subject"], r["attribute"], r["condition"])
    return {
        "proposal_id": str(r["id"]), "scope": r["scope_id"], "status": r["status"],
        "statement": r["statement"], "quote": r["quote"], "proposer": r["proposer_user_id"],
        "created_at": r["created_at"].isoformat(),
        "conflict": None if not conflict else {
            "existing_value": conflict["value"], "authority": conflict["authority"],
            "doc_title": conflict["doc_title"]},
    }


def _require_owner(p, scope):
    if _owner_of(p, scope) != p.user_id:
        raise HTTPException(403, "only the scope owner may review proposals")


def list_proposals(p, *, scope) -> list[dict]:
    _require_owner(p, scope)
    out = []
    for r in p.conn.execute(
        "SELECT id, value, quote, proposer_user_id, session_label, created_at,"
        " capture_statement(subject, attribute, condition, value) AS statement,"
        " subject, attribute, condition FROM proposals"
        " WHERE scope_id=%s AND status='pending' ORDER BY created_at", (scope,),
    ).fetchall():
        conflict = _active_claim_for_key(p, scope, r["subject"], r["attribute"], r["condition"])
        out.append({
            "proposal_id": str(r["id"]), "statement": r["statement"], "value": r["value"],
            "quote": r["quote"], "proposer": r["proposer_user_id"],
            "session_label": r["session_label"], "created_at": r["created_at"].isoformat(),
            "conflict": None if not conflict else {
                "existing_value": conflict["value"], "authority": conflict["authority"],
                "doc_title": conflict["doc_title"]},
        })
    return out


def _load_pending(p, proposal_id):
    row = p.conn.execute("SELECT * FROM proposals WHERE id=%s AND status='pending'", (proposal_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such pending proposal")
    return row


def _deny_non_owner_or_self(p, pr):
    _require_owner(p, pr["scope_id"])
    if p.user_id == pr["proposer_user_id"] and not _allow_self(p, pr["scope_id"]):
        raise HTTPException(403, "self-approval is not allowed for this scope")


def approve(p, *, proposal_id, supersede_anyway=False) -> dict:
    pr = _load_pending(p, proposal_id)
    _deny_non_owner_or_self(p, pr)
    existing = _active_claim_for_key(p, pr["scope_id"], pr["subject"], pr["attribute"], pr["condition"])
    if existing and existing["authority"] == "document" and not supersede_anyway:
        raise HTTPException(409, "a document-authority claim exists for this key; "
                                 "re-approve with supersede_anyway=true to override")
    statement = _statement(p.conn, pr["subject"], pr["attribute"], pr["condition"], pr["value"])
    vec = embed_passages([statement])[0]
    try:
        row = p.conn.execute("SELECT * FROM approve_workflow_proposal(%s,%s,%s,%s)",
                             (proposal_id, p.user_id, supersede_anyway, vec)).fetchone()
    except psycopg.errors.Error as e:
        code = _ERRCODE_HTTP.get(getattr(e, "sqlstate", None))
        if code:
            raise HTTPException(code, str(e).splitlines()[0]) from e
        raise
    return {"status": "approved", "claim_id": row["claim_id"], "document_id": row["document_id"],
            "title": row["title"], "statement": row["statement"],
            "superseded_claim_id": row["superseded_claim_id"]}


def reject(p, *, proposal_id) -> dict:
    pr = _load_pending(p, proposal_id)
    _deny_non_owner_or_self(p, pr)
    p.conn.execute("UPDATE proposals SET status='rejected', decided_by=%s, decided_at=now() WHERE id=%s",
                   (p.user_id, proposal_id))
    return {"status": "rejected", "proposal_id": proposal_id}


def _write_workflow_record(conn, pr, *, decided_by, supersede_anyway) -> dict:
    """Direct write for the ADMIN operator CLI only (admin conn). Mirrors
    approve_workflow_proposal but is an operator override (no owner/self check);
    still enforces the document-authority 409 and uses capture_statement."""
    scope, subject, attribute, condition = pr["scope_id"], pr["subject"], pr["attribute"], pr["condition"]
    existing = conn.execute(
        "SELECT cl.id, d.authority FROM claims cl JOIN chunks c ON c.id=cl.source_chunk_id"
        " JOIN documents d ON d.id=c.document_id WHERE cl.scope_id=%s AND cl.subject=%s AND cl.attribute=%s"
        "   AND cl.condition IS NOT DISTINCT FROM %s AND cl.superseded_by IS NULL",
        (scope, subject, attribute, condition)).fetchone()
    if existing and existing["authority"] == "document" and not supersede_anyway:
        raise HTTPException(409, "a document-authority claim exists; use supersede_anyway")
    proposer = conn.execute("SELECT name FROM users WHERE id=%s", (pr["proposer_user_id"],)).fetchone()
    proposer_name = proposer["name"] if proposer else pr["proposer_user_id"]
    statement = _statement(conn, subject, attribute, condition, pr["value"])
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
