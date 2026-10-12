"""Envelope encryption + per-tenant key storage (item b).

Unit tests (no DB) always run: round-trip, AAD binding, tamper detection.
DB-gated tests exercise store -> gateway read -> decrypt and prove the
plaintext key is never in the DB nor in captured logs.
"""

import base64
import io
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import keyvault

_SENTINEL = "sk-TENANT-PLAINTEXT-CANARY-9f3a1b"


@pytest.fixture(autouse=True)
def _kek(monkeypatch):
    monkeypatch.setenv("TENANT_KEY_KEK", base64.b64encode(b"k" * 32).decode())


def test_roundtrip():
    blob = keyvault.encrypt(_SENTINEL, tenant_id="brindlewood", provider="anthropic")
    assert _SENTINEL not in blob  # ciphertext does not contain the plaintext
    assert keyvault.decrypt(blob, tenant_id="brindlewood", provider="anthropic") == _SENTINEL


def test_aad_binding_blocks_cross_tenant_reuse():
    blob = keyvault.encrypt(_SENTINEL, tenant_id="brindlewood", provider="anthropic")
    # same blob, different tenant -> authentication tag fails
    with pytest.raises(Exception):
        keyvault.decrypt(blob, tenant_id="hollowmere", provider="anthropic")
    # different provider -> also fails
    with pytest.raises(Exception):
        keyvault.decrypt(blob, tenant_id="brindlewood", provider="openai")


def test_tamper_detected():
    blob = keyvault.encrypt(_SENTINEL, tenant_id="brindlewood", provider="anthropic")
    raw = bytearray(base64.b64decode(blob))
    raw[-1] ^= 0x01  # flip a ciphertext bit
    with pytest.raises(Exception):
        keyvault.decrypt(base64.b64encode(bytes(raw)).decode(), tenant_id="brindlewood", provider="anthropic")


# ---------------------------------------------------------------------------
# DB-gated: store via admin creds, read via the gateway role, prove no leak.
# ---------------------------------------------------------------------------

_DB_READY = bool(os.getenv("GATEWAY_DB_PASSWORD")) and bool(os.getenv("ADMIN_DB_USER"))


def _admin_exec(sql, args=()):
    import psycopg

    with psycopg.connect(
        host=os.getenv("DB_HOST", "db"), port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("CONTROL_DB", "control"),
        user=os.environ["ADMIN_DB_USER"], password=os.environ["ADMIN_DB_PASSWORD"],
        autocommit=True,
    ) as c:
        return c.execute(sql, args).fetchall() if sql.strip().lower().startswith("select") else c.execute(sql, args)


@pytest.mark.skipif(not _DB_READY, reason="needs control DB + admin + gateway creds")
def test_store_read_decrypt_and_no_plaintext_leak(caplog):
    import provider_config

    blob = keyvault.encrypt(_SENTINEL, tenant_id="brindlewood", provider="anthropic")
    _admin_exec(
        "INSERT INTO tenant_providers (tenant_id, provider, base_url, key_owner, model_allowlist, key_ciphertext)"
        " VALUES ('brindlewood','anthropic','https://api.anthropic.com/v1','theirs','{}',%s)"
        " ON CONFLICT (tenant_id, provider) DO UPDATE SET key_ciphertext=EXCLUDED.key_ciphertext",
        (blob,),
    )
    # the stored ciphertext column must not contain the plaintext
    rows = _admin_exec(
        "SELECT key_ciphertext FROM tenant_providers WHERE tenant_id='brindlewood' AND provider='anthropic'"
    )
    assert rows and _SENTINEL not in rows[0][0]

    # the gateway role reads + decrypts; capture ALL logs and assert no leak
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logging.getLogger().addHandler(handler)
    with caplog.at_level(logging.DEBUG):
        cfg = provider_config.load("brindlewood", "anthropic")
    logging.getLogger().removeHandler(handler)
    assert cfg is not None and cfg.api_key == _SENTINEL  # decrypt works
    assert _SENTINEL not in buf.getvalue()
    assert _SENTINEL not in caplog.text

    _admin_exec("DELETE FROM tenant_providers WHERE tenant_id='brindlewood' AND provider='anthropic'")


# ---------------------------------------------------------------------------
# Admin CLI: set-provider / set-key / rotate-key / revoke-key + mandatory cap.
# ---------------------------------------------------------------------------

class _NS:
    def __init__(self, **kw): self.__dict__.update(kw)


@pytest.mark.skipif(not _DB_READY, reason="needs control DB + admin creds")
def test_admin_cli_set_rotate_revoke_and_mandatory_cap(monkeypatch):
    import admin_keys
    import provider_config

    T, P = "brindlewood", "anthropic"
    # key_owner=ours with no cap must be refused (mandatory spend cap)
    with pytest.raises(SystemExit):
        admin_keys.set_provider(_NS(tenant=T, provider=P, base_url="https://api.anthropic.com/v1",
                                    key_owner="ours", models="claude-x", cap_usd=None))
    # theirs, no cap required
    admin_keys.set_provider(_NS(tenant=T, provider=P, base_url="https://api.anthropic.com/v1",
                                key_owner="theirs", models="claude-x,claude-y", cap_usd=None))
    monkeypatch.setenv("KEY", _SENTINEL)
    admin_keys._store_key(_NS(tenant=T, provider=P), require_existing=False)
    cfg = provider_config.load(T, P)
    assert cfg.api_key == _SENTINEL and cfg.model_allowlist == ["claude-x", "claude-y"]
    # rotate to a new value
    monkeypatch.setenv("KEY", _SENTINEL + "-rotated")
    admin_keys._store_key(_NS(tenant=T, provider=P), require_existing=True)
    assert provider_config.load(T, P).api_key == _SENTINEL + "-rotated"
    # revoke clears the key
    admin_keys.revoke_key(_NS(tenant=T, provider=P))
    assert provider_config.load(T, P).api_key is None

    _admin_exec("DELETE FROM tenant_providers WHERE tenant_id=%s AND provider=%s", (T, P))
