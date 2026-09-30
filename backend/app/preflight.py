"""In-container half of ./preflight.sh (local development stack only until gate 9).

Signs in through the test identity provider, so it exercises the real session path,
then checks the demo tenant's scripted questions against the live model. A cached
answer counts as FAIL. Prints PASS/FAIL per check; exits 1 on any FAIL.
"""
import json
import sys

import httpx

from . import llm
from .devtools import mock_login
from .tenants.admin import admin_tenant_conn

API = "http://backend:8000"
TENANT = "brindlewood"
REFUND = "What's our refund window for enterprise customers?"
UNSUPPORTED = "What is our parental leave policy?"
failed = False


def check(name: str, ok: bool, detail: str = "") -> None:
    global failed
    failed |= not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def main() -> None:
    client = httpx.Client(base_url=API, timeout=90)

    with admin_tenant_conn(TENANT) as conn:
        n_docs = conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"]
        n_claims = conn.execute("SELECT count(*) AS n FROM claims").fetchone()["n"]
        chain = conn.execute(
            "SELECT old.value AS old, new.value AS new FROM claims old"
            " JOIN claims new ON new.id = old.superseded_by"
            " WHERE old.attribute = 'refund window' AND old.scope_id = 'company-wide'").fetchone()
    check("tenant database seeded", n_docs > 0, f"{n_docs} documents")
    check("claims present", n_claims > 0 and chain is not None,
          f"{n_claims} claims; refund chain: {chain['old'] + ' -> ' + chain['new'] if chain else 'MISSING'}")

    sessions = {}
    for user in ("priya", "marcus"):
        email = f"{user}@brindlewood.example"
        try:
            sessions[user] = mock_login.login(client, "mock-google", f"google-{user}",
                                              mock_login.google_claims(email, "brindlewood.example"))
            check(f"sign-in round-trip ({user}, test provider)",
                  sessions[user]["session"]["user"]["email"] == email)
        except mock_login.LoginFailed as e:
            check(f"sign-in round-trip ({user}, test provider)", False, str(e))

    try:
        out = llm.structured("Reply with ok=true.", "Ping.", {
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False})
        check("model API live round-trip", out.get("ok") is True, f"model {llm.MODEL}")
    except llm.Unavailable as e:
        check("model API live round-trip", False, str(e)[:160])

    def ask(user: str, question: str) -> dict:
        s = sessions.get(user)
        if not s:
            return {}
        r = client.post("/api/ask", json={"question": question}, headers={
            "Cookie": s["cookie"], "X-CSRF-Token": s["csrf"], "Origin": "http://localhost:5173"})
        return r.json() if r.status_code == 200 else {"http_status": r.status_code}

    p = ask("priya", REFUND)
    change = [f for f in p.get("facts", []) if f.get("previous")]
    check("Priya refund: live, change 14 -> 30 on 4 March, nothing from Finance",
          p.get("refused") is False and p.get("cached") is False
          and any(f["current"]["valid_from"] == "2026-03-04" for f in change)
          and "45" not in p.get("answer", "")
          and all(c["scope"] != "finance" for c in p.get("citations", [])),
          p.get("answer", json.dumps(p))[:100])
    m = ask("marcus", REFUND)
    check("Marcus refund: live, with the 45-day annual-contract condition",
          m.get("refused") is False and m.get("cached") is False
          and any(f.get("condition") == "annual contract" for f in m.get("facts", [])),
          m.get("answer", json.dumps(m))[:100])
    u = ask("priya", UNSUPPORTED)
    check("unsupported question refuses with a suggested owner",
          u.get("refused") is True and bool(u.get("suggested_owner")),
          f"suggested owner: {u.get('suggested_owner')}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
