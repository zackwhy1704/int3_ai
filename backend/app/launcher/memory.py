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
from ..embed import embed_query

log = logging.getLogger("launcher.memory")

MAX_CONTENT = 2000
MAX_PER_USER = 500
RECALL_K = 5
# Cosine similarity floor for recall; low on purpose (memories are short, queries are whole messages).
RECALL_MIN_SCORE = 0.25


def audit(tenant_id: str, user_id: str | None, action: str, count: int, actor: str, device_id=None) -> None:
    with control_conn() as control:
        control.execute(
            "INSERT INTO memory_audit (tenant_id, user_id, device_id, action, count, actor)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (tenant_id, user_id, device_id, action, count, actor),
        )


def save(conn, user_id: str, content: str, session: str | None = None) -> dict:
    content = " ".join((content or "").split())
    if not content:
        raise ValueError("nothing to remember")
    if len(content) > MAX_CONTENT:
        raise ValueError(f"a memory is limited to {MAX_CONTENT} characters")
    n = conn.execute("SELECT count(*) AS n FROM user_memories WHERE user_id = %s", (user_id,)).fetchone()["n"]
    if n >= MAX_PER_USER:
        raise ValueError(f"memory is full ({MAX_PER_USER} items); delete some in int3 first")
    row = conn.execute(
        "INSERT INTO user_memories (user_id, content, embedding, source_session) VALUES (%s, %s, %s, %s)"
        " RETURNING id, created_at",
        (user_id, content, embed_query(content), (session or "")[:200] or None),
    ).fetchone()
    return {"id": row["id"], "content": content, "created_at": row["created_at"].isoformat()}


def recall(conn, user_id: str, query: str, k: int = RECALL_K) -> list[dict]:
    if not (query or "").strip():
        return []
    rows = conn.execute(
        "SELECT id, content, created_at, 1 - (embedding <=> %(v)s) AS score FROM user_memories"
        " WHERE user_id = %(u)s ORDER BY embedding <=> %(v)s LIMIT %(k)s",
        {"v": embed_query(query), "u": user_id, "k": max(1, min(int(k), 20))},
    ).fetchall()
    return [{"id": r["id"], "content": r["content"], "created_at": r["created_at"].isoformat()}
            for r in rows if float(r["score"]) >= RECALL_MIN_SCORE]


def list_all(conn, user_id: str, limit: int = 100, offset: int = 0) -> dict:
    total = conn.execute("SELECT count(*) AS n FROM user_memories WHERE user_id = %s", (user_id,)).fetchone()["n"]
    rows = conn.execute(
        "SELECT id, content, created_at FROM user_memories WHERE user_id = %s"
        " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
        (user_id, max(1, min(limit, 200)), max(0, offset)),
    ).fetchall()
    return {"total": total,
            "memories": [{"id": r["id"], "content": r["content"], "created_at": r["created_at"].isoformat()}
                         for r in rows]}


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
