"""Runtime read of a tenant's provider config (gateway side, least privilege).

Connects as the gateway_auth role (SELECT only) and decrypts the envelope-
encrypted provider key with the KEK from the gateway's env. The plaintext key
exists only in memory, only for the duration of an upstream call, and is never
logged.
"""

import os
from dataclasses import dataclass
from typing import Optional

import keyvault


@dataclass
class ProviderConfig:
    tenant_id: str
    provider: str
    base_url: str
    key_owner: str  # "ours" | "theirs"
    model_allowlist: list[str]
    daily_cap_usd: Optional[float]
    api_key: Optional[str]  # decrypted; None if no key stored


def _dsn() -> dict:
    return {
        "host": os.getenv("DB_HOST", "db"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("CONTROL_DB", "control"),
        "user": os.environ["GATEWAY_DB_ROLE"],
        "password": os.environ["GATEWAY_DB_PASSWORD"],
    }


def load(tenant_id: str, provider: str) -> Optional[ProviderConfig]:
    """The tenant's config for one provider, or None if not configured."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(**_dsn(), row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute(
            "SELECT base_url, key_owner, model_allowlist, key_ciphertext, daily_cap_usd"
            " FROM tenant_providers WHERE tenant_id = %s AND provider = %s",
            (tenant_id, provider),
        ).fetchone()
    if row is None:
        return None
    api_key = None
    if row["key_ciphertext"]:
        api_key = keyvault.decrypt(
            row["key_ciphertext"], tenant_id=tenant_id, provider=provider
        )
    cap = row["daily_cap_usd"]
    return ProviderConfig(
        tenant_id=tenant_id,
        provider=provider,
        base_url=row["base_url"],
        key_owner=row["key_owner"],
        model_allowlist=list(row["model_allowlist"] or []),
        daily_cap_usd=float(cap) if cap is not None else None,
        api_key=api_key,
    )
