"""Local mode M1 (docs/LOCAL_MODE_DESIGN.md): the Hermes-setup report, the brain token, and what that
token can reach. Real Postgres, run in the tools container:
  docker compose run --rm tools pytest -v backend/tests/test_launcher_local.py

Invariants proven here:
  - local endpoints exist only for local-mode tenants; the policy holds no secret
  - the setup verdict is the server's: every weakening the launcher can see is a refusal reason
  - a brain token is minted only after a fresh, good report
  - a brain token reaches the passages-only surface and nothing else (no server-written answers)
  - every brain call is checked live: revoke the device, or report a weakened setup, and the
    next call is refused; every call (including refusals) is audited
  - per-user minute limit; citations validate only against this user's own retrieval
  - brain, launcher and service tokens never stand in for one another
"""

from __future__ import annotations

import pytest

from app import config
from app.db import control_conn
from app.launcher import local_policy, tokens
from app.tenants.admin import admin_conn

from .test_launcher import _bearer, _sign_in

TENANT = "brindlewood"


def _set_mode(mode: str) -> None:
    with admin_conn(config.CONTROL_DB) as c:
        c.execute("UPDATE tenants SET launch_mode = %s WHERE id = %s", (mode, TENANT))


@pytest.fixture()
def local_tenant():
    _set_mode("local")
    yield
    _set_mode("hosted")


def good_report() -> dict:
    return {
        "disabled_toolsets": list(local_policy.REQUIRED_DISABLED_TOOLSETS),
        "approvals_mode": "manual",
        "cron_mode": "deny",
        "memory_enabled": False,
        "user_profile_enabled": False,
        "mcp_servers": [local_policy.MCP_SERVER_NAME],
        "mcp_url": config.LOCAL_MCP_URL,
        "plugins": [],
        "write_safe_root": r"C:\Users\priya\int3-workspace",
        "workspace_synced": False,
        "telemetry_enabled": False,
        "inline_shell": False,
        "tool_search": "off",
        "mcp_resources": False,
        "mcp_prompts": False,
        "adopt_external_logins": False,
        "model_provider": "anthropic",
        "credential_sources": ["api_key"],
    }


def _report(client, access: str, report: dict) -> dict:
    r = client.post("/launcher/local/posture", json={"hermes_version": "dev", "report": report},
                    headers=_bearer(access))
    assert r.status_code == 200, r.text
    return r.json()


def _brain_token(client, user: str = "priya") -> tuple[str, dict]:
    session = _sign_in(client, user)
    access = session["access_token"]
    assert _report(client, access, good_report())["ok"]
    r = client.post("/launcher/local/launch", headers=_bearer(access))
    assert r.status_code == 200, r.text
    return r.json()["brain_token"], session


def _audit(device_id: str) -> list[dict]:
    with control_conn() as c:
        return c.execute(
            "SELECT tool, outcome, query, claim_ids, invalid_ids FROM brain_audit"
            " WHERE device_id = %s ORDER BY id", (device_id,)).fetchall()


# ── policy and report ───────────────────────────────────────────────────────


def test_local_endpoints_are_404_for_hosted_tenants(client):
    access = _sign_in(client, "priya")["access_token"]
    assert client.get("/launcher/local/policy", headers=_bearer(access)).status_code == 404
    assert client.post("/launcher/local/launch", headers=_bearer(access)).status_code == 404
    assert client.get("/launcher/me", headers=_bearer(access)).json()["tenant"]["launch_mode"] == "hosted"


def test_policy_carries_the_lockdown_and_no_secret(client, local_tenant):
    access = _sign_in(client, "priya")["access_token"]
    r = client.get("/launcher/local/policy", headers=_bearer(access))
    assert r.status_code == 200
    body = r.json()
    cfg = body["config"]
    assert set(cfg["agent.disabled_toolsets"]) >= {"terminal", "code_execution", "browser", "web"}
    assert "memory" not in cfg["agent.disabled_toolsets"]  # would hide the memory provider's tools
    assert cfg["memory.memory_enabled"] is False and cfg["approvals.mode"] == "manual"
    assert cfg["tools.tool_search.enabled"] == "off"
    entry = cfg[f"mcp_servers.{local_policy.MCP_SERVER_NAME}"]
    assert entry["url"] == config.LOCAL_MCP_URL
    assert entry["headers"]["Authorization"] == "Bearer ${INT3_BRAIN_TOKEN}"
    assert entry["tools"]["resources"] is False and entry["tools"]["prompts"] is False
    assert entry["timeout"] == 135  # MCP endpoint's 90 s approval window + 2 x 15 s brain calls + 15 s margin
    assert "cb_" not in r.text  # no token of any kind in the policy
    assert body["soul"]["block"] == local_policy.SOUL_BLOCK


def test_soul_block_routes_company_facts_to_propose_and_personal_ones_to_memory():
    """Live turn (b) at 10e1083: "Remember for operations that ..." went to int3_remember because
    nothing told the model otherwise. The launcher writes this block into the user's SOUL.md."""
    block = local_policy.SOUL_BLOCK
    for needle in ('"Remember for <a team> / for the company', "statement about the business",
                   "call `propose_claim`", "NEVER save these with `int3_remember`",
                   '"Remember that I', "call `int3_remember`", "ask them",
                   "before saving anything"):
        assert needle in block, needle
    # company before personal, so the narrower rule is read first
    assert block.index("propose_claim") < block.index("call `int3_remember`")
    # the launcher appends "## Your int3 scopes" (launcher/src-tauri/src/local.rs scopes_section)
    assert 'scope ids listed under "Your int3 scopes"' in block and "never invent one" in block
    assert "Never state the status of a proposal you could not see" in block
    assert "say you\n  can't check it" in block


@pytest.mark.parametrize("change, reason", [
    ({"disabled_toolsets": ["code_execution", "browser"]}, "tools switched back on"),
    ({"approvals_mode": "off"}, "approvals mode"),
    ({"cron_mode": "approve"}, "scheduled jobs"),
    ({"memory_enabled": True}, "own memory"),
    ({"mcp_servers": [local_policy.MCP_SERVER_NAME, "github"]}, "other MCP servers"),
    ({"mcp_servers": []}, "brain entry is missing"),
    ({"mcp_url": "https://evil.example/mcp"}, "points somewhere else"),
    ({"plugins": ["some-plugin"]}, "extra Hermes plugins"),
    ({"write_safe_root": ""}, "not confined"),
    ({"workspace_synced": True}, "synced folder"),
    ({"workspace_synced": None}, "synced folder"),
    ({"write_safe_root": r"C:\Users\priya\OneDrive - Acme\int3-workspace"}, "synced folder"),
    ({"write_safe_root": r"D:\Dropbox\int3-workspace"}, "synced folder"),
    ({"telemetry_enabled": True}, "usage data with Nous"),
    ({"telemetry_enabled": None}, "usage data with Nous"),
    ({"inline_shell": True}, "skills may run commands"),
    ({"tool_search": "auto"}, "tool lookup is switched back on"),
    ({"tool_search": None}, "tool lookup is switched back on"),
    ({"mcp_resources": True}, "MCP resources or prompts switched back on"),
    ({"mcp_prompts": True}, "MCP resources or prompts switched back on"),
    ({"mcp_prompts": None}, "MCP resources or prompts switched back on"),
    ({"adopt_external_logins": True}, "personal Claude Code or ChatGPT login"),
    ({"credential_sources": ["personal_login"]}, "personal Claude Code or ChatGPT login"),
    ({"credential_sources": ["custom_endpoint"]}, "model server elsewhere"),
    ({"credential_sources": ["something-new"]}, "unknown credential"),
    ({"credential_sources": None}, "credentials not reported"),
])
def test_every_weakening_is_a_reason(change, reason):
    ok, reasons = local_policy.evaluate({**good_report(), **change}, config.LOCAL_MCP_URL)
    assert not ok
    assert any(reason in r for r in reasons), reasons


def test_good_report_passes_and_empty_report_fails():
    assert local_policy.evaluate(good_report(), config.LOCAL_MCP_URL) == (True, [])
    # no model chosen yet is fine: there is nothing to refuse
    assert local_policy.evaluate({**good_report(), "credential_sources": []}, config.LOCAL_MCP_URL)[0]
    ok, reasons = local_policy.evaluate({}, config.LOCAL_MCP_URL)
    assert not ok and len(reasons) >= 6


def test_launch_needs_a_good_fresh_report(client, local_tenant):
    access = _sign_in(client, "priya")["access_token"]
    assert client.post("/launcher/local/launch", headers=_bearer(access)).status_code == 409
    bad = {**good_report(), "approvals_mode": "off"}
    assert not _report(client, access, bad)["ok"]
    r = client.post("/launcher/local/launch", headers=_bearer(access))
    assert r.status_code == 409 and "approvals mode" in r.json()["detail"]
    assert _report(client, access, good_report())["ok"]
    r = client.post("/launcher/local/launch", headers=_bearer(access))
    assert r.status_code == 200
    assert r.json()["brain_token"].startswith(tokens.BRAIN_PREFIX)
    assert r.json()["mcp_url"] == config.LOCAL_MCP_URL


# ── what a brain token reaches ──────────────────────────────────────────────


def test_brain_token_searches_and_is_audited(client, local_tenant):
    token, session = _brain_token(client)
    r = client.post("/v1/search", json={"query": "How many days of annual leave do staff get?"},
                    headers=_bearer(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["retrieval_id"] and body["results"]
    rows = _audit(session["device_id"])
    assert rows[-1]["tool"] == "search_brain" and rows[-1]["outcome"] == "ok"
    assert "annual leave" in rows[-1]["query"]
    assert set(rows[-1]["claim_ids"]) == {res["claimId"] for res in body["results"]}


def test_brain_token_cannot_get_server_written_answers_or_other_routes(client, local_tenant):
    token, session = _brain_token(client)
    r = client.post("/v1/brain_ask", json={"question": "When are invoices payable?"}, headers=_bearer(token))
    assert r.status_code == 403
    assert client.get("/v1/sources", headers=_bearer(token)).status_code == 403
    assert [a["outcome"] for a in _audit(session["device_id"])] == ["refused", "refused"]


def test_validate_checks_ids_against_this_users_retrieval(client, local_tenant):
    token, session = _brain_token(client)
    body = client.post("/v1/search", json={"query": "How many days of annual leave do staff get?"},
                       headers=_bearer(token)).json()
    real = body["results"][0]["claimId"]
    r = client.post("/v1/validate", json={"retrieval_id": body["retrieval_id"], "claim_ids": [real, 999999]},
                    headers=_bearer(token))
    assert r.json() == {"valid": [real], "invalid": [999999]}
    last = _audit(session["device_id"])[-1]
    assert last["tool"] == "validate_citations" and last["invalid_ids"] == [999999]
    # another user's token cannot validate against priya's retrieval
    other, _ = _brain_token(client, "ada")
    r = client.post("/v1/validate", json={"retrieval_id": body["retrieval_id"], "claim_ids": [real]},
                    headers=_bearer(other))
    assert r.json() == {"valid": [], "invalid": [real]}


def test_mcp_session_open_names_the_user_opaquely_and_only_for_a_live_token(client, local_tenant):
    """The brain MCP endpoint asks this before it opens a session (deploy/hermes/brain-mcp/local_server.py)."""
    token, session = _brain_token(client)
    r = client.post("/v1/session", headers=_bearer(token))
    assert r.status_code == 200, r.text
    key = r.json()["user_key"]
    assert len(key) == 32 and "priya" not in key and TENANT not in key
    assert _audit(session["device_id"])[-1]["tool"] == "mcp_session"
    # the same user's next token (a relaunch on the same PC) gets the same key, and so does their
    # second PC; another user gets a different one
    r = client.post("/launcher/local/launch", headers=_bearer(session["access_token"]))
    again = r.json()["brain_token"]
    assert client.post("/v1/session", headers=_bearer(again)).json()["user_key"] == key
    second_pc, _ = _brain_token(client)
    assert client.post("/v1/session", headers=_bearer(second_pc)).json()["user_key"] == key
    other, _ = _brain_token(client, "ada")
    assert client.post("/v1/session", headers=_bearer(other)).json()["user_key"] != key
    # the relaunch revoked the first token: it can no longer open a session
    assert client.post("/v1/session", headers=_bearer(token)).status_code == 401
    # a sign-in token is not a brain token
    assert client.post("/v1/session", headers=_bearer(session["access_token"])).status_code in (401, 403)


def test_revoking_the_device_stops_the_next_call(client, local_tenant):
    token, session = _brain_token(client)
    assert client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(token)).status_code == 200
    assert client.post("/launcher/auth/revoke", headers=_bearer(session["access_token"])).status_code == 200
    r = client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(token))
    assert r.status_code == 401 and "Reopen int3" in r.json()["detail"]


def test_a_weakened_report_stops_the_next_call(client, local_tenant):
    token, session = _brain_token(client)
    assert client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(token)).status_code == 200
    _report(client, session["access_token"], {**good_report(), "disabled_toolsets": []})
    r = client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(token))
    assert r.status_code == 403 and "tools switched back on" in r.json()["detail"]
    _report(client, session["access_token"], good_report())
    assert client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(token)).status_code == 200


def test_switching_the_tenant_back_to_hosted_kills_brain_tokens(client, local_tenant):
    token, _ = _brain_token(client)
    _set_mode("hosted")
    assert client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(token)).status_code == 401


def test_per_minute_limit(client, local_tenant, monkeypatch):
    from app.launcher import brain_auth

    monkeypatch.setattr(config, "BRAIN_RATE_PER_MINUTE", 2)
    brain_auth._minute.clear()
    token, session = _brain_token(client, "ada")
    codes = [client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(token)).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]
    assert _audit(session["device_id"])[-1]["outcome"] == "rate_limited"
    brain_auth._minute.clear()


def test_token_kinds_do_not_stand_in_for_each_other(client, local_tenant):
    token, session = _brain_token(client)
    # a brain token is not a launcher access token
    assert client.get("/launcher/me", headers=_bearer(token)).status_code == 401
    # a launcher access token is not a brain token
    assert client.post("/v1/search", json={"query": "holidays"},
                       headers=_bearer(session["access_token"])).status_code == 401
    # a minted-but-garbled brain token is refused
    assert client.post("/v1/search", json={"query": "holidays"},
                       headers=_bearer(token[:-2] + "xx")).status_code == 401


def test_a_tenant_can_allow_personal_logins():
    allowed = list(local_policy.DEFAULT_ALLOWED_SOURCES) + ["personal_login"]
    report = {**good_report(), "adopt_external_logins": True, "credential_sources": ["personal_login"]}
    assert local_policy.evaluate(report, config.LOCAL_MCP_URL, allowed) == (True, [])
    assert local_policy.policy(config.LOCAL_MCP_URL, allowed)["config"]["auth.adopt_external_logins"] is True


def test_default_policy_pre_answers_telemetry_and_refuses_personal_logins(client, local_tenant):
    access = _sign_in(client, "priya")["access_token"]
    body = client.get("/launcher/local/policy", headers=_bearer(access)).json()
    cfg = body["config"]
    assert cfg["telemetry.shared_metrics.enabled"] is False and cfg["telemetry.shared_metrics.send"] is False
    assert cfg["telemetry.shared_metrics.offer_version"] == local_policy.SHARED_METRICS_OFFER_VERSION
    assert cfg["auth.adopt_external_logins"] is False and cfg["skills.inline_shell"] is False
    assert body["allowed_credential_sources"] == ["api_key", "local_endpoint", "hermes_oauth"]


def test_a_disallowed_model_credential_stops_the_brain_with_a_clear_message(client, local_tenant):
    token, session = _brain_token(client)
    _report(client, session["access_token"], {**good_report(), "credential_sources": ["personal_login"]})
    r = client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(token))
    assert r.status_code == 403
    assert "personal Claude Code or ChatGPT login" in r.json()["detail"]
    assert "Choose AI model" in r.json()["detail"]



# ── scopes, search text, audit outcome (local mode, brain tokens) ─────────────


def _rows_since(device_id: str, after_id: int) -> list[dict]:
    with control_conn() as c:
        return c.execute(
            "SELECT id, tool, outcome, query FROM brain_audit WHERE device_id = %s AND id > %s ORDER BY id",
            (device_id, after_id)).fetchall()


def _last_id() -> int:
    with control_conn() as c:
        return c.execute("SELECT coalesce(max(id), 0) AS n FROM brain_audit").fetchone()["n"]


def test_me_lists_only_own_scopes_with_kind(client, local_tenant):
    """The launcher writes these into the SOUL block (local.rs scopes_section)."""
    access = _sign_in(client, "priya")["access_token"]
    brains = client.get("/launcher/me", headers=_bearer(access)).json()["brains"]
    assert brains == [{"id": "company-wide", "name": "Company-wide", "kind": "everyone"},
                      {"id": "operations", "name": "Operations", "kind": brains[1]["kind"]}]
    assert brains[1]["kind"] != "everyone"
    assert "finance" not in str(brains) and "leadership" not in str(brains)


def test_unknown_scope_is_distinct_from_not_a_member_and_lists_own_scopes(client, local_tenant):
    token, _ = _brain_token(client)
    body = {"subject": "Leeds dock", "attribute": "closing time", "value": "6pm", "quote": "q"}
    mine = "your scopes are: company-wide (Company-wide), operations (Operations)"
    r = client.post("/v1/propose_claim", json={**body, "scope": "company"}, headers=_bearer(token))
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == f"unknown scope 'company'; {mine}"
    r = client.post("/v1/propose_claim", json={**body, "scope": "finance"}, headers=_bearer(token))
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == f"not a member of scope 'finance'; {mine}"
    r = client.get("/v1/proposals", params={"scope": "company"}, headers=_bearer(token))
    assert r.status_code == 404 and r.json()["detail"] == f"unknown scope 'company'; {mine}"
    # a real scope priya belongs to but does not own: still the owner gate
    r = client.get("/v1/proposals", params={"scope": "operations"}, headers=_bearer(token))
    assert r.status_code == 403 and "only the scope owner" in r.json()["detail"]


def test_search_text_names_subject_and_attribute_for_every_claim(client, local_tenant):
    from app.api.v1 import claim_text
    token, _ = _brain_token(client)
    body = client.post("/v1/search", json={"query": "How many days of annual leave do staff get?"},
                       headers=_bearer(token)).json()
    assert body["results"]
    with admin_conn("t_" + TENANT) as c:
        for res in body["results"]:
            cl = c.execute("SELECT subject, attribute, condition, value FROM claims WHERE id = %s",
                           (res["claimId"],)).fetchone()
            assert res["content"] == claim_text(cl["subject"], cl["attribute"], cl["condition"], cl["value"])
            assert res["content"].startswith(f"{cl['subject']} — {cl['attribute']}")
    assert claim_text("Leeds loading dock", "closing time", "From December", "Closes at 6pm on Fridays") \
        == "Leeds loading dock — closing time (From December): Closes at 6pm on Fridays"
    assert claim_text("X", "y", None, "z") == "X — y: z" and claim_text("X", "y", "  ", "z") == "X — y: z"


def test_workflow_claim_search_text_names_subject_and_condition(client, local_tenant, monkeypatch):
    import uuid
    monkeypatch.setattr(config, "PROPOSAL_RATE_PER_HOUR", 0)  # not a rate-limit test; earlier tests propose too
    tag = uuid.uuid4().hex[:6]
    priya, _ = _brain_token(client)
    ada, _ = _brain_token(client, "ada")
    subj = f"Leeds loading dock {tag}"
    r = client.post("/v1/propose_claim", headers=_bearer(priya), json={
        "scope": "operations", "subject": subj, "attribute": "closing time", "value": "Closes at 6pm on Fridays",
        "condition": "From December", "quote": f"The {subj} closes at 6pm on Fridays from December."})
    assert r.status_code == 200, r.text
    r = client.post("/v1/approve_proposal", headers=_bearer(ada), json={"proposal_id": r.json()["proposal_id"]})
    assert r.status_code == 200, r.text
    claim_id = r.json()["claim_id"]
    res = client.post("/v1/search", json={"query": f"When does the {subj} close on Fridays?"},
                      headers=_bearer(priya)).json()["results"]
    hit = next(x for x in res if x["claimId"] == claim_id)
    assert hit["content"] == f"{subj} — closing time (From December): Closes at 6pm on Fridays"
    assert "workflow" in hit["sourceTitle"]


def test_audit_records_the_real_outcome_after_the_handler(client, local_tenant):
    """At 4c25c11 every /v1 call was logged before the handler ran, so priya's refused
    list_proposals (403) appeared as ok. One row per call, with the outcome that happened."""
    token, session = _brain_token(client)
    dev, start = session["device_id"], _last_id()
    r = client.get("/v1/proposals", params={"scope": "operations"}, headers=_bearer(token))
    assert r.status_code == 403
    r = client.post("/v1/propose_claim", headers=_bearer(token), json={
        "scope": "company", "subject": "s", "attribute": "a", "value": "v", "quote": "q"})
    assert r.status_code == 404
    r = client.post("/v1/search", json={"query": "How many days of annual leave do staff get?"},
                    headers=_bearer(token))
    assert r.status_code == 200
    rows = _rows_since(dev, start)
    assert [(x["tool"], x["outcome"]) for x in rows] == [
        ("list_proposals", "refused"), ("propose_claim", "not_found"), ("search_brain", "ok")]
    assert rows[0]["query"] == "operations"  # what was asked is kept on a refused row too


def test_audit_outcome_mapping():
    from fastapi import HTTPException

    from app.launcher.brain_auth import outcome_of
    assert [outcome_of(HTTPException(c)) for c in (401, 403, 404, 429, 409, 422, 500)] == \
        ["refused", "refused", "not_found", "rate_limited", "error", "error", "error"]
    assert outcome_of(RuntimeError("boom")) == "error"
