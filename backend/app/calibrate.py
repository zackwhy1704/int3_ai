"""Measure how well relevance scores separate supported from unsupported questions.

    docker compose run --rm tools python -m app.calibrate [--tenant brindlewood]

Uses the union of all scopes, so "supported" means supported somewhere in the
corpus. Prints the best embedding cosine and the best cross-encoder score for
each question.
"""

import argparse
from pathlib import Path

import yaml

from .rerank import rerank
from .retrieve import search
from .tenants.admin import admin_tenant_conn


def best(conn, scopes: list[str], question: str) -> tuple[float, float, str]:
    hits = search(conn, question, scopes)
    cosine = max(float(h["score"]) for h in hits)
    top = rerank(question, hits)[0]
    return cosine, top["relevance"], top["doc_title"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="brindlewood")
    tenant = ap.parse_args().tenant
    q = yaml.safe_load((Path("/seed") / tenant / "questions.yaml").read_text())
    conn = admin_tenant_conn(tenant)
    scopes = [r["id"] for r in conn.execute("SELECT id FROM scopes")]
    stats = {}
    for label, questions in [
        ("supported", q["answerable"]),
        ("unsupported", q["unsupported"]),
    ]:
        print(f"== {label}  (cosine | cross-encoder | top document)")
        rows = [(question, *best(conn, scopes, question)) for question in questions]
        for question, cos, rel, title in rows:
            print(f"  {cos:.3f} | {rel:7.2f} | {title[:40]:40} | {question}")
        stats[label] = ([r[1] for r in rows], [r[2] for r in rows])
    (sc, sr), (uc, ur) = stats["supported"], stats["unsupported"]
    print(
        f"\ncosine        supported {min(sc):.3f}..{max(sc):.3f}  unsupported {min(uc):.3f}..{max(uc):.3f}"
        f"  gap {min(sc) - max(uc):+.3f}"
    )
    print(
        f"cross-encoder supported {min(sr):.2f}..{max(sr):.2f}  unsupported {min(ur):.2f}..{max(ur):.2f}"
        f"  gap {min(sr) - max(ur):+.2f}"
    )


if __name__ == "__main__":
    main()
