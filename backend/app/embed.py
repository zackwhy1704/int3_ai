from functools import lru_cache

import numpy as np
from fastembed import TextEmbedding

MODEL = "BAAI/bge-small-en-v1.5"
# bge models expect this prefix on queries (not on passages).
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def _model() -> TextEmbedding:
    return TextEmbedding(MODEL, cache_dir="/models")


def embed_passages(texts: list[str]) -> list[np.ndarray]:
    return list(_model().embed(texts))


def embed_query(text: str) -> np.ndarray:
    return next(iter(_model().embed([QUERY_PREFIX + text])))
