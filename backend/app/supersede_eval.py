"""Measure the supersede pipeline (extract -> record) on fixed passage pairs.

    docker compose run --rm tools python -m app.supersede_eval --runs 10

Every run happens in its own transaction with its own scratch scope and is rolled
back, so nothing touches the real claims. Failures are counted and reported; a
failing run is never retried.
"""
import argparse
import os
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import yaml

from . import claims
from .tenants.admin import admin_tenant_conn

SEED_DIR = Path("/seed/brindlewood")
TENANT = os.environ.get("SUPERSEDE_TENANT", "brindlewood")
OLD_DATE, NEW_DATE = date(2026, 1, 1), date(2026, 3, 1)
ZERO = [0.0] * 384


def load_cases() -> list[dict]:
    return yaml.safe_load((SEED_DIR / "supersede_cases.yaml").read_text())["cases"]


def run_case(case: dict) -> dict:
    """One run of one case. Returns {'pass': bool, 'detail': str}."""
    conn = admin_tenant_conn(TENANT)
    conn.autocommit = False
    try:
        scope = f"eval-{uuid.uuid4().hex[:8]}"
        conn.execute("INSERT INTO scopes VALUES (%s, 'eval', 'eval', 'scratch', 'ada')", (scope,))
        outcomes = []
        for label, text, when in [("old", case["old"], OLD_DATE), ("new", case["new"], NEW_DATE)]:
            doc = f"{scope}-{label}"
            conn.execute("INSERT INTO documents VALUES (%s, %s, %s, 'eval', 'eval', %s, %s)",
                         (doc, scope, doc, when, text))
            chunk_id = conn.execute(
                "INSERT INTO chunks (document_id, scope_id, ord, text, embedding)"
                " VALUES (%s, %s, 0, %s, %s) RETURNING id", (doc, scope, text, ZERO),
            ).fetchone()["id"]
            extracted = claims.extract(conn, text, scope)
            if not extracted:
                outcomes.append(f"{label}: nothing extracted")
            for c in extracted:
                o = claims.record(conn, c, chunk_id, scope, when)
                outcomes.append(f"{label}: {c['subject']}|{c['attribute']}|{c['condition']}"
                                f" = {c['value']} -> {o}")

        rows = conn.execute("SELECT value, superseded_by FROM claims WHERE scope_id = %s",
                            (scope,)).fetchall()
        supersedes = sum(r["superseded_by"] is not None for r in rows)
        current = sum(r["superseded_by"] is None for r in rows)
        extracted_both = not any("nothing extracted" in o for o in outcomes)
        kind = case["kind"]
        if kind in ("identical", "reworded"):
            ok = extracted_both and supersedes == 0
        elif kind == "changed":
            ok = supersedes == 1
        else:  # conditional
            ok = extracted_both and supersedes == 0 and current >= 2
        return {"pass": ok, "detail": "; ".join(outcomes)}
    finally:
        conn.rollback()
        conn.close()


def evaluate(runs: int, workers: int = 8) -> dict[str, list[dict]]:
    cases = load_cases()
    jobs = [c for c in cases for _ in range(runs)]
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(run_case, jobs))
    by_case = defaultdict(list)
    for case, r in zip(jobs, results, strict=True):
        by_case[case["id"]].append(r)
    return by_case


def report(by_case: dict[str, list[dict]]) -> str:
    cases = {c["id"]: c for c in load_cases()}
    lines, by_kind = [], defaultdict(lambda: [0, 0])
    for cid, rs in by_case.items():
        passed = sum(r["pass"] for r in rs)
        by_kind[cases[cid]["kind"]][0] += passed
        by_kind[cases[cid]["kind"]][1] += len(rs)
        lines.append(f"{cid:22} {passed:>3}/{len(rs):<3} ({100 * passed / len(rs):5.1f}%)")
        for r in rs:
            if not r["pass"]:
                lines.append(f"    FAIL: {r['detail']}")
    lines.append("")
    for kind, (p, n) in by_kind.items():
        lines.append(f"{kind:12} {p:>3}/{n:<3} ({100 * p / n:5.1f}%)")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    print(report(evaluate(ap.parse_args().runs)))
