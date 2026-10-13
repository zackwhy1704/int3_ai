"""Operator fallback for proposal review. The PRIMARY path is the per-user MCP
tools (list_proposals/approve_proposal/reject_proposal, owner-gated + elicitation);
this CLI is for an operator with admin DB creds when an owner is unavailable. It
records decided_by honestly as 'operator:<name>'.

    python -m app.tenants.proposals list    --id-tenant T [--scope S]
    python -m app.tenants.proposals approve --id-tenant T --proposal <uuid> --operator alice [--supersede-anyway]
    python -m app.tenants.proposals reject  --id-tenant T --proposal <uuid> --operator alice
"""
import argparse
import sys

from .. import capture
from .admin import admin_tenant_conn


def _list(a) -> None:
    with admin_tenant_conn(a.id_tenant) as c:
        sql = ("SELECT id, scope_id, subject, attribute, value, proposer_user_id, created_at"
               " FROM proposals WHERE status='pending'")
        params = ()
        if a.scope:
            sql += " AND scope_id = %s"
            params = (a.scope,)
        for r in c.execute(sql + " ORDER BY created_at", params).fetchall():
            print(f"{r['id']}  [{r['scope_id']}] {r['subject']}/{r['attribute']}={r['value']}"
                  f"  by {r['proposer_user_id']}  {r['created_at'].isoformat()}")


def _approve(a) -> None:
    with admin_tenant_conn(a.id_tenant) as c:
        pr = c.execute("SELECT * FROM proposals WHERE id=%s AND status='pending'", (a.proposal,)).fetchone()
        if not pr:
            sys.exit("no such pending proposal")
        print(capture._write_workflow_record(c, pr, decided_by=f"operator:{a.operator}",
                                             supersede_anyway=a.supersede_anyway))


def _reject(a) -> None:
    with admin_tenant_conn(a.id_tenant) as c:
        n = c.execute("UPDATE proposals SET status='rejected', decided_by=%s, decided_at=now()"
                      " WHERE id=%s AND status='pending'",
                      (f"operator:{a.operator}", a.proposal)).rowcount
        sys.exit(0 if n else "no such pending proposal")


def main() -> None:
    p = argparse.ArgumentParser(prog="app.tenants.proposals")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("list", "approve", "reject"):
        s = sub.add_parser(name)
        s.add_argument("--id-tenant", dest="id_tenant", required=True)
        if name == "list":
            s.add_argument("--scope")
        else:
            s.add_argument("--proposal", required=True)
            s.add_argument("--operator", required=True)
        if name == "approve":
            s.add_argument("--supersede-anyway", dest="supersede_anyway", action="store_true")
    a = p.parse_args()
    {"list": _list, "approve": _approve, "reject": _reject}[a.cmd](a)


if __name__ == "__main__":
    main()
