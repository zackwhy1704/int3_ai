"""Phase 2 capture: propose/approve/reject invariants + workflow-record retrieval.

Integration (needs the stack + fixture tenants). Owners (from the seed):
operations=priya, finance=marcus, leadership=ada. Each test uses a unique subject
tag so it is idempotent against the persistent fixture database.
"""
import uuid

import numpy as np
import pytest

from app import capture, config
from app.tenants import admin

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    # Tests reuse the same proposers; the real 30/hour cap (test_rate_limit_429
    # exercises it explicitly) would otherwise trip from accumulated proposals.
    monkeypatch.setattr(config, "PROPOSAL_RATE_PER_HOUR", 0)


def _tag():
    return uuid.uuid4().hex[:8]


def _tconn():
    return admin.admin_tenant_conn("brindlewood")


def _count(sql, params):
    with _tconn() as c:
        return c.execute(sql, params).fetchone()["n"]


def _propose(client, as_user, user, scope, subject, attr="a", value="v", quote="q"):
    return client.post("/v1/propose_claim", headers=as_user(user), json={
        "scope": scope, "subject": subject, "attribute": attr, "value": value, "quote": quote})


def test_propose_into_non_member_scope_403_and_no_row(client, as_user):
    before = _count("SELECT count(*) n FROM proposals WHERE scope_id=%s", ("leadership",))
    r = _propose(client, as_user, "marcus", "leadership", f"secret-{_tag()}")
    assert r.status_code == 403
    assert _count("SELECT count(*) n FROM proposals WHERE scope_id=%s", ("leadership",)) == before


def test_non_owner_approve_403(client, as_user):
    pid = _propose(client, as_user, "priya", "operations", f"no1-{_tag()}").json()["proposal_id"]
    r = client.post("/v1/approve_proposal", headers=as_user("marcus"), json={"proposal_id": pid})
    assert r.status_code == 403


def test_double_approve_yields_exactly_one_claim(client, as_user):
    subj = f"dedup-{_tag()}"
    pid = _propose(client, as_user, "priya", "operations", subj, quote="dedup q").json()["proposal_id"]
    assert client.post("/v1/approve_proposal", headers=as_user("priya"), json={"proposal_id": pid}).status_code == 200
    assert client.post("/v1/approve_proposal", headers=as_user("priya"), json={"proposal_id": pid}).status_code == 404
    assert _count("SELECT count(*) n FROM claims WHERE subject=%s AND scope_id='operations'", (subj,)) == 1


def test_rate_limit_429(client, as_user, monkeypatch):
    current = _count(
        "SELECT count(*) n FROM proposals WHERE proposer_user_id='ada'"
        " AND created_at > now() - interval '1 hour'", ())
    monkeypatch.setattr(config, "PROPOSAL_RATE_PER_HOUR", current + 2)
    codes = [_propose(client, as_user, "ada", "leadership", f"rl-{_tag()}").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_end_to_end_capture_then_retrieve(client, as_user):
    tag = _tag()
    q = {"question": f"When does the {tag} staff choir rehearse?"}
    # fresh: the brain has no such fact (off-topic for operations) -> refusal.
    assert client.post("/v1/brain_ask", headers=as_user("priya"), json=q).json()["refused"] is True

    body = {"scope": "operations", "subject": f"{tag} staff choir", "attribute": "rehearsal day",
            "value": "Thursdays", "quote": f"The {tag} staff choir rehearses on Thursdays."}
    pid = client.post("/v1/propose_claim", headers=as_user("priya"), json=body).json()["proposal_id"]
    client.post("/v1/approve_proposal", headers=as_user("priya"), json={"proposal_id": pid})

    out = client.post("/v1/brain_ask", headers=as_user("priya"), json=q).json()
    assert out["refused"] is False
    assert "thursday" in out["answer"].lower()
    assert any("workflow" in (c.get("doc_title", "").lower()) for c in out["citations"])


def test_neutralised_workflow_record_reverts_to_refusal(client, as_user, monkeypatch):
    """fail-without-fix: with the embedded chunk neutralised (zero vector), the
    captured claim is invisible to passage-first retrieval and the brain refuses —
    proving the workflow RECORD (not a chunk-less claim) is what makes it answerable."""
    monkeypatch.setattr(capture, "embed_passages", lambda texts: [np.zeros(384, dtype=np.float32) for _ in texts])
    tag = _tag()
    q = {"question": f"Where is the {tag} evacuation assembly point?"}
    assert client.post("/v1/brain_ask", headers=as_user("priya"), json=q).json()["refused"] is True
    body = {"scope": "operations", "subject": f"{tag} evacuation", "attribute": "assembly point",
            "value": "the north car park", "quote": f"The {tag} evacuation assembly point is the north car park."}
    pid = client.post("/v1/propose_claim", headers=as_user("priya"), json=body).json()["proposal_id"]
    client.post("/v1/approve_proposal", headers=as_user("priya"), json={"proposal_id": pid})
    # the claim exists but its chunk is unretrievable -> still refused.
    assert client.post("/v1/brain_ask", headers=as_user("priya"), json=q).json()["refused"] is True
    assert _count("SELECT count(*) n FROM claims WHERE subject=%s AND scope_id='operations'", (f"{tag} evacuation",)) == 1


def test_cross_scope_workflow_claim_not_leaked(client, as_user):
    """A non-member of operations cannot retrieve a captured operations claim."""
    tag = _tag()
    body = {"scope": "operations", "subject": f"{tag} drill", "attribute": "frequency",
            "value": "quarterly", "quote": f"The {tag} drill is held once a quarter."}
    pid = client.post("/v1/propose_claim", headers=as_user("priya"), json=body).json()["proposal_id"]
    client.post("/v1/approve_proposal", headers=as_user("priya"), json={"proposal_id": pid})
    out = client.post("/v1/brain_ask", headers=as_user("marcus"),
                      json={"question": f"How often is the {tag} drill held?"}).json()
    cits = out.get("citations") or []
    assert all(c.get("scope") != "operations" for c in cits)  # marcus (finance) sees no operations citation
