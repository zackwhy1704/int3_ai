"""In-container half of ./preflight.sh. Prints PASS/FAIL per check; exits 1 on any FAIL.

Every question here goes to the live model (a cached answer counts as FAIL), and
each successful answer refreshes the cache the demo falls back on.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx
import yaml

from . import llm
from .db import connect
from .seed import SEED_DIR

API = "http://localhost:8000"
failed = False


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def ask(user: str, question: str) -> dict:
    r = httpx.post(f"{API}/api/ask", json={"question": question},
                   headers={"X-User-Id": user}, timeout=90)
    return r.json() if r.status_code == 200 else {"http_status": r.status_code, "body": r.text}


def main() -> None:
    with connect() as conn:
        n_files = len(list((SEED_DIR / "docs").glob("*.md")))
        n_docs = conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]
        n_chunks = conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"]
        check("database seeded", n_docs == n_files and n_chunks > 0,
              f"{n_docs}/{n_files} documents, {n_chunks} chunks")

        n_expected = len(json.loads((SEED_DIR / "claims.json").read_text()))
        n_claims = conn.execute("SELECT count(*) AS n FROM claims").fetchone()["n"]
        chain = conn.execute(
            "SELECT old.value AS old, new.value AS new FROM claims old"
            " JOIN claims new ON new.id = old.superseded_by"
            " WHERE old.attribute = 'refund window' AND old.scope_id = 'company-wide'"
        ).fetchone()
        check("claims present", n_claims == n_expected and chain is not None,
              f"{n_claims}/{n_expected} claims; refund chain: "
              f"{chain['old'] + ' -> ' + chain['new'] if chain else 'MISSING'}")

    try:
        out = llm.structured("Reply with ok=true.", "Ping.", {
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False})
        check("API key valid (live round-trip)", out.get("ok") is True, f"model {llm.MODEL}")
    except llm.Unavailable as e:
        check("API key valid (live round-trip)", False, str(e)[:160])

    demo = yaml.safe_load((SEED_DIR / "demo.yaml").read_text())
    refund, unsupported = demo["scripted"]["refund"], demo["scripted"]["unsupported"]

    p = ask("priya", refund)
    change = [f for f in p.get("facts", []) if f.get("previous")]
    check("Priya refund: live answer, 30 days, change from 14 on 4 March, no Finance",
          p.get("refused") is False and p.get("cached") is False
          and any(f["current"]["valid_from"] == "2026-03-04"
                  and f["previous"]["valid_from"] == "2026-01-12" for f in change)
          and "45" not in p.get("answer", "")
          and all(c["scope"] != "finance" for c in p.get("citations", [])),
          p.get("answer", json.dumps(p))[:120])

    m = ask("marcus", refund)
    check("Marcus refund: live answer with the 45-day annual-contract condition",
          m.get("refused") is False and m.get("cached") is False
          and any(f.get("condition") == "annual contract" and "45" in f["current"]["value"]
                  for f in m.get("facts", [])),
          m.get("answer", json.dumps(m))[:120])

    u = ask("priya", unsupported)
    check("Unsupported question refuses with a suggested owner",
          u.get("refused") is True and bool(u.get("suggested_owner")),
          f"suggested owner: {u.get('suggested_owner')}")

    jobs = [(user, q) for user, qs in demo["try_asking"].items() for q in qs]
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda j: ask(*j), jobs))
    dead = [f"{user}: {q}" for (user, q), r in zip(jobs, results)
            if r.get("refused") is not False or r.get("cached") is not False]
    check(f"'Try asking' questions answer live ({len(jobs)} asked)", not dead,
          "; ".join(dead) or "all answered")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
