"""Supersede suite. The model is non-deterministic, so this reports a measured pass
rate per case and asserts a floor; it never retries a failed run.

    docker compose run --rm -e SUPERSEDE_RUNS=10 tools pytest -m llm -s tests/test_supersede.py
"""
import os

import pytest

from app.supersede_eval import evaluate, report

RUNS = int(os.environ.get("SUPERSEDE_RUNS", "3"))
FLOOR = 0.9


@pytest.mark.llm
def test_supersede_pass_rate():
    by_case = evaluate(RUNS)
    print("\n" + report(by_case))
    rates = {cid: sum(r["pass"] for r in rs) / len(rs) for cid, rs in by_case.items()}
    assert all(rate >= FLOOR for rate in rates.values()), rates


def test_claims_are_append_only(client):
    """The trigger holds even for the superuser (grants don't apply to it; triggers do)."""
    from app.tenants.admin import admin_tenant_conn

    with admin_tenant_conn("brindlewood") as conn:
        row = conn.execute("SELECT id FROM claims LIMIT 1").fetchone()
        assert row is not None, "no claims were extracted"
        with pytest.raises(Exception, match="append-only"):
            conn.execute("UPDATE claims SET value = 'tampered' WHERE id = %s", (row["id"],))
        superseded = conn.execute(
            "SELECT id FROM claims WHERE superseded_by IS NOT NULL LIMIT 1").fetchone()
        assert superseded is not None, "no claim was superseded"
        with pytest.raises(Exception, match="already superseded"):
            conn.execute("UPDATE claims SET superseded_by = id WHERE id = %s", (superseded["id"],))
