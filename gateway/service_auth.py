"""Per-user machine-token verification for GATEWAY_AUTH=service.

In this topology the gateway's caller is the agent inside a per-user container,
not a human — so the OIDC (human ID token) mode does not fit. A service token
identifies the user whose budget a request spends.

Single source of truth: the gateway verifies against the SAME control-DB
``service_tokens`` table the backend mints into (backend/app/auth/service_tokens.py),
not a copy. Rationale:
  * one place mints, one place revokes — a separate gateway store would split
    revocation and risk a token still honoured at one tier after the other
    revoked it (exactly the gap we are closing);
  * no secret duplication — the token hash lives in one row read by both tiers,
    never copied into a second store;
  * the gateway needs only READ on that one table (SELECT), not control-plane
    write access. A dedicated read-only DB role is the production refinement;
    the pilot reuses the control role's read path.

Token placement note (important): the *brain* service token must never enter
the Hermes container. THIS gateway token necessarily does — Hermes holds it to
call the model. That is acceptable because its only power is spending that
user's budget through a gateway that pins the model and caps max_tokens; it
cannot read the brain or reach any other endpoint.

``resolve`` is module-level so tests can monkeypatch it without a database.
"""

import hashlib
import os
from typing import Optional

_PREFIX = "cb_svc_"  # same recognisable prefix the backend mints with


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def resolve_from_db(token: str) -> Optional[str]:
    """``"tenant/user"`` for a live (non-revoked) token, else None.

    Reads the control DB directly. Imported lazily so the module (and the
    gateway test suite) loads without psycopg or DB config present.
    """
    if not token or not token.startswith(_PREFIX):
        return None
    import psycopg
    from psycopg.rows import dict_row

    # Connect as the least-privilege gateway role (SELECT on service_tokens
    # only). The gateway must NOT hold app_control, which can mint tokens and
    # read sessions — a compromised gateway could then impersonate any user.
    dsn = {
        "host": os.getenv("DB_HOST", "db"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("CONTROL_DB", "control"),
        "user": os.environ["GATEWAY_DB_ROLE"],
        "password": os.environ["GATEWAY_DB_PASSWORD"],
    }
    with psycopg.connect(**dsn, row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute(
            "SELECT tenant_id, user_id FROM service_tokens"
            " WHERE token_hash = %s AND revoked_at IS NULL",
            (_hash(token),),
        ).fetchone()
    if not row:
        return None
    return f"{row['tenant_id']}/{row['user_id']}"


def _gateway_dsn() -> dict:
    return {
        "host": os.getenv("DB_HOST", "db"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("CONTROL_DB", "control"),
        "user": os.environ["GATEWAY_DB_ROLE"],
        "password": os.environ["GATEWAY_DB_PASSWORD"],
    }


def assert_least_privilege() -> None:
    """Fail closed at boot if the gateway's DB role can MUTATE service_tokens.

    An env that points the gateway at an over-privileged role (e.g. app_control,
    which can mint tokens) must refuse to start rather than silently enabling
    token forgery. Verifies against the live grant via has_table_privilege, so
    it catches a misconfig the code alone cannot."""
    import psycopg

    with psycopg.connect(**_gateway_dsn(), autocommit=True) as conn:
        for priv in ("INSERT", "UPDATE", "DELETE"):
            has = conn.execute(
                "SELECT has_table_privilege(current_user, 'service_tokens', %s)", (priv,)
            ).fetchone()[0]
            if has:
                user = conn.execute("SELECT current_user").fetchone()[0]
                raise RuntimeError(
                    f"gateway DB role {user!r} has {priv} on service_tokens; "
                    "refusing to start — the gateway needs SELECT only "
                    "(point GATEWAY_DB_ROLE at gateway_auth, not app_control)"
                )


# Indirection point for tests (monkeypatch gateway.service_auth.resolve).
resolve = resolve_from_db
