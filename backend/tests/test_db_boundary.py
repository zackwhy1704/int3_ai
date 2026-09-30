"""The database is the isolation boundary. Each connection below is opened the way a
buggy or hostile code path might try it; Postgres itself refuses. None of these rely
on application code."""
import psycopg
import pytest

from app import config
from app.db import tenant_password


def connect(dbname: str, user: str, password: str):
    return psycopg.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=dbname,
                           user=user, password=password, autocommit=True)


A, B, C = "t_brindlewood", "t_hollowmere", "t_brindlewood_labs"


@pytest.mark.parametrize("dbname", [A, B, C, "postgres"])
def test_control_role_cannot_open_any_tenant_database(client, dbname):
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        connect(dbname, "app_control", config.CONTROL_PASSWORD)


@pytest.mark.parametrize("dbname", [B, C, "control", "postgres"])
def test_tenant_a_role_cannot_open_other_databases(client, dbname):
    with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
        connect(dbname, A, tenant_password("brindlewood"))


def test_wrong_tenant_password_is_refused(client):
    # Tenant B's derived password does not open tenant A's role.
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        connect(A, A, tenant_password("hollowmere"))


def test_tenant_role_is_read_only_on_content(client):
    with connect(A, A, tenant_password("brindlewood")) as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] > 0
        for stmt in ("UPDATE claims SET value = 'x'",
                     "DELETE FROM claims",
                     "UPDATE claims SET superseded_by = NULL",
                     "INSERT INTO documents (id, scope_id, title, source, owner, effective_date, body)"
                     " VALUES ('x', 'company-wide', 'x', 'x', 'x', '2026-01-01', 'x')",
                     "UPDATE memberships SET scope_id = 'finance'",
                     "UPDATE users SET active = true",
                     "CREATE TABLE sneaky (id int)"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)


def test_control_role_cannot_rewrite_tenancy(client):
    with connect("control", "app_control", config.CONTROL_PASSWORD) as conn:
        for stmt in ("UPDATE identities SET tenant_id = 'hollowmere'",
                     "UPDATE identities SET user_id = 'ada'",
                     "INSERT INTO identities (email, tenant_id, user_id) VALUES ('x@y.z', 'hollowmere', 'alex')",
                     "UPDATE tenants SET ms_tenant_id = 'x'",
                     "DELETE FROM identities"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
