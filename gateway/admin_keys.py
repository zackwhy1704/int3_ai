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


def mint_key_link(a) -> None:
    """Mint a ONE-TIME, short-TTL link for a tenant admin to enter their BYOK provider key into
    the encrypt-only intake. Only the sha256 of the token is stored (like service_tokens); the
    token appears once, here. Requires a provider row with key_owner='theirs'. Prints a WARNING if
    a key is already set (using the link will overwrite it and emit key_link.key_replaced)."""
    import datetime
    import hashlib
    import secrets

    ttl = max(0.0, min(float(a.ttl_hours), 24.0))  # default 2h (CLI), hard max 24h
    with _admin_conn() as c:
        row = c.execute(
            "SELECT key_owner, key_ciphertext IS NOT NULL FROM tenant_providers"
            " WHERE tenant_id=%s AND provider=%s", (a.tenant, a.provider)).fetchone()
        if not row:
            sys.exit(f"no provider row for {a.tenant}/{a.provider}; run set-provider --key-owner theirs first")
        if row[0] != "theirs":
            sys.exit(f"key_owner is '{row[0]}', not 'theirs' — a key-entry link is only for BYOK tenants")
        token = "cb_keylink_" + secrets.token_hex(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=ttl)
        c.execute(
            "INSERT INTO key_entry_links (token_hash, tenant_id, provider, expires_at, created_by)"
            " VALUES (%s,%s,%s,%s,%s)",
            (token_hash, a.tenant, a.provider, expires, os.getenv("USER") or os.getenv("USERNAME") or "operator"))
    base = (a.url_base or os.getenv("KEY_INTAKE_BASE_URL") or "https://KEY-INTAKE-HOST").rstrip("/")
    if row[1]:
        print("WARNING: a provider key is ALREADY set for this tenant/provider.")
        print("         Using this link REPLACES it (emits key_link.key_replaced). Confirm intent.")
    print(f"one-time key-entry link (expires in {ttl:g}h, single use):")
    print(f"  {base}/key/{token}")
    print("Deliver this to the tenant admin OUT OF BAND (not email/chat where possible); it must be")
    print("opened over TLS. After entry, re-confirm the fingerprint it shows with the admin.")


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
    for name in ("set-provider", "set-key", "rotate-key", "revoke-key", "mint-key-link"):
        s = sub.add_parser(name)
        s.add_argument("--tenant", required=True)
        s.add_argument("--provider", required=True)
        if name == "set-provider":
            s.add_argument("--base-url", dest="base_url", required=True)
            s.add_argument("--key-owner", dest="key_owner", choices=["ours", "theirs"], required=True)
            s.add_argument("--models", default="")
            s.add_argument("--cap-usd", dest="cap_usd", type=float, default=None)
        if name == "mint-key-link":
            s.add_argument("--ttl-hours", dest="ttl_hours", type=float, default=2.0)
            s.add_argument("--url-base", dest="url_base", default=None,
                           help="intake base URL (else KEY_INTAKE_BASE_URL); e.g. https://keys.example/")
    a = p.parse_args()
    {"set-provider": set_provider,
     "set-key": lambda a: _store_key(a, require_existing=False),
     "rotate-key": lambda a: _store_key(a, require_existing=True),
     "revoke-key": revoke_key,
     "mint-key-link": mint_key_link}[a.cmd](a)


if __name__ == "__main__":
    main()
