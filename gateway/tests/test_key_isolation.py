"""Item f: a tenant's provider key is used ONLY for that tenant's own calls.

The mocked upstream records a FINGERPRINT (sha256 prefix) of whatever key it was
handed — never the key itself. Interleaving two tenants' calls, each upstream
request carries that tenant's fingerprint and never the other tenant's. No DB,
no network: Route objects are built directly and httpx is replaced."""

import asyncio
import hashlib
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import routing
import translate
import upstream


def _fp(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _KeyCapture:
    """Records the fingerprint of the credential presented on each upstream POST."""
    seen: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        headers = headers or {}
        key = headers.get("x-api-key") or headers.get("authorization", "").removeprefix("Bearer ")
        _KeyCapture.seen.append(_fp(key))
        if "/messages" in url:
            return _Resp({"model": json.get("model"), "content": [{"type": "text", "text": "ok"}],
                          "stop_reason": "end_turn", "usage": {}})
        return _Resp({"model": json.get("model"), "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}], "usage": {}})


def _call(route: routing.Route) -> None:
    body = translate.sanitise_request({"model": route.model, "messages": [{"role": "user", "content": "hi"}]})
    with patch.object(upstream, "httpx") as hx:
        hx.AsyncClient = _KeyCapture
        asyncio.run(upstream.complete(body, route, caller="t/u"))


def test_each_tenant_only_ever_sends_its_own_key():
    key_a, key_b = "sk-ant-AAA-tenant-a", "sk-ant-BBB-tenant-b"
    route_a = routing.Route("anthropic", "https://api.anthropic.com/v1", key_a, "claude-x", "theirs", None)
    route_b = routing.Route("anthropic", "https://api.anthropic.com/v1", key_b, "claude-x", "theirs", None)
    fp_a, fp_b = _fp(key_a), _fp(key_b)
    assert fp_a != fp_b

    _KeyCapture.seen = []
    for _ in range(5):                 # interleave A,B,A,B,...
        _call(route_a)
        _call(route_b)

    assert _KeyCapture.seen == [fp_a, fp_b] * 5          # every call carried the right key
    assert fp_a not in _KeyCapture.seen[1::2]            # tenant B's calls never carried A's key
    assert fp_b not in _KeyCapture.seen[0::2]            # tenant A's calls never carried B's key


def test_openai_route_also_isolated():
    key_a, key_b = "sk-oai-AAA", "sk-oai-BBB"
    route_a = routing.Route("openai", "https://api.openai.com/v1", key_a, "gpt-mid", "theirs", None)
    route_b = routing.Route("openai", "https://api.openai.com/v1", key_b, "gpt-mid", "theirs", None)
    _KeyCapture.seen = []
    _call(route_b)   # B first this time
    _call(route_a)
    assert _KeyCapture.seen == [_fp(key_b), _fp(key_a)]
