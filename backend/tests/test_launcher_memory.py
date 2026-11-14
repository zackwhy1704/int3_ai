"""Local mode M2 (docs/LOCAL_MODE_DESIGN.md §3.3): each employee's assistant memory, kept on our server.
Real Postgres, run in the tools container:
  docker compose run --rm tools pytest -v backend/tests/test_launcher_memory.py

Invariants proven here:
  - the Hermes plugin saves/recalls/forgets with the BRAIN token only, under the same live gate as the
    brain (a weakened setup or a revoked device stops it); the int3 app lists/deletes with the
    launcher token only
  - a user only ever sees or deletes their own memories (same tenant, different user: nothing)
  - deletion is a hard delete; every deletion is audited with counts, never content
  - a cancelled tenant's brain tokens stop at once; its memories are purged only after the
    retention window, and the purge is audited
  - memory content never lands in brain_audit
"""

from __future__ import annotations

import pytest

from app import config
from app.db import control_conn, tenant_conn
from app.launcher import local_policy, memory
from app.tenants.admin import admin_conn

from .test_launcher import _bearer, _sign_in
from .test_launcher_local import TENANT, _brain_token, _report, _set_mode, good_report

SECRET_FACT = "My desk is 4B and I prefer the Tuesday standup notes in bullet form"


@pytest.fixture()
def local_tenant():
    _set_mode("local")
    yield
    _set_mode("hosted")


@pytest.fixture(autouse=True)
def _clean():
    yield
    with admin_conn(config.CONTROL_DB) as c:
        c.execute("UPDATE tenants SET cancelled_at = NULL WHERE id = %s", (TENANT,))
    with tenant_conn(TENANT) as t:
        t.execute("DELETE FROM user_memories")


def _save(client, token: str, content: str) -> dict:
    r = client.post("/launcher/local/memory/save", json={"content": content}, headers=_bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def _count() -> int:
    with tenant_conn(TENANT) as t:
        return t.execute("SELECT count(*) AS n FROM user_memories").fetchone()["n"]


def _memory_audit(user: str | None = None) -> list[dict]:
    with control_conn() as c:
        return c.execute(
            "SELECT user_id, action, count, actor FROM memory_audit WHERE tenant_id = %s"
            " AND (%s::text IS NULL OR user_id = %s) ORDER BY id", (TENANT, user, user)).fetchall()


def test_policy_turns_on_the_int3_memory_provider(client, local_tenant):
    access = _sign_in(client, "priya")["access_token"]
    body = client.get("/launcher/local/policy", headers=_bearer(access)).json()
    assert body["config"]["memory.provider"] == "int3"
    assert body["config"]["memory.memory_enabled"] is False  # built-in on-PC memory stays off
    assert body["memory_plugin"] == "int3" and body["memory_url"] == config.LOCAL_MEMORY_URL


def test_posture_requires_the_int3_provider_and_allows_only_its_plugin():
    good = {**good_report(), "plugins": ["int3"], "memory_provider": "int3"}
    assert local_policy.evaluate(good, config.LOCAL_MCP_URL) == (True, [])
    ok, reasons = local_policy.evaluate({**good, "memory_provider": "honcho"}, config.LOCAL_MCP_URL)
    assert not ok and any("int3 memory" in r for r in reasons)
    assert not local_policy.evaluate({**good, "memory_provider": None}, config.LOCAL_MCP_URL)[0]
    ok, reasons = local_policy.evaluate({**good, "plugins": ["int3", "evil"]}, config.LOCAL_MCP_URL)
    assert not ok and any("evil" in r for r in reasons)
    full = {**good, "memory_plugin_intact": True, "memory_url": config.LOCAL_MEMORY_URL}
    assert local_policy.evaluate(full, config.LOCAL_MCP_URL, memory_url=config.LOCAL_MEMORY_URL)[0]
    for change, reason in [({"memory_plugin_intact": False}, "plugin is missing or was changed"),
                           ({"memory_plugin_intact": None}, "plugin is missing or was changed"),
                           ({"memory_url": "https://evil.example"}, "points somewhere else")]:
        ok, reasons = local_policy.evaluate({**full, **change}, config.LOCAL_MCP_URL,
                                            memory_url=config.LOCAL_MEMORY_URL)
        assert not ok and any(reason in r for r in reasons), change


def test_save_recall_list_and_isolation(client, local_tenant):
    priya, priya_session = _brain_token(client, "priya")
    marcus, marcus_session = _brain_token(client, "marcus")
    saved = _save(client, priya, SECRET_FACT)
    _save(client, priya, "Remind me that the Lisbon offsite budget owner is Ada")

    r = client.post("/launcher/local/memory/recall", json={"query": "which desk do I sit at?"},
                    headers=_bearer(priya))
    assert r.status_code == 200
    assert r.json()["memories"][0]["id"] == saved["id"]

    # same tenant, different user: recall and list see nothing of priya's
    r = client.post("/launcher/local/memory/recall", json={"query": "which desk do I sit at?"},
                    headers=_bearer(marcus))
    assert r.json()["memories"] == []
    mine = client.get("/launcher/local/memory", headers=_bearer(priya_session["access_token"])).json()
    theirs = client.get("/launcher/local/memory", headers=_bearer(marcus_session["access_token"])).json()
    assert mine["total"] == 2 and theirs["total"] == 0
    # marcus cannot delete priya's memory, either through the plugin or the app
    r = client.post("/launcher/local/memory/forget", json={"id": saved["id"]}, headers=_bearer(marcus))
    assert r.json() == {"deleted": 0}
    r = client.post("/launcher/local/memory/delete", json={"id": saved["id"]},
                    headers=_bearer(marcus_session["access_token"]))
    assert r.status_code == 404
    assert _count() == 2


def test_token_kinds_are_not_interchangeable(client, local_tenant):
    brain, session = _brain_token(client, "priya")
    access = session["access_token"]
    # the app's launcher token cannot save/recall; the plugin's brain token cannot list/delete
    assert client.post("/launcher/local/memory/save", json={"content": "x"},
                       headers=_bearer(access)).status_code == 401
    assert client.get("/launcher/local/memory", headers=_bearer(brain)).status_code == 401
    assert client.post("/launcher/local/memory/delete", json={"all": True},
                       headers=_bearer(brain)).status_code == 401
    # memory is not reachable under /v1
    assert client.post("/v1/memory/save", json={"content": "x"},
                       headers=_bearer(brain)).status_code in (403, 404)


def test_memory_is_gated_like_the_brain(client, local_tenant):
    brain, session = _brain_token(client, "priya")
    _report(client, session["access_token"], {**good_report(), "approvals_mode": "off"})
    r = client.post("/launcher/local/memory/save", json={"content": "x"}, headers=_bearer(brain))
    assert r.status_code == 403 and "Reopen int3" in r.json()["detail"]
    client.post("/launcher/auth/revoke", headers=_bearer(session["access_token"]))
    r = client.post("/launcher/local/memory/recall", json={"query": "x"}, headers=_bearer(brain))
    assert r.status_code == 401


def test_delete_one_and_all_are_hard_and_audited_without_content(client, local_tenant):
    brain, session = _brain_token(client, "priya")
    access = session["access_token"]
    a = _save(client, brain, SECRET_FACT)
    _save(client, brain, "second")
    _save(client, brain, "third")
    r = client.post("/launcher/local/memory/forget", json={"id": a["id"]}, headers=_bearer(brain))
    assert r.json() == {"deleted": 1}
    assert client.post("/launcher/local/memory/delete", json={"id": 1, "all": True},
                       headers=_bearer(access)).status_code == 422
    one = client.get("/launcher/local/memory", headers=_bearer(access)).json()["memories"][0]["id"]
    assert client.post("/launcher/local/memory/delete", json={"id": one},
                       headers=_bearer(access)).json() == {"deleted": 1}
    assert client.post("/launcher/local/memory/delete", json={"all": True},
                       headers=_bearer(access)).json() == {"deleted": 1}
    assert client.get("/launcher/local/memory", headers=_bearer(access)).json()["total"] == 0
    assert _count() == 0
    rows = [(r["action"], r["count"], r["actor"]) for r in _memory_audit("priya")[-3:]]
    assert rows == [("delete_one", 1, "assistant"), ("delete_one", 1, "user"), ("delete_all", 1, "user")]
    with control_conn() as c:
        n = c.execute("SELECT count(*) AS n FROM brain_audit WHERE coalesce(query, '') LIKE %s",
                      ("%desk is 4B%",)).fetchone()["n"]
    assert n == 0


def test_save_rejects_empty_and_too_long(client, local_tenant):
    brain, _ = _brain_token(client, "priya")
    assert client.post("/launcher/local/memory/save", json={"content": "   "},
                       headers=_bearer(brain)).status_code == 422
    assert client.post("/launcher/local/memory/save", json={"content": "x" * (memory.MAX_CONTENT + 1)},
                       headers=_bearer(brain)).status_code == 422


def test_cancelled_tenant_stops_now_and_purges_after_retention(client, local_tenant):
    brain, _ = _brain_token(client, "priya")
    _save(client, brain, SECRET_FACT)
    with admin_conn(config.CONTROL_DB) as c:
        c.execute("UPDATE tenants SET cancelled_at = now() WHERE id = %s", (TENANT,))
    # access stops at once...
    r = client.post("/launcher/local/memory/recall", json={"query": "desk"}, headers=_bearer(brain))
    assert r.status_code == 401
    # ...but the memory is kept through the retention window
    assert TENANT not in dict(memory.purge_cancelled())
    assert _count() == 1
    with admin_conn(config.CONTROL_DB) as c:
        c.execute("UPDATE tenants SET cancelled_at = now() - interval '31 days' WHERE id = %s", (TENANT,))
    assert memory.purge_cancelled(days=90) == []  # the window is configurable
    assert dict(memory.purge_cancelled())[TENANT] == 1
    assert _count() == 0
    last = _memory_audit()[-1]
    assert (last["action"], last["count"], last["actor"]) == ("purge", 1, "retention")


def test_preferences_come_every_turn_and_unrelated_turns_get_no_facts(client, local_tenant):
    brain, session = _brain_token(client, "priya")
    pref = client.post("/launcher/local/memory/save", json={"content": "I prefer short answers.", "kind": "preference"},
                       headers=_bearer(brain)).json()
    fact = _save(client, brain, "The forklift certification renewal is due in March.")
    assert client.post("/launcher/local/memory/save", json={"content": "x", "kind": "secret"},
                       headers=_bearer(brain)).status_code == 422
    r = client.post("/launcher/local/memory/recall", json={"query": "What is the capital of Australia?"},
                    headers=_bearer(brain)).json()
    assert [m["id"] for m in r["preferences"]] == [pref["id"]]  # a preference applies to every turn
    assert r["memories"] == []                                  # an unrelated fact is not injected
    r = client.post("/launcher/local/memory/recall", json={"query": "When does the forklift licence need renewing?"},
                    headers=_bearer(brain)).json()
    assert [m["id"] for m in r["memories"]] == [fact["id"]]
    listed = client.get("/launcher/local/memory", headers=_bearer(session["access_token"])).json()["memories"]
    assert {m["kind"] for m in listed} == {"preference", "fact"}


def test_memory_has_its_own_quota(client, local_tenant, monkeypatch):
    brain, _ = _brain_token(client, "priya")
    with control_conn() as c:  # earlier tests already used some of priya's memory quota today
        used = c.execute("SELECT count(*) AS n FROM brain_audit WHERE user_id = 'priya' AND tool LIKE 'memory%%'"
                         " AND outcome IN ('ok', 'empty') AND at > date_trunc('day', now())").fetchone()["n"]
    monkeypatch.setattr(config, "MEMORY_RATE_PER_DAY", used + 2)
    for _ in range(2):
        assert client.post("/launcher/local/memory/recall", json={"query": "x"}, headers=_bearer(brain)).status_code == 200
    r = client.post("/launcher/local/memory/recall", json={"query": "x"}, headers=_bearer(brain))
    assert r.status_code == 429 and "memory" in r.json()["detail"]
    # the brain is unaffected by memory use...
    assert client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(brain)).status_code == 200
    # ...and memory is unaffected when the brain's own daily limit is used up
    monkeypatch.setattr(config, "MEMORY_RATE_PER_DAY", used + 100)
    monkeypatch.setattr(config, "BRAIN_RATE_PER_DAY", 0)
    assert client.post("/v1/search", json={"query": "holidays"}, headers=_bearer(brain)).status_code == 429
    assert client.post("/launcher/local/memory/recall", json={"query": "x"}, headers=_bearer(brain)).status_code == 200
