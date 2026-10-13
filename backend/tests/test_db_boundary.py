"""The database is the isolation boundary. Each connection below is opened the way a
buggy or hostile code path might try it; Postgres itself refuses. None of these rely
on application code."""

import psycopg
import pytest

from app import config
from app.db import tenant_password


def connect(dbname: str, user: str, password: str):
    return psycopg.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        dbname=dbname,
        user=user,
        password=password,
        autocommit=True,
    )


A, B, C = "t_brindlewood", "t_hollowmere", "t_brindlewood_labs"


@pytest.mark.parametrize("dbname", [A, B, C, "postgres"])
def test_control_role_cannot_open_any_tenant_database(client, dbname):
    with pytest.raises(
        psycopg.OperationalError, match="permission denied for database"
    ):
        connect(dbname, "app_control", config.CONTROL_PASSWORD)


@pytest.mark.parametrize("dbname", [B, C, "control", "postgres"])
def test_tenant_a_role_cannot_open_other_databases(client, dbname):
    with pytest.raises(
        psycopg.OperationalError, match="permission denied for database"
    ):
        connect(dbname, A, tenant_password("brindlewood"))


def test_wrong_tenant_password_is_refused(client):
    # Tenant B's derived password does not open tenant A's role.
    with pytest.raises(
        psycopg.OperationalError, match="password authentication failed"
    ):
        connect(A, A, tenant_password("hollowmere"))


def test_tenant_role_capture_boundary(client):
    """The runtime role may read content and read/write proposals, and approves a
    proposal ONLY by EXECUTEing the SECURITY DEFINER function — it cannot write
    documents/chunks/claims directly (Phase 2 audit item 3), nor touch identities,
    memberships or the schema."""
    with connect(A, A, tenant_password("brindlewood")) as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] > 0
        # No direct content write and no identity/schema write (grant-blocked).
        for stmt in (
            "INSERT INTO documents (id,scope_id,title,source,owner,effective_date,body)"
            " VALUES ('x','company-wide','x','x','x','2026-01-01','x')",
            "UPDATE claims SET superseded_by = NULL",
            "DELETE FROM claims",
            "DELETE FROM proposals",
            "UPDATE memberships SET scope_id = 'finance'",
            "CREATE TABLE sneaky (id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
        # Exact privilege posture.
        def priv(tbl, p):
            return conn.execute("SELECT has_table_privilege(current_user, %s, %s)", (tbl, p)).fetchone()[0]
        assert priv("proposals", "INSERT") and priv("proposals", "UPDATE")  # runtime manages proposals
        assert not priv("documents", "INSERT")
        assert not priv("chunks", "INSERT")
        assert not priv("claims", "INSERT") and not priv("claims", "UPDATE")
        assert conn.execute(
            "SELECT has_function_privilege(current_user,"
            " 'approve_workflow_proposal(uuid,text,boolean,vector)', 'EXECUTE')"
        ).fetchone()[0] is True


def test_public_has_no_execute_on_capture_functions(client):
    """EXECUTE on the capture functions is default-DENY: PUBLIC has none (a
    non-NULL proacl with no empty-grantee entry means PUBLIC was revoked)."""
    with connect(A, A, tenant_password("brindlewood")) as conn:
        for fn in ("approve_workflow_proposal", "capture_statement"):
            acl = conn.execute(
                "SELECT proacl::text[] FROM pg_proc WHERE proname = %s", (fn,)
            ).fetchone()[0]
            assert acl is not None, f"{fn}: proacl is default (PUBLIC implicitly has EXECUTE)"
            assert not any(e.startswith("=") for e in acl), f"{fn}: PUBLIC EXECUTE present: {acl}"


def test_control_role_cannot_rewrite_tenancy(client):
    with connect("control", "app_control", config.CONTROL_PASSWORD) as conn:
        for stmt in (
            "UPDATE identities SET tenant_id = 'hollowmere'",
            "UPDATE identities SET user_id = 'ada'",
            "INSERT INTO identities (email, tenant_id, user_id) VALUES ('x@y.z', 'hollowmere', 'alex')",
            "UPDATE tenants SET ms_tenant_id = 'x'",
            "DELETE FROM identities",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
