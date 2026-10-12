"""The gateway's DB role is least-privilege: SELECT on service_tokens, nothing
more. Real Postgres integration test (needs the tools container's admin creds).

Required-fix invariant: the gateway (the tier nearest the per-user container)
must not be able to MINT a service token or touch sessions. If it could, a
compromised gateway could impersonate any user and read their brain via /v1.
"""

from __future__ import annotations

import psycopg
import pytest

from app import config
from app.tenants import admin

_GW_PASS = "gw-role-test-password"


@pytest.fixture()
def gateway_role(monkeypatch):
    # Create/refresh the gateway role with a known password, via admin.init().
    monkeypatch.setattr(config, "GATEWAY_DB_PASSWORD", _GW_PASS)
    admin.init()
    return config.GATEWAY_DB_ROLE, _GW_PASS


def _conn(role: str, password: str) -> psycopg.Connection:
    return psycopg.connect(
        host=config.DB_HOST, port=config.DB_PORT, dbname=config.CONTROL_DB,
        user=role, password=password, autocommit=True,
    )


@pytest.mark.integration
def test_gateway_role_can_select_service_tokens(gateway_role):
    role, pw = gateway_role
    with _conn(role, pw) as c:
        # SELECT is the one thing it may do; must not raise.
        c.execute("SELECT token_hash, tenant_id, user_id FROM service_tokens LIMIT 1")


@pytest.mark.integration
def test_gateway_role_cannot_insert_service_tokens(gateway_role):
    role, pw = gateway_role
    with _conn(role, pw) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO service_tokens (token_hash, tenant_id, user_id, label)"
                " VALUES ('deadbeef', 'brindlewood', 'mcp_spike', 'forged')"
            )


@pytest.mark.integration
def test_gateway_role_cannot_read_sessions(gateway_role):
    role, pw = gateway_role
    with _conn(role, pw) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT id_hash FROM sessions LIMIT 1")
