"""Truth layer: extract dated claims from chunks, and supersede on real change.

Two separate model calls, never merged:
- extract(): pulls (subject, attribute, condition, value, quote) out of a passage.
- judge():   a narrow binary call on two value strings: same fact or changed?

A claim's key is (scope, subject, attribute, condition). Only a claim with the
same key can supersede another, so a conditional exception ("45 days for annual
contracts") and the default rule ("30 days") always coexist.
"""

import logging

import psycopg

from . import llm

log = logging.getLogger("claims")

EXTRACT_SYSTEM = (
    "You extract factual claims from a company document passage. Extract only "
    "claims that state a specific rule or value: a duration, amount, limit, date, "
    "rate, deadline or named owner. For each claim give:\n"
    "- subject: what the claim is about, short and lowercase (e.g. 'enterprise customers')\n"
    "- attribute: the property, short and lowercase (e.g. 'refund window')\n"
    "- condition: lowercase qualifier that limits who or what the claim applies to, "
    "or an empty string if it applies to everyone. A condition can qualify the thing "
    "(e.g. 'annual contract'), the PEOPLE it applies to (e.g. 'board members', "
    "'warehouse staff', 'new starters'), or the time (e.g. 'fridays'). Phrases like "
    "'for board members', 'only for managers' or 'on Fridays' are conditions.\n"
    "- value: the value, as stated\n"
    "- quote: the exact sentence from the passage that states it, copied verbatim\n"
    "If an existing key below describes the same subject, attribute and condition, "
    "reuse its exact wording. Never drop or change a qualifier to match an existing "
    "key: a claim that applies only to some people or cases is a different key from "
    "one that applies to everyone. Return an empty list if the passage states no such value."
)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "attribute": {"type": "string"},
                    "condition": {"type": "string"},
                    "value": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["subject", "attribute", "condition", "value", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = (
    "You compare two values recorded for the same attribute of the same thing. "
    "Answer changed=false if they state the same fact, even if worded differently "
    "(e.g. '14 days' and 'fourteen days' and 'two weeks'). Answer changed=true only "
    "if the fact itself is different."
)

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"changed": {"type": "boolean"}},
    "required": ["changed"],
    "additionalProperties": False,
}


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def extract(conn: psycopg.Connection, text: str, scope_id: str) -> list[dict]:
    keys = conn.execute(
        "SELECT DISTINCT subject, attribute, coalesce(condition, '') AS condition"
        " FROM claims WHERE scope_id = %s ORDER BY 1, 2, 3",
        (scope_id,),
    ).fetchall()
    key_lines = "\n".join(
        f"- {k['subject']} | {k['attribute']} | {k['condition']}" for k in keys
    )
    out = llm.structured(
        EXTRACT_SYSTEM,
        f"Existing keys (subject | attribute | condition):\n{key_lines or '(none)'}\n\nPassage:\n{text}",
        EXTRACT_SCHEMA,
        max_tokens=2048,
    )
    claims = []
    for c in out["claims"]:
        if c["quote"] not in text:
            log.warning("dropped claim, quote not verbatim in passage: %r", c["quote"])
            continue
        claims.append(
            {
                "subject": _norm(c["subject"]),
                "attribute": _norm(c["attribute"]),
                "condition": _norm(c["condition"]) or None,
                "value": c["value"].strip(),
                "quote": c["quote"],
            }
        )
    return claims


def judge(old_value: str, new_value: str) -> bool:
    """True if the two value strings state different facts. Model call only."""
    out = llm.structured(
        JUDGE_SYSTEM, f"Value A: {old_value}\nValue B: {new_value}", JUDGE_SCHEMA
    )
    return out["changed"]


def is_change(old_value: str, new_value: str) -> bool:
    if _norm(old_value) == _norm(new_value):
        return False
    return judge(old_value, new_value)


def record(
    conn: psycopg.Connection, claim: dict, chunk_id: int, scope_id: str, valid_from
) -> str:
    """Append one claim. Returns 'new', 'unchanged', 'superseded' or 'historic'."""
    current = conn.execute(
        "SELECT id, value, valid_from FROM claims"
        " WHERE scope_id = %s AND subject = %s AND attribute = %s"
        " AND condition IS NOT DISTINCT FROM %s AND superseded_by IS NULL",
        (scope_id, claim["subject"], claim["attribute"], claim["condition"]),
    ).fetchone()
    if current is not None and not is_change(current["value"], claim["value"]):
        return "unchanged"

    new_is_older = current is not None and valid_from < current["valid_from"]
    with conn.transaction():
        new_id = conn.execute(
            "INSERT INTO claims (subject, attribute, condition, value, quote, valid_from,"
            " superseded_by, source_chunk_id, scope_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                claim["subject"],
                claim["attribute"],
                claim["condition"],
                claim["value"],
                claim["quote"],
                valid_from,
                current["id"] if new_is_older else None,
                chunk_id,
                scope_id,
            ),
        ).fetchone()["id"]
        if current is None:
            return "new"
        if new_is_older:
            # Arrived out of order: it is history, already superseded by the current claim.
            return "historic"
        conn.execute(
            "UPDATE claims SET superseded_by = %s WHERE id = %s",
            (new_id, current["id"]),
        )
    return "superseded"


def extract_all(conn: psycopg.Connection) -> None:
    """Extract claims from every chunk, oldest document first.

    Claim-selection rule: among sources dated the same day, the highest-authority
    source is ingested first, so it becomes the claim of record and lower-authority
    restatements of the same value are recorded as 'unchanged'."""
    chunks = conn.execute(
        "SELECT c.id, c.text, c.scope_id, d.effective_date FROM chunks c"
        " JOIN documents d ON d.id = c.document_id"
        " ORDER BY d.effective_date,"
        "  CASE d.authority WHEN 'document' THEN 0 WHEN 'chat' THEN 1 ELSE 2 END,"
        "  d.id, c.ord"
    ).fetchall()
    for ch in chunks:
        for claim in extract(conn, ch["text"], ch["scope_id"]):
            outcome = record(
                conn, claim, ch["id"], ch["scope_id"], ch["effective_date"]
            )
            log.info(
                "claim %s | %s | %s = %s -> %s",
                claim["subject"],
                claim["attribute"],
                claim["condition"],
                claim["value"],
                outcome,
            )
