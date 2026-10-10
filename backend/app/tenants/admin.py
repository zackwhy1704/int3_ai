"""Operator commands. Run with Postgres superuser credentials, never from the API process.

    python -m app.tenants.admin init
    python -m app.tenants.admin provision --id acme --name "Acme Pte Ltd" \
        --admin-email ops@acme.example [--admin-name "Ops"] \
        [--google-domain acme.example] [--ms-tenant-id <guid>] [--seed seed/acme]
    python -m app.tenants.admin configure --id acme [--google-domain D | --no-google-domain] \
        [--ms-tenant-id G | --no-ms-tenant-id]

Isolation is enforced by Postgres:
- CONNECT on every database is revoked from PUBLIC. The control database accepts only
  app_control; each tenant database accepts only its own role t_<id>.
- Tenant roles own nothing. They get SELECT on content tables and write access only to
  the answer cache. The claims table is not writable at all by the runtime role, on top
  of the append-only trigger.
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg

from psycopg import sql
from psycopg.rows import dict_row

from .. import config, seed
from ..db import tenant_db_name, tenant_password

ADMIN_USER = os.environ.get("ADMIN_DB_USER", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_DB_PASSWORD", "")
APP_DIR = Path(__file__).resolve().parent.parent


def admin_conn(dbname: str) -> psycopg.Connection:
    if not ADMIN_USER:
        sys.exit(
            "ADMIN_DB_USER / ADMIN_DB_PASSWORD are not set: run this in the tools container"
        )
    conn = psycopg.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        dbname=dbname,
        user=ADMIN_USER,
        password=ADMIN_PASSWORD,
        row_factory=dict_row,
        autocommit=True,
    )
    return conn


def admin_tenant_conn(tenant_id: str, vector: bool = True) -> psycopg.Connection:
    from pgvector.psycopg import register_vector

    conn = admin_conn(tenant_db_name(tenant_id))
    if vector:
        register_vector(conn)
    return conn


def role_exists(conn, role: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        is not None
    )


def init() -> None:
    """Create the control-plane role and schema. Idempotent."""
    with admin_conn(config.CONTROL_DB) as conn:
        if not role_exists(conn, config.CONTROL_ROLE):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(config.CONTROL_ROLE),
                    sql.Literal(config.CONTROL_PASSWORD),
                )
            )
        else:
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(config.CONTROL_ROLE),
                    sql.Literal(config.CONTROL_PASSWORD),
                )
            )
        for db in ("postgres", config.CONTROL_DB):
            conn.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(db)
                )
            )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(config.CONTROL_DB), sql.Identifier(config.CONTROL_ROLE)
            )
        )
        conn.execute((APP_DIR / "control_schema.sql").read_text())
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        conn.execute(
            sql.SQL("""
            GRANT USAGE ON SCHEMA public TO {r};
            GRANT SELECT ON tenants TO {r};
            GRANT SELECT ON identities TO {r};
            GRANT UPDATE (google_sub, ms_subject) ON identities TO {r};
            GRANT SELECT, INSERT, UPDATE, DELETE ON login_attempts, sessions TO {r};
            GRANT SELECT, INSERT, UPDATE, DELETE ON service_tokens TO {r};
            GRANT SELECT, INSERT ON retrievals TO {r};
        """).format(r=sql.Identifier(config.CONTROL_ROLE))
        )
    print("control plane ready")


def provision(
    tenant_id: str,
    name: str,
    admin_email: str | None,
    admin_name: str,
    google_domain: str | None,
    ms_tenant_id: str | None,
    seed_dir: Path | None,
) -> None:
    db = tenant_db_name(tenant_id)
    if not seed_dir and not admin_email:
        sys.exit("--admin-email is required without --seed")
    with admin_conn(config.CONTROL_DB) as control:
        if control.execute(
            "SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone():
            sys.exit(f"tenant {tenant_id} already exists")
        if role_exists(control, db):
            sys.exit(f"role {db} already exists")
        control.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(db), sql.Literal(tenant_password(tenant_id))
            )
        )
        control.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
    try:
        _provision_database(
            tenant_id,
            name,
            admin_email,
            admin_name,
            google_domain,
            ms_tenant_id,
            seed_dir,
        )
    except BaseException:
        # Leave nothing half-made: no tenant row, no database, no role.
        with admin_conn(config.CONTROL_DB) as control:
            control.execute("DELETE FROM identities WHERE tenant_id = %s", (tenant_id,))
            control.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
            control.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(db)
                )
            )
            control.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(db))
            )
        raise


def _provision_database(
    tenant_id, name, admin_email, admin_name, google_domain, ms_tenant_id, seed_dir
) -> None:
    db = tenant_db_name(tenant_id)
    with admin_conn(config.CONTROL_DB) as control:
        control.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db))
        )
        control.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(db), sql.Identifier(db)
            )
        )

    with admin_tenant_conn(tenant_id, vector=False) as tconn:
        tconn.execute((APP_DIR / "schema.sql").read_text())
        tconn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        tconn.execute(
            sql.SQL("""
            GRANT USAGE ON SCHEMA public TO {r};
            GRANT SELECT ON users, scopes, memberships, documents, chunks, claims TO {r};
            GRANT SELECT, INSERT, UPDATE ON answer_cache TO {r};
        """).format(r=sql.Identifier(db))
        )

    with admin_tenant_conn(tenant_id) as tconn:
        if seed_dir:
            seed.load_documents(tconn, seed_dir)
            seed.load_claims(tconn, seed_dir)
        else:
            tconn.execute(
                "INSERT INTO users (id, name, title, email) VALUES ('admin', %s, 'Admin', %s)",
                (admin_name, admin_email.lower()),
            )
            tconn.execute(
                "INSERT INTO scopes VALUES ('company-wide', 'Company-wide', 'everyone',"
                " 'Shared with everyone in the company.', 'admin')"
            )
            tconn.execute("INSERT INTO memberships VALUES ('admin', 'company-wide')")
        users = tconn.execute("SELECT id, email FROM users").fetchall()

    with admin_conn(config.CONTROL_DB) as control:
        control.execute(
            "INSERT INTO tenants (id, name, google_domain, ms_tenant_id)"
            " VALUES (%s, %s, %s, %s)",
            (tenant_id, name, google_domain, ms_tenant_id),
        )
        for u in users:
            control.execute(
                "INSERT INTO identities (email, tenant_id, user_id) VALUES (%s, %s, %s)",
                (u["email"], tenant_id, u["id"]),
            )
    print(f"tenant {tenant_id} provisioned: database {db}, {len(users)} user(s)")


def configure(tenant_id: str, google_domain, ms_tenant_id) -> None:
    """Set sign-in policy. `...` means leave unchanged; None clears it."""
    with admin_conn(config.CONTROL_DB) as control:
        if google_domain is not ...:
            control.execute(
                "UPDATE tenants SET google_domain = %s WHERE id = %s",
                (google_domain, tenant_id),
            )
        if ms_tenant_id is not ...:
            control.execute(
                "UPDATE tenants SET ms_tenant_id = %s WHERE id = %s",
                (ms_tenant_id, tenant_id),
            )
        print(
            control.execute(
                "SELECT * FROM tenants WHERE id = %s", (tenant_id,)
            ).fetchone()
        )


def exists(tenant_id: str) -> bool:
    with admin_conn(config.CONTROL_DB) as control:
        return (
            control.execute(
                "SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)
            ).fetchone()
            is not None
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("provision")
    p.add_argument("--id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--admin-email")
    p.add_argument("--admin-name", default="Admin")
    p.add_argument("--google-domain")
    p.add_argument("--ms-tenant-id")
    p.add_argument("--seed", type=Path)
    c = sub.add_parser("configure")
    c.add_argument("--id", required=True)
    c.add_argument("--google-domain", default=...)
    c.add_argument("--no-google-domain", action="store_true")
    c.add_argument("--ms-tenant-id", default=...)
    c.add_argument("--no-ms-tenant-id", action="store_true")
    a = ap.parse_args()
    if a.cmd == "init":
        init()
    elif a.cmd == "provision":
        provision(
            a.id,
            a.name,
            a.admin_email,
            a.admin_name,
            a.google_domain,
            a.ms_tenant_id,
            a.seed,
        )
    else:
        configure(
            a.id,
            None if a.no_google_domain else a.google_domain,
            None if a.no_ms_tenant_id else a.ms_tenant_id,
        )


if __name__ == "__main__":
    main()
