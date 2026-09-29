"""Question -> scoped retrieval -> relevance gate -> schema-constrained answer."""
import logging

from . import llm, retrieve
from .rerank import rerank

log = logging.getLogger("answer")

# Cross-encoder logit below which a chunk is not treated as support. Measured with
# app/calibrate.py on the seed questions: supported top scores 2.56..9.69,
# unsupported -11.43..-6.91. 0 sits inside that gap (logit 0 = 50% relevance).
REFUSE_THRESHOLD = 0.0

SYSTEM = (
    "You answer questions for staff of a company using ONLY the numbered passages "
    "provided. If the passages do not answer the question, set supported to false. "
    "Cite every passage you used by its id. When passages give different values for "
    "the same thing, prefer the one with the latest date and say so. Be brief: one or "
    "two sentences."
)


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


def ask(question: str, scopes: list[str]) -> dict:
    hits = rerank(question, retrieve.search(question, scopes))
    context = [h for h in hits if h["relevance"] >= REFUSE_THRESHOLD]
    if not context:
        return refusal(hits[0] if hits else None)

    passages = "\n\n".join(
        f"[id {h['chunk_id']}] {h['doc_title']} ({h['source']}, {h['effective_date']})\n{h['text']}"
        for h in context
    )
    out = llm.structured(
        SYSTEM, f"Passages:\n\n{passages}\n\nQuestion: {question}",
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
    }
