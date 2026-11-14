"""Tests for the local-mode brain MCP endpoint's sessions (local_server.py), against a fake brain and a
real MCP client. Run inside the brain-mcp image:

  docker run --rm -v "$PWD/deploy/hermes/brain-mcp:/t:ro" -w /t --entrypoint sh hermes-deploy-brain-mcp \
    -c "pip install -q pytest==8.4.2 && python -m pytest -q -p no:cacheprovider test_local_server.py"
"""

import asyncio
import json
import os
import threading
import time

import httpx
import pytest
import uvicorn

BRAIN_PORT, MCP_PORT = 18901, 18902
os.environ.update({
    "BRAIN_URL": f"http://127.0.0.1:{BRAIN_PORT}",
    "MCP_APPROVAL_SECONDS": "6",
    "MCP_SESSION_IDLE_SECONDS": "3",
    "MCP_SESSIONS_PER_USER": "2",
})

import local_server  # noqa: E402  (reads the environment above at import)
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402
from mcp.types import ElicitResult  # noqa: E402

URL = f"http://127.0.0.1:{MCP_PORT}/mcp"
USERS = {"cb_bt_alice1": "alice", "cb_bt_alice2": "alice", "cb_bt_alice3": "alice", "cb_bt_bob": "bob"}
CALLS: list[tuple[str, str]] = []  # (path, token) every request the fake brain saw


async def fake_brain(scope, receive, send):
    if scope["type"] != "http":
        return
    more = True
    while more:
        more = (await receive()).get("more_body", False)
    auth = dict(scope["headers"]).get(b"authorization", b"").decode()
    token = auth.partition(" ")[2]
    path = scope["path"]
    CALLS.append((path, token))
    status, out = 200, {}
    if token not in USERS:
        status, out = 401, {"detail": "expired"}
    elif path == "/v1/session":
        out = {"user_key": USERS[token]}
    elif path == "/v1/proposal_preview":
        out = {"statement": "Leeds dock - Friday close: 6pm"}
    elif path == "/v1/propose_claim":
        out = {"status": "pending", "proposal_id": "p1"}
    body = json.dumps(out).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


def _wait_port(port):
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/", timeout=0.5)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} never opened")


@pytest.fixture(scope="module")
def server():
    brain = uvicorn.Server(uvicorn.Config(fake_brain, host="127.0.0.1", port=BRAIN_PORT, log_level="warning"))
    threading.Thread(target=brain.run, daemon=True).start()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_until_complete, args=(local_server.serve(MCP_PORT),), daemon=True).start()
    _wait_port(BRAIN_PORT)
    _wait_port(MCP_PORT)
    yield loop


def _h(token, sid=None):
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": "2025-06-18"}
    if sid:
        h["Mcp-Session-Id"] = sid
    return h


def open_raw(token) -> str:
    r = httpx.post(URL, headers=_h(token), json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {"elicitation": {}},
                   "clientInfo": {"name": "test", "version": "0"}}})
    assert r.status_code == 200, r.text
    sid = r.headers["mcp-session-id"]
    r = httpx.post(URL, headers=_h(token, sid), json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202, r.text
    return sid


def tools_list(token, sid) -> int:
    return httpx.post(URL, headers=_h(token, sid), json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).status_code


def _text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


async def _propose(token, on_card):
    """Call propose_claim in a real client session; on_card(request_id, session_id) answers the card."""
    async with streamablehttp_client(URL, headers={"Authorization": f"Bearer {token}"}) as (r, w, get_sid):
        async def elicit(context, params):
            return await on_card(context.request_id, get_sid())
        async with ClientSession(r, w, elicitation_callback=elicit) as s:
            await s.initialize()
            return await s.call_tool("propose_claim", {
                "scope": "operations", "subject": "Leeds dock", "attribute": "Friday close",
                "value": "6pm", "quote": "the Leeds dock closes at 6pm on Fridays"})


def _proposed() -> int:
    return sum(1 for p, _ in CALLS if p == "/v1/propose_claim")


def test_accepted_card_reaches_the_brain(server):
    """The bug at 10e1083: the accept arrived in a request the stateless server could not route."""
    async def accept(_rid, _sid):
        return ElicitResult(action="accept", content={})
    before = _proposed()
    res = asyncio.run(_propose("cb_bt_alice1", accept))
    assert '"pending"' in _text(res)
    assert _proposed() == before + 1
    assert CALLS[-1] == ("/v1/propose_claim", "cb_bt_alice1")  # the caller's own token, forwarded


def test_a_confirmation_sent_in_another_session_is_refused(server):
    before = _proposed()
    other = open_raw("cb_bt_alice1")  # same user, same token, different session

    async def answer_elsewhere_then_decline(rid, sid):
        def forge():
            body = {"jsonrpc": "2.0", "id": rid, "result": {"action": "accept", "content": {}}}
            r1 = httpx.post(URL, headers=_h("cb_bt_alice1", other), json=body)  # wrong session
            r2 = httpx.post(URL, headers=_h("cb_bt_bob", sid), json=body)      # right session, wrong token
            return r1.status_code, r2.status_code
        s1, s2 = await asyncio.to_thread(forge)
        assert s2 == 404
        await asyncio.sleep(1.0)  # had the forged accept counted, propose_claim would have run by now
        assert _proposed() == before
        return ElicitResult(action="decline")

    res = asyncio.run(_propose("cb_bt_alice1", answer_elsewhere_then_decline))
    assert "not_confirmed" in _text(res)
    assert _proposed() == before


def test_a_session_id_under_another_token_answers_404(server):
    sid = open_raw("cb_bt_alice1")
    assert tools_list("cb_bt_alice1", sid) == 200
    assert tools_list("cb_bt_bob", sid) == 404
    assert tools_list("cb_bt_alice2", sid) == 404  # even the same user's other token
    assert tools_list("cb_bt_alice1", sid) == 200  # and the owner is unaffected


def test_a_dead_or_missing_token_opens_no_session(server):
    for token in ("cb_bt_dead", "not-a-brain-token"):
        r = httpx.post(URL, headers=_h(token), json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}}})
        assert r.status_code == 401 and "mcp-session-id" not in r.headers
    r = httpx.post(URL, headers={"Accept": "application/json, text/event-stream"}, json={})
    assert r.status_code == 401


def test_per_user_cap_closes_the_least_recently_used_session(server):
    a = open_raw("cb_bt_alice1")
    b = open_raw("cb_bt_alice2")
    bob = open_raw("cb_bt_bob")
    assert tools_list("cb_bt_alice1", a) == 200  # a is now more recent than b
    c = open_raw("cb_bt_alice3")                  # third alice session: cap is 2
    assert tools_list("cb_bt_alice2", b) == 404   # b (least recently used) was closed
    assert tools_list("cb_bt_alice1", a) == 200
    assert tools_list("cb_bt_alice3", c) == 200
    assert tools_list("cb_bt_bob", bob) == 200    # other users are not touched


def test_idle_sessions_expire(server):
    sid = open_raw("cb_bt_bob")
    assert tools_list("cb_bt_bob", sid) == 200
    time.sleep(4.5)  # MCP_SESSION_IDLE_SECONDS=3 in this test
    assert tools_list("cb_bt_bob", sid) == 404


def _call_and_leave_card_open(token, on_card=lambda: None) -> tuple[str, float]:
    """tools/call propose_claim over raw HTTP and never answer the card (the way Hermes's mcp 2.3
    client keeps reading while its card is open; the 1.30 test client would block in its callback).
    Returns (tool result text, seconds from the card appearing to the result)."""
    sid = open_raw(token)
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "propose_claim", "arguments": {
        "scope": "operations", "subject": "Leeds dock", "attribute": "Friday close", "value": "6pm",
        "quote": "the Leeds dock closes at 6pm on Fridays"}}}
    card_at = None
    with httpx.stream("POST", URL, headers=_h(token, sid), json=body, timeout=30) as r:
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            msg = json.loads(line[5:])
            if msg.get("method") == "elicitation/create":
                card_at = time.monotonic()
                on_card()
            elif msg.get("id") == 7:
                assert card_at is not None, "the card was never shown"
                text = " ".join(c.get("text", "") for c in msg["result"]["content"])
                return text, time.monotonic() - card_at
    raise AssertionError("no tool result")


def test_unanswered_card_times_out_with_a_clear_message(server):
    before = _proposed()
    text, took = _call_and_leave_card_open("cb_bt_bob")
    assert "No answer within 6 seconds" in text and 5.5 < took < 8, (text, took)
    assert _proposed() == before


def test_restart_while_a_card_is_pending_answers_at_once(server):
    loop = server
    try:
        text, took = _call_and_leave_card_open(
            "cb_bt_bob", on_card=lambda: loop.call_soon_threadsafe(local_server.begin_shutdown))
    finally:
        loop.call_soon_threadsafe(local_server._stopping.clear)
    assert "int3 restarted" in text and "Please ask again" in text, text
    assert took < 1, took  # not the 6 s approval window, let alone Hermes's call timeout


def test_read_tools_are_marked_read_only(server):
    async def go():
        async with streamablehttp_client(URL, headers={"Authorization": "Bearer cb_bt_bob"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name: bool(t.annotations and t.annotations.readOnlyHint) for t in (await s.list_tools()).tools}
    ro = asyncio.run(go())
    assert ro == {"search_brain": True, "validate_citations": True, "list_proposals": True,
                  "propose_claim": False, "approve_proposal": False, "reject_proposal": False}
