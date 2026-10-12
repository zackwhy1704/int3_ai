"""Item c: multi-provider routing — field-allowlist rebuild, per-provider
upstream shape, tenant-key injection, usage normalisation. No Docker/DB/network
(the token resolver and httpx are mocked/injected)."""

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import routing
import translate
import upstream


# --- field allowlist --------------------------------------------------------

def test_sanitise_drops_persistence_and_identity_fields():
    body = {
        "model": "claude-x",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function"}],
        "temperature": 0.2,
        "max_tokens": 50,
        "stream": True,
        # everything below must be dropped:
        "store": True,
        "metadata": {"trace": "x"},
        "user": "attacker-chosen-id",
        "service_tier": "scale",
        "prompt_cache_key": "k",
        "safety_identifier": "s",
        "logprobs": True,
    }
    out = translate.sanitise_request(body)
    assert set(out) <= {"model", "messages", "tools", "tool_choice", "temperature", "max_tokens", "stream"}
    for dropped in ("store", "metadata", "user", "service_tier", "prompt_cache_key", "safety_identifier", "logprobs"):
        assert dropped not in out


# --- mocked upstream capture ------------------------------------------------

class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._p = payload

    def json(self):
        return self._p


class _Capture:
    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _Capture.calls.append({"url": url, "headers": headers, "json": json})
        if "/messages" in url:  # anthropic shape
            return _Resp({"model": json["model"], "content": [{"type": "text", "text": "ok"}],
                          "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 2,
                          "cache_read_input_tokens": 1, "cache_creation_input_tokens": 4}})
        return _Resp({"id": "x", "object": "chat.completion", "model": json["model"],
                      "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 6}}})


def _run_complete(route, body):
    _Capture.calls = []
    with patch.object(upstream, "httpx") as hx:
        hx.AsyncClient = _Capture
        out = asyncio.run(upstream.complete(translate.sanitise_request(body), route, caller="brindlewood/priya"))
    return out, _Capture.calls[-1]


def test_dropped_fields_never_reach_upstream_anthropic():
    route = routing.Route("anthropic", "https://api.anthropic.com/v1", "sk-tenant-A", "claude-x", "theirs", None)
    _out, call = _run_complete(route, {
        "model": "claude-x", "messages": [{"role": "user", "content": "hi"}],
        "store": True, "metadata": {"t": "x"}, "user": "evil",
    })
    assert "store" not in call["json"] and "metadata" not in call["json"] and "user" not in call["json"]
    # tenant key injected (anthropic uses x-api-key), not any client-supplied key
    assert call["headers"]["x-api-key"] == "sk-tenant-A"


def test_openai_route_is_passthrough_with_bearer_tenant_key():
    route = routing.Route("openai", "https://api.openai.com/v1", "sk-tenant-OAI", "gpt-mid", "theirs", None)
    _out, call = _run_complete(route, {
        "model": "gpt-mid", "messages": [{"role": "user", "content": "hi"}], "store": True, "metadata": {},
    })
    assert call["url"].endswith("/chat/completions")  # no anthropic translation
    assert call["json"]["model"] == "gpt-mid" and "store" not in call["json"]
    assert call["headers"]["authorization"] == "Bearer sk-tenant-OAI"


# --- usage normalisation ----------------------------------------------------

def test_usage_normalisation_per_provider():
    a = routing.normalise_usage("anthropic", {"input_tokens": 5, "output_tokens": 7,
                                              "cache_creation_input_tokens": 9, "cache_read_input_tokens": 2})
    assert a == {"input": 5, "output": 7, "cache_write": 9, "cache_read": 2}
    o = routing.normalise_usage("openai", {"prompt_tokens": 10, "completion_tokens": 3,
                                           "prompt_tokens_details": {"cached_tokens": 4}})
    assert o == {"input": 10, "output": 3, "cache_write": 0, "cache_read": 4}


# --- routing resolution (dev mode, no DB) -----------------------------------

def test_dev_mode_routes_to_env_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    r = routing.resolve("dev", None)
    assert r.provider == "anthropic" and r.api_key == "env-key" and r.model == "claude-sonnet-5-5"
