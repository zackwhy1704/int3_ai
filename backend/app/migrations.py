"""Versioned schema migrations, recorded per database in schema_migrations and
applied idempotently by app.tenants.admin.migrate
(CLI: `python -m app.tenants.admin migrate`).

Phase 2 capture is tenant-version 1: the proposals table, the workflow authority,
the allow_self_approval flag, the SECURITY DEFINER approve function, and the
capture grant posture (writer role owns the function; runtime has EXECUTE only).
A migration re-applies the current schema + capture grants idempotently and
records the version — so a PRE-Phase-2 tenant DB gains everything, and a current
one is a no-op. The control DB has no Phase 2 change (proposals live per tenant);
it only gets the schema_migrations bookkeeping.
"""

TENANT_VERSION = 1
CONTROL_VERSION = 1


def ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )


def current(conn) -> int:
    ensure_table(conn)
    return conn.execute("SELECT coalesce(max(version), 0) AS v FROM schema_migrations").fetchone()["v"]


def record(conn, version: int) -> None:
    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING", (version,))
