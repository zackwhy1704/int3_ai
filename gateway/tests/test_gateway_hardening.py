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


# Model/allowlist enforcement moved to routing (item c/d); see test_routing.py.


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


# ---------------------------------------------------------------------------
# startup self-check (fail closed on an over-privileged DB role).
# DB-gated: skipped in the DB-free CI gateway run; exercised against the live
# control DB with GATEWAY_DB_PASSWORD + APP_CONTROL_PASSWORD set.
# ---------------------------------------------------------------------------

_DB_READY = bool(os.getenv("GATEWAY_DB_PASSWORD")) and bool(os.getenv("APP_CONTROL_PASSWORD"))


@pytest.mark.skipif(not _DB_READY, reason="needs control DB + gateway_auth role")
def test_self_check_passes_for_gateway_auth():
    # Env already points GATEWAY_DB_ROLE at gateway_auth (SELECT-only).
    service_auth.assert_least_privilege()  # must not raise


@pytest.mark.skipif(not _DB_READY, reason="needs control DB + app_control creds")
def test_self_check_refuses_overprivileged_role():
    with patch.dict(
        os.environ,
        {"GATEWAY_DB_ROLE": "app_control", "GATEWAY_DB_PASSWORD": os.environ["APP_CONTROL_PASSWORD"]},
    ):
        with pytest.raises(RuntimeError, match="refusing to start"):
            service_auth.assert_least_privilege()
