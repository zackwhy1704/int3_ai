"""Database connections. There is no default database.

- control_conn(): the control plane (tenants, identities, sessions), as role app_control.
- tenant_conn(tenant_id): one tenant's database, as that tenant's own role.

The isolation boundary is in Postgres, not here: each tenant database accepts
connections only from its own role (CONNECT is revoked from PUBLIC), and each role
can only read and write what its grants allow. A connection opened any other way
fails at the database. See app/tenants/admin.py for the grants.
"""

import hashlib
import hmac
import re

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from . import config

TENANT_ID = re.compile(r"^[a-z][a-z0-9_]{1,38}$")


def tenant_db_name(tenant_id: str) -> str:
    if not TENANT_ID.match(tenant_id):
        raise ValueError(f"invalid tenant id: {tenant_id!r}")
    return f"t_{tenant_id}"


def tenant_password(tenant_id: str, key: str | None = None) -> str:
    key = key if key is not None else config.TENANT_DB_KEY
    if not key:
        raise RuntimeError("TENANT_DB_KEY is not set")
    return hmac.new(
        key.encode(), f"tenant-role:{tenant_id}".encode(), hashlib.sha256
    ).hexdigest()


def _connect(dbname: str, user: str, password: str, vector: bool) -> psycopg.Connection:
    conn = psycopg.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        dbname=dbname,
        user=user,
        password=password,
        row_factory=dict_row,
        autocommit=True,
    )
    if vector:
        register_vector(conn)
    return conn


def control_conn() -> psycopg.Connection:
    return _connect(
        config.CONTROL_DB, config.CONTROL_ROLE, config.CONTROL_PASSWORD, vector=False
    )


def tenant_conn(tenant_id: str) -> psycopg.Connection:
    name = tenant_db_name(tenant_id)
    return _connect(name, name, tenant_password(tenant_id), vector=True)
