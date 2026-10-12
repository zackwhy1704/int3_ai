"""Operator CLI to manage per-tenant provider config + keys.

Runs as a ONE-OFF with operator DB credentials (ADMIN_DB_USER /
ADMIN_DB_PASSWORD) — NOT the long-running gateway's least-privilege role. The
gateway service never has write access to tenant_providers; only this CLI does.

The provider key is read from the KEY env var (never a command-line arg, which
would leak via the process list / shell history), encrypted with keyvault
before it touches the DB, and never printed.

  set-provider --tenant T --provider P --base-url U --key-owner ours|theirs
               [--models m1,m2] [--cap-usd X]
  set-key      --tenant T --provider P        (KEY env -> encrypt -> store)
  rotate-key   --tenant T --provider P        (KEY env -> re-encrypt -> replace)
  revoke-key   --tenant T --provider P        (clear the stored key)
"""

import argparse
import os
import sys

import psycopg

import keyvault


def _admin_conn() -> psycopg.Connection:
    user = os.environ.get("ADMIN_DB_USER")
    password = os.environ.get("ADMIN_DB_PASSWORD")
    if not user or not password:
        sys.exit("ADMIN_DB_USER / ADMIN_DB_PASSWORD required (operator credentials)")
    return psycopg.connect(
        host=os.getenv("DB_HOST", "db"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("CONTROL_DB", "control"),
        user=user,
        password=password,
        autocommit=True,
    )


def _require_cap(key_owner: str, cap_usd) -> None:
    # Mandatory daily cap when we own (and pay for) the key.
    if key_owner == "ours" and cap_usd is None:
        sys.exit("a tenant with key_owner='ours' must set --cap-usd (mandatory spend cap)")


def set_provider(a) -> None:
    _require_cap(a.key_owner, a.cap_usd)
    models = [m.strip() for m in (a.models or "").split(",") if m.strip()]
    with _admin_conn() as c:
        c.execute(
            "INSERT INTO tenant_providers"
            " (tenant_id, provider, base_url, key_owner, model_allowlist, daily_cap_usd)"
            " VALUES (%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (tenant_id, provider) DO UPDATE SET"
            "   base_url=EXCLUDED.base_url, key_owner=EXCLUDED.key_owner,"
            "   model_allowlist=EXCLUDED.model_allowlist,"
            "   daily_cap_usd=EXCLUDED.daily_cap_usd, updated_at=now()",
            (a.tenant, a.provider, a.base_url, a.key_owner, models, a.cap_usd),
        )
    print(f"set provider {a.provider} for tenant {a.tenant} (key_owner={a.key_owner}, models={models})")


def _store_key(a, *, require_existing: bool) -> None:
    key = os.environ.get("KEY", "")
    if not key:
        sys.exit("provide the provider key in the KEY environment variable")
    blob = keyvault.encrypt(key, tenant_id=a.tenant, provider=a.provider)
    with _admin_conn() as c:
        if require_existing:
            row = c.execute(
                "SELECT 1 FROM tenant_providers WHERE tenant_id=%s AND provider=%s",
                (a.tenant, a.provider),
            ).fetchone()
            if not row:
                sys.exit("no such tenant/provider; use set-provider first")
        n = c.execute(
            "UPDATE tenant_providers SET key_ciphertext=%s, updated_at=now()"
            " WHERE tenant_id=%s AND provider=%s",
            (blob, a.tenant, a.provider),
        ).rowcount
    if not n:
        sys.exit("no such tenant/provider; use set-provider first")
    print(f"{'rotated' if require_existing else 'set'} key for {a.tenant}/{a.provider} (ciphertext stored, plaintext never persisted)")


def revoke_key(a) -> None:
    with _admin_conn() as c:
        c.execute(
            "UPDATE tenant_providers SET key_ciphertext=NULL, updated_at=now()"
            " WHERE tenant_id=%s AND provider=%s",
            (a.tenant, a.provider),
        )
    print(f"revoked key for {a.tenant}/{a.provider}")


def main() -> None:
    p = argparse.ArgumentParser(prog="admin_keys")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("set-provider", "set-key", "rotate-key", "revoke-key"):
        s = sub.add_parser(name)
        s.add_argument("--tenant", required=True)
        s.add_argument("--provider", required=True)
        if name == "set-provider":
            s.add_argument("--base-url", dest="base_url", required=True)
            s.add_argument("--key-owner", dest="key_owner", choices=["ours", "theirs"], required=True)
            s.add_argument("--models", default="")
            s.add_argument("--cap-usd", dest="cap_usd", type=float, default=None)
    a = p.parse_args()
    {"set-provider": set_provider,
     "set-key": lambda a: _store_key(a, require_existing=False),
     "rotate-key": lambda a: _store_key(a, require_existing=True),
     "revoke-key": revoke_key}[a.cmd](a)


if __name__ == "__main__":
    main()
