"""Cross-encoder relevance scores for already-scope-filtered chunks.

The refusal threshold is applied to these scores, not to embedding cosine
similarity: measured on the seed corpus, bge-small cosine left a 0.04 gap between
the weakest supported question and the strongest unsupported one, while this
cross-encoder left a gap of about 9.5 logits. See app/calibrate.py.
"""
from functools import lru_cache

from fastembed.rerank.cross_encoder import TextCrossEncoder

MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


@lru_cache(maxsize=1)
def _model() -> TextCrossEncoder:
    return TextCrossEncoder(MODEL, cache_dir="/models")


def rerank(query: str, hits: list[dict]) -> list[dict]:
    """Return hits sorted by cross-encoder score, each with a 'relevance' key."""
    if not hits:
        return []
    scores = _model().rerank(query, [f"{h['doc_title']}\n{h['text']}" for h in hits])
    for h, s in zip(hits, scores, strict=True):
        h["relevance"] = float(s)
    return sorted(hits, key=lambda h: h["relevance"], reverse=True)
