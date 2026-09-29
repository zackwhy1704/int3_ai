"""Measure how well relevance scores separate supported from unsupported questions.

    docker compose exec backend python -m app.calibrate

Uses the union of all scopes, so "supported" means supported somewhere in the
corpus. Prints the best embedding cosine and the best cross-encoder score for
each question.
"""
import os
from pathlib import Path

import yaml

from .rerank import rerank
from .retrieve import search

ALL_SCOPES = ["company-wide", "operations", "finance", "leadership"]
SEED_DIR = Path(os.environ.get("SEED_DIR", "/seed"))


def best(question: str) -> tuple[float, float, str]:
    hits = search(question, ALL_SCOPES)
    cosine = max(float(h["score"]) for h in hits)
    top = rerank(question, hits)[0]
    return cosine, top["relevance"], top["doc_title"]


def main() -> None:
    q = yaml.safe_load((SEED_DIR / "questions.yaml").read_text())
    stats = {}
    for label, questions in [("supported", q["answerable"]), ("unsupported", q["unsupported"])]:
        print(f"== {label}  (cosine | cross-encoder | top document)")
        rows = [(question, *best(question)) for question in questions]
        for question, cos, rel, title in rows:
            print(f"  {cos:.3f} | {rel:7.2f} | {title[:40]:40} | {question}")
        stats[label] = ([r[1] for r in rows], [r[2] for r in rows])
    (sc, sr), (uc, ur) = stats["supported"], stats["unsupported"]
    print(f"\ncosine        supported {min(sc):.3f}..{max(sc):.3f}  unsupported {min(uc):.3f}..{max(uc):.3f}"
          f"  gap {min(sc) - max(uc):+.3f}")
    print(f"cross-encoder supported {min(sr):.2f}..{max(sr):.2f}  unsupported {min(ur):.2f}..{max(ur):.2f}"
          f"  gap {min(sr) - max(ur):+.2f}")


if __name__ == "__main__":
    main()
