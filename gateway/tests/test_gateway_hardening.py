"""Item 3b: gateway hardening — server-pinned model, max_tokens clamp, and
GATEWAY_AUTH=service machine-token verification.

Run with the other gateway unit tests: `cd gateway && pytest`.
No Docker, no network, no DB — the upstream HTTP call and the token resolver
are both mocked/injected.
"""

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import auth
import service_auth
import translate
import upstream


# ---------------------------------------------------------------------------
# (b) server pins the model and caps max_tokens
# ---------------------------------------------------------------------------


def test_oversized_max_tokens_clamped_and_small_passes_through():
    cap = translate.DEFAULT_MAX_TOKENS
    over = translate.openai_request_to_anthropic(
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10**9}
    )
    assert over["max_tokens"] == cap, "a client over-request must clamp to the cap"
    under = translate.openai_request_to_anthropic(
        {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 7}
    )
    assert under["max_tokens"] == 7, "a smaller client request must pass through"


class _FakeResponse:
    status_code = 200

    def json(self):
        return {
            "id": "msg_x",
            "model": "whatever",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class _CapturingClient:
    """Stands in for httpx.AsyncClient; records the body posted upstream."""

    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _CapturingClient.captured = json
        return _FakeResponse()


def test_client_model_ignored_in_upstream_body():
    """The body actually sent upstream carries the server-pinned model, not the
    client's requested one. Asserted from a mocked upstream (captured post body)."""
    pinned = translate.pinned_model()
    _CapturingClient.captured = {}
    with patch.object(upstream, "httpx") as hx:
        hx.AsyncClient = _CapturingClient
        asyncio.run(
            upstream.complete(
                {
                    "model": "attacker/giant-model",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            )
        )
    assert _CapturingClient.captured.get("model") == pinned
    assert _CapturingClient.captured.get("model") != "attacker/giant-model"


# ---------------------------------------------------------------------------
# (a) GATEWAY_AUTH=service — per-user machine token
# ---------------------------------------------------------------------------


class _Creds:
    def __init__(self, token):
        self.credentials = token


def _verify(credentials):
    return asyncio.run(auth.verify_token(credentials))


def test_service_auth_unauthenticated_is_401():
    with patch.object(auth, "GATEWAY_AUTH", "service"):
        with pytest.raises(auth.HTTPException) as ei:
            _verify(None)
    assert ei.value.status_code == 401


def test_service_auth_revoked_or_unknown_is_401():
    with patch.object(auth, "GATEWAY_AUTH", "service"), \
         patch.object(service_auth, "resolve", lambda t: None):
        with pytest.raises(auth.HTTPException) as ei:
            _verify(_Creds("cb_svc_revoked"))
    assert ei.value.status_code == 401


def test_service_auth_valid_token_returns_tenant_user():
    with patch.object(auth, "GATEWAY_AUTH", "service"), \
         patch.object(service_auth, "resolve", lambda t: "brindlewood/priya"):
        identity = _verify(_Creds("cb_svc_good"))
    assert identity == "brindlewood/priya"
