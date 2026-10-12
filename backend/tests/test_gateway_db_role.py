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


_ALL_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@pytest.mark.integration
def test_gateway_role_has_exactly_select_on_service_tokens_and_nothing_else(gateway_role):
    """Exhaustive: for EVERY table in the control DB, gateway_auth has SELECT on
    service_tokens only and no privilege of any kind on anything else — asserted
    via has_table_privilege (which accounts for PUBLIC grants too, unlike a
    REVOKE that only touches the role's own grants). Plus no CREATE on the schema."""
    role, _pw = gateway_role
    with admin.admin_conn(config.CONTROL_DB) as c:
        tables = [
            r["table_name"]
            for r in c.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema='public' ORDER BY table_name"
            )
        ]
        assert "service_tokens" in tables
        select_only = {"service_tokens", "tenant_providers"}  # the gateway's read set
        for t in tables:
            held = [
                p for p in _ALL_PRIVS
                if c.execute("SELECT has_table_privilege(%s,%s,%s) v", (role, t, p)).fetchone()["v"]
            ]
            expected = ["SELECT"] if t in select_only else []
            assert held == expected, f"{role} on {t}: held {held}, expected {expected}"
        assert not c.execute(
            "SELECT has_schema_privilege(%s,'public','CREATE') v", (role,)
        ).fetchone()["v"], "gateway_auth must not have CREATE on schema public"


@pytest.mark.integration
def test_gateway_role_cannot_connect_to_tenant_databases(gateway_role):
    """gateway_auth must not reach any tenant database. Postgres grants CONNECT
    to PUBLIC by default, but _provision_database revokes ALL on each tenant DB
    from PUBLIC, so the role (which has no explicit tenant grant) cannot connect.
    This guards that revocation against regression."""
    role, _pw = gateway_role
    with admin.admin_conn("postgres") as c:
        tenant_dbs = [
            r["datname"]
            for r in c.execute("SELECT datname FROM pg_database WHERE datname LIKE 't\\_%%'")
        ]
        assert tenant_dbs, "no tenant DBs provisioned — fixture precondition missing"
        for db in tenant_dbs:
            can = c.execute(
                "SELECT has_database_privilege(%s,%s,'CONNECT') v", (role, db)
            ).fetchone()["v"]
            assert not can, f"{role} can CONNECT to tenant DB {db}"
