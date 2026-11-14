"""Local mode M2: each employee's assistant memory, stored on OUR server (docs/LOCAL_MODE_DESIGN.md §3.3).

Hermes's built-in memory is off on the PC; the int3 memory plugin (launcher/hermes-plugin/int3) saves and
recalls here with the user's brain token. Explicit memories only: something is stored when the user asks
the assistant to remember it, never whole conversations.

Isolation: memories live in the TENANT's own database, and every query is keyed by the user id the
server resolved from the token; no request ever names a user. Deletion is a hard delete. Every deletion
(one, all, or the retention purge after a tenant is cancelled) is written to memory_audit, with counts
only, never content.
"""

import logging

from .. import config
from ..db import control_conn, tenant_conn
from ..embed import embed_passages, embed_query
from ..rerank import _model as _cross_encoder

log = logging.getLogger("launcher.memory")

MAX_CONTENT = 2000
MAX_PER_USER = 500
KINDS = ("fact", "preference")
# Recall of FACTS: vector search picks RECALL_CANDIDATES, the cross-encoder (app/rerank.py) scores them,
# and at most RECALL_K are kept that score >= RECALL_MIN_SCORE and within RECALL_MARGIN of the best.
# Measured by app/launcher/memory_calibrate.py (20 memory/query pairs + 10 unrelated queries): -8.0 / 2.0
# gave precision 0.89, recall 0.80, and nothing for any unrelated query (their best was -9.2).
# Embedding cosine alone could not separate them (precision 0.28-0.60 at recall 0.60-0.95).
RECALL_K = 3
RECALL_CANDIDATES = 10
RECALL_MIN_SCORE = -8.0
RECALL_MARGIN = 2.0
# PREFERENCES (how the user wants the assistant to work) do not match any one question, so the newest
# few are given on every turn instead of being searched.
PREFERENCES_PER_TURN = 5


def audit(tenant_id: str, user_id: str | None, action: str, count: int, actor: str, device_id=None) -> None:
    with control_conn() as control:
        control.execute(
            "INSERT INTO memory_audit (tenant_id, user_id, device_id, action, count, actor)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (tenant_id, user_id, device_id, action, count, actor),
        )


def save(conn, user_id: str, content: str, session: str | None = None, kind: str = "fact") -> dict:
    if kind not in KINDS:
        raise ValueError("kind must be fact or preference")
    content = " ".join((content or "").split())
    if not content:
        raise ValueError("nothing to remember")
    if len(content) > MAX_CONTENT:
        raise ValueError(f"a memory is limited to {MAX_CONTENT} characters")
    n = conn.execute("SELECT count(*) AS n FROM user_memories WHERE user_id = %s", (user_id,)).fetchone()["n"]
    if n >= MAX_PER_USER:
        raise ValueError(f"memory is full ({MAX_PER_USER} items); delete some in int3 first")
    row = conn.execute(
        "INSERT INTO user_memories (user_id, content, embedding, source_session, kind)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at",
        (user_id, content, embed_passages([content])[0], (session or "")[:200] or None, kind),
    ).fetchone()
    return {"id": row["id"], "kind": kind, "content": content, "created_at": row["created_at"].isoformat()}


def _out(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "content": r["content"], "created_at": r["created_at"].isoformat()}


def preferences(conn, user_id: str, limit: int = PREFERENCES_PER_TURN) -> list[dict]:
    rows = conn.execute(
        "SELECT id, kind, content, created_at FROM user_memories WHERE user_id = %s AND kind = 'preference'"
        " ORDER BY created_at DESC, id DESC LIMIT %s", (user_id, limit)).fetchall()
    return [_out(r) for r in rows]


def recall(conn, user_id: str, query: str, k: int = RECALL_K) -> list[dict]:
    """The user's FACTS relevant to ``query`` (see RECALL_* above); [] when nothing is relevant."""
    query = (query or "").strip()
    if not query:
        return []
    rows = conn.execute(
        "SELECT id, kind, content, created_at FROM user_memories WHERE user_id = %(u)s AND kind = 'fact'"
        " ORDER BY embedding <=> %(v)s LIMIT %(n)s",
        {"v": embed_query(query), "u": user_id, "n": RECALL_CANDIDATES},
    ).fetchall()
    if not rows:
        return []
    scores = list(map(float, _cross_encoder().rerank(query, [r["content"] for r in rows])))
    ranked = sorted(zip(scores, rows, strict=True), key=lambda sr: -sr[0])[: max(1, min(int(k), RECALL_K))]
    best = ranked[0][0]
    return [_out(r) for s, r in ranked if s >= RECALL_MIN_SCORE and s >= best - RECALL_MARGIN]


def list_all(conn, user_id: str, limit: int = 100, offset: int = 0) -> dict:
    total = conn.execute("SELECT count(*) AS n FROM user_memories WHERE user_id = %s", (user_id,)).fetchone()["n"]
    rows = conn.execute(
        "SELECT id, kind, content, created_at FROM user_memories WHERE user_id = %s"
        " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
        (user_id, max(1, min(limit, 200)), max(0, offset)),
    ).fetchall()
    return {"total": total, "memories": [_out(r) for r in rows]}


def delete_one(conn, user_id: str, memory_id: int) -> int:
    return conn.execute(
        "DELETE FROM user_memories WHERE id = %s AND user_id = %s", (memory_id, user_id)
    ).rowcount


def delete_all(conn, user_id: str) -> int:
    return conn.execute("DELETE FROM user_memories WHERE user_id = %s", (user_id,)).rowcount


def purge_cancelled(days: int | None = None) -> list[tuple[str, int]]:
    """Delete every memory of tenants cancelled more than ``days`` ago (default MEMORY_RETENTION_DAYS).
    Returns (tenant_id, rows_deleted) per tenant touched. Run by the operator CLI / a daily timer."""
    days = config.MEMORY_RETENTION_DAYS if days is None else days
    with control_conn() as control:
        due = [r["id"] for r in control.execute(
            "SELECT id FROM tenants WHERE cancelled_at IS NOT NULL"
            " AND cancelled_at < now() - make_interval(days => %s) ORDER BY id", (days,)).fetchall()]
    out = []
    for tenant_id in due:
        with tenant_conn(tenant_id) as conn:
            n = conn.execute("DELETE FROM user_memories").rowcount
        if n:
            audit(tenant_id, None, "purge", n, "retention")
        out.append((tenant_id, n))
    return out
