"""Question -> scoped retrieval -> relevance gate -> schema-constrained answer."""
import hashlib
import logging

from psycopg.types.json import Jsonb

from . import llm, retrieve
from .db import connect
from .rerank import rerank

log = logging.getLogger("answer")

# Cross-encoder logit below which a chunk is not treated as support. Measured with
# app/calibrate.py on the seed questions: supported top scores 2.56..9.69,
# unsupported -11.43..-6.91. 0 sits inside that gap (logit 0 = 50% relevance).
REFUSE_THRESHOLD = 0.0

SYSTEM = (
    "You answer questions for staff of a company using ONLY the numbered passages "
    "and the fact ledger provided. If they do not answer the question, set supported "
    "to false. Cite every passage you used by its id. The ledger says which values "
    "are current and which were replaced; state current values only, and when the "
    "ledger lists several conditions for the same thing, give each. Be brief: one or "
    "two sentences."
)

FACT_SQL = """
    SELECT cl.id, cl.subject, cl.attribute, cl.condition, cl.value, cl.quote,
           cl.valid_from, cl.superseded_by, cl.scope_id, cl.source_chunk_id AS chunk_id,
           d.title AS doc_title
    FROM claims cl JOIN chunks c ON c.id = cl.source_chunk_id
    JOIN documents d ON d.id = c.document_id
    WHERE cl.scope_id = ANY(%(scopes)s) AND {where}
"""


def _fact(row: dict) -> dict:
    return {"value": row["value"], "quote": row["quote"], "valid_from": str(row["valid_from"]),
            "chunk_id": row["chunk_id"], "doc_title": row["doc_title"]}


def facts(chunk_ids: list[int], scopes: list[str]) -> list[dict]:
    """Current claims touched by these chunks, each with its predecessor if any.

    A superseded claim in a chunk is followed forward to the current one. Every
    lookup is filtered by the asker's scopes, like retrieval."""
    with connect() as conn:
        def one(where: str, **params) -> dict | None:
            return conn.execute(FACT_SQL.format(where=where), {"scopes": scopes, **params}).fetchone()

        heads = {}
        for row in conn.execute(FACT_SQL.format(where="cl.source_chunk_id = ANY(%(ids)s)"),
                                {"scopes": scopes, "ids": chunk_ids}).fetchall():
            while row is not None and row["superseded_by"] is not None:
                row = one("cl.id = %(id)s", id=row["superseded_by"])
            if row is not None:
                heads[row["id"]] = row

        result = []
        for head in heads.values():
            prev = one("cl.superseded_by = %(id)s", id=head["id"])
            result.append({
                "subject": head["subject"], "attribute": head["attribute"],
                "condition": head["condition"], "scope": head["scope_id"],
                "current": _fact(head), "previous": _fact(prev) if prev else None,
            })
    return sorted(result, key=lambda f: (f["subject"], f["attribute"], f["condition"] or ""))


def ledger_text(fs: list[dict]) -> str:
    lines = []
    for f in fs:
        cond = f" (only for: {f['condition']})" if f["condition"] else ""
        line = (f"- {f['subject']} / {f['attribute']}{cond}: {f['current']['value']}"
                f" (current since {f['current']['valid_from']})")
        if f["previous"]:
            line += (f"; replaced {f['previous']['value']}"
                     f" (valid from {f['previous']['valid_from']})")
        lines.append(line)
    return "\n".join(lines) or "(no recorded facts)"


def refusal(best: dict | None) -> dict:
    return {
        "refused": True,
        "message": "No reliable source found",
        # Owner of the closest in-scope document: the person most likely to know.
        "suggested_owner": best["owner"] if best else None,
    }


def answer_schema(ids: list[int]) -> dict:
    # Structural citations: the only values the model can put in cited_chunk_ids
    # are the ids of the passages it was given.
    return {
        "type": "object",
        "properties": {
            "supported": {"type": "boolean"},
            "answer": {"type": "string"},
            "cited_chunk_ids": {"type": "array", "items": {"type": "integer", "enum": ids}},
        },
        "required": ["supported", "answer", "cited_chunk_ids"],
        "additionalProperties": False,
    }


def cache_key(question: str, scopes: list[str]) -> str:
    # Keyed on the resolved scope set, never on the question alone.
    raw = " ".join(question.lower().split()) + "|" + ",".join(sorted(scopes))
    return hashlib.sha256(raw.encode()).hexdigest()


def ask(question: str, scopes: list[str]) -> dict:
    """Live answer, cached on success. If the model call fails, serve the last real
    answer for the same question and scopes, flagged as cached. Nothing is made up:
    with no cached answer, the caller gets llm.Unavailable."""
    key = cache_key(question, scopes)
    with connect() as conn:
        try:
            result = answer_live(question, scopes)
        except llm.Unavailable as e:
            log.error("model unavailable, trying cache: %s", e)
            row = conn.execute("SELECT response, created_at FROM answer_cache WHERE key = %s",
                               (key,)).fetchone()
            if row is None:
                raise
            return {**row["response"], "cached": True, "cached_at": row["created_at"].isoformat()}
        conn.execute(
            "INSERT INTO answer_cache (key, question, scopes, response) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (key) DO UPDATE SET response = EXCLUDED.response, created_at = now()",
            (key, question, sorted(scopes), Jsonb(result)),
        )
    return {**result, "cached": False}


def answer_live(question: str, scopes: list[str]) -> dict:
    hits = rerank(question, retrieve.search(question, scopes))
    context = [h for h in hits if h["relevance"] >= REFUSE_THRESHOLD]
    if not context:
        return refusal(hits[0] if hits else None)

    passages = "\n\n".join(
        f"[id {h['chunk_id']}] {h['doc_title']} ({h['source']}, {h['effective_date']})\n{h['text']}"
        for h in context
    )
    ledger = ledger_text(facts([h["chunk_id"] for h in context], scopes))
    out = llm.structured(
        SYSTEM,
        f"Passages:\n\n{passages}\n\nFact ledger:\n{ledger}\n\nQuestion: {question}",
        answer_schema([h["chunk_id"] for h in context]),
    )

    by_id = {h["chunk_id"]: h for h in context}
    cited = list(dict.fromkeys(out["cited_chunk_ids"]))
    # Backstop for the schema: never silently repair, refuse and log instead.
    if any(c not in by_id for c in cited):
        log.error("model cited ids outside the retrieved set: %s", cited)
        return refusal(context[0])
    if not out["supported"] or not cited:
        return refusal(context[0])

    return {
        "refused": False,
        "answer": out["answer"],
        "citations": [
            {"chunk_id": c, "document_id": by_id[c]["document_id"],
             "doc_title": by_id[c]["doc_title"], "source": by_id[c]["source"],
             "owner": by_id[c]["owner"], "effective_date": str(by_id[c]["effective_date"]),
             "scope": by_id[c]["scope"], "text": by_id[c]["text"]}
            for c in cited
        ],
        "answered_from": sorted({by_id[c]["scope"] for c in cited}),
        # From the claims table, not from the model: what is current, and what it replaced.
        "facts": facts(cited, scopes),
    }
