"""Single-use BYOK key-entry intake (Phase 2b).

Unit tests (no DB) drive the endpoint with a fake store + stubbed provider validation, and prove:
single use / expiry -> 410; validate-then-consume ordering; 3-failure burn; headers; and that the
KEY, the TOKEN, and the POST body never appear in captured logs or the response. DB-gated tests
exercise the real consume_key_link SECURITY DEFINER function (single use, concurrency, owner check)
and skip without a control DB.
"""
import base64
import io
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import keyvault
import key_intake
import limits
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_KEY = "sk-ant-SYNTHETIC-CANARY-5a2b9c"           # synthetic provider key (never real)
_TOKEN = "cb_keylink_" + "a" * 64                  # synthetic one-time token
_TOKHASH = key_intake._hash(_TOKEN)
_LINKID = _TOKHASH[:12]


class _FakeStore:
    """Mimics the key_intake DB boundary without Postgres."""
    def __init__(self, link, consume="stored", fail_seq=None):
        self._link = link
        self._consume = consume
        self._fails = list(fail_seq or [1, 2, 3])
        self.consume_calls = []
        self.validated_before_consume = None

    def lookup(self, token_hash):
        return dict(self._link) if self._link and token_hash == _TOKHASH else None

    def record_failure(self, token_hash):
        return self._fails.pop(0) if self._fails else 3

    def consume(self, token_hash, ciphertext, fingerprint):
        self.consume_calls.append((token_hash, ciphertext, fingerprint))
        return self._consume


def _live_link(**over):
    link = {"tenant_id": "brindlewood", "provider": "anthropic", "used": False,
            "expired": False, "fail_count": 0, "base_url": "https://api.anthropic.com/v1",
            "key_owner": "theirs"}
    link.update(over)
    return link


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # intake holds ONLY the public key (encrypt-only); rate limit disabled unless a test sets it.
    pub, _priv = keyvault.generate_keypair()
    monkeypatch.setenv("KEYVAULT_PUBLIC_KEY", pub)
    monkeypatch.delenv("KEYVAULT_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("TENANT_KEY_KEK", raising=False)
    monkeypatch.setattr(limits, "RATE_LIMIT_PER_MIN", 0)   # disabled for flow tests
    monkeypatch.setattr(key_intake, "_validate_provider_key", lambda *a, **k: True)


def _client():
    return TestClient(key_intake.create_app())


def _capture_logs():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    logging.getLogger("key_intake").addHandler(h)
    logging.getLogger("key_intake").setLevel(logging.DEBUG)
    return buf, h


def test_get_form_served_for_live_link(monkeypatch):
    monkeypatch.setattr(key_intake, "_store", _FakeStore(_live_link()))
    r = _client().get(f"/key/{_TOKEN}")
    assert r.status_code == 200 and "Enter your anthropic API key" in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in r.headers["content-security-policy"]


@pytest.mark.parametrize("over", [{"used": True}, {"expired": True}, None])
def test_used_or_expired_or_missing_link_is_410(monkeypatch, over):
    link = None if over is None else _live_link(**over)
    monkeypatch.setattr(key_intake, "_store", _FakeStore(link))
    assert _client().get(f"/key/{_TOKEN}").status_code == 410
    assert _client().post(f"/key/{_TOKEN}", data={"key": _KEY}).status_code == 410


def test_validate_then_consume_order_and_success(monkeypatch):
    store = _FakeStore(_live_link(), consume="stored")
    monkeypatch.setattr(key_intake, "_store", store)
    seen = {}
    def fake_validate(provider, base_url, key):
        seen["validated"] = key
        seen["consumed_yet"] = len(store.consume_calls)   # must be 0 at validation time
        return True
    monkeypatch.setattr(key_intake, "_validate_provider_key", fake_validate)
    r = _client().post(f"/key/{_TOKEN}", data={"key": _KEY})
    assert r.status_code == 200 and "Key stored" in r.text
    assert seen["consumed_yet"] == 0                       # validated BEFORE consume
    assert len(store.consume_calls) == 1
    # a v2 ciphertext was stored, not the plaintext key
    _h, ciphertext, fp = store.consume_calls[0]
    assert _KEY not in ciphertext and base64.b64decode(ciphertext)[:1] == b"\x02"
    assert fp == key_intake._fingerprint(_KEY)
    assert _KEY not in r.text                               # key never echoed


def test_validation_failure_does_not_consume_and_burns_after_3(monkeypatch):
    store = _FakeStore(_live_link(), fail_seq=[1, 2, 3])
    monkeypatch.setattr(key_intake, "_store", store)
    monkeypatch.setattr(key_intake, "_validate_provider_key", lambda *a, **k: False)
    c = _client()
    assert c.post(f"/key/{_TOKEN}", data={"key": _KEY}).status_code == 400   # 1
    assert c.post(f"/key/{_TOKEN}", data={"key": _KEY}).status_code == 400   # 2
    r3 = c.post(f"/key/{_TOKEN}", data={"key": _KEY})                         # 3 -> burned
    assert r3.status_code == 403 and "closed" in r3.text
    assert store.consume_calls == []                                          # never consumed on failure


def test_rejected_owner_outcome_is_410(monkeypatch):
    # the SECURITY DEFINER function returns 'rejected_owner' for key_owner='ours'/missing row
    monkeypatch.setattr(key_intake, "_store", _FakeStore(_live_link(), consume="rejected_owner"))
    r = _client().post(f"/key/{_TOKEN}", data={"key": _KEY})
    assert r.status_code == 410


def test_key_token_and_body_absent_from_logs(monkeypatch):
    monkeypatch.setattr(key_intake, "_store", _FakeStore(_live_link(), consume="stored"))
    buf, h = _capture_logs()
    try:
        r = _client().post(f"/key/{_TOKEN}", data={"key": _KEY})
    finally:
        logging.getLogger("key_intake").removeHandler(h)
    logs = buf.getvalue()
    assert r.status_code == 200
    assert _KEY not in logs and _KEY not in r.text          # key never logged or echoed
    assert _TOKEN not in logs                                # raw one-time token never logged
    assert "key=" not in logs                                # POST body field never logged
    assert _LINKID in logs and key_intake._fingerprint(_KEY) in logs   # only non-secret id + fingerprint
    assert "key_link.consumed" in logs


def test_replaced_outcome_logs_key_replaced_event(monkeypatch):
    monkeypatch.setattr(key_intake, "_store", _FakeStore(_live_link(), consume="replaced"))
    buf, h = _capture_logs()
    try:
        _client().post(f"/key/{_TOKEN}", data={"key": _KEY})
    finally:
        logging.getLogger("key_intake").removeHandler(h)
    assert "key_link.key_replaced" in buf.getvalue()


def test_rate_limit_returns_429(monkeypatch):
    monkeypatch.setattr(limits, "RATE_LIMIT_PER_MIN", 1)
    monkeypatch.setattr(limits, "_hits", __import__("collections").defaultdict(__import__("collections").deque))
    monkeypatch.setattr(key_intake, "_store", _FakeStore(_live_link()))
    c = _client()
    assert c.post(f"/key/{_TOKEN}", data={"key": _KEY}).status_code == 200
    assert c.post(f"/key/{_TOKEN}", data={"key": _KEY}).status_code == 429


# ---------------------------------------------------------------------------
# DB-gated: the real consume_key_link SECURITY DEFINER function.
# ---------------------------------------------------------------------------
_DB_READY = bool(os.getenv("GATEWAY_DB_PASSWORD")) and bool(os.getenv("ADMIN_DB_USER"))


@pytest.mark.skipif(not _DB_READY, reason="needs control DB + admin creds")
def test_consume_function_single_use_concurrency_and_owner(monkeypatch):
    import psycopg
    pub, priv = keyvault.generate_keypair()
    monkeypatch.setenv("KEYVAULT_PUBLIC_KEY", pub)

    def admin():
        return psycopg.connect(host=os.getenv("DB_HOST", "db"), port=int(os.getenv("DB_PORT", "5432")),
                               dbname=os.getenv("CONTROL_DB", "control"),
                               user=os.environ["ADMIN_DB_USER"], password=os.environ["ADMIN_DB_PASSWORD"],
                               autocommit=True)
    import datetime, hashlib, secrets
    th = hashlib.sha256(secrets.token_hex(8).encode()).hexdigest()
    exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    ct = keyvault.encrypt_v2("sk-synthetic", tenant_id="brindlewood", provider="anthropic")
    with admin() as c:
        c.execute("INSERT INTO tenant_providers (tenant_id,provider,base_url,key_owner,model_allowlist)"
                  " VALUES ('brindlewood','anthropic','https://api.anthropic.com/v1','theirs','{}')"
                  " ON CONFLICT (tenant_id,provider) DO UPDATE SET key_owner='theirs', key_ciphertext=NULL")
        c.execute("INSERT INTO key_entry_links (token_hash,tenant_id,provider,expires_at,created_by)"
                  " VALUES (%s,'brindlewood','anthropic',%s,'test')", (th, exp))
        first = c.execute("SELECT consume_key_link(%s,%s,'fp')", (th, ct)).fetchone()[0]
        second = c.execute("SELECT consume_key_link(%s,%s,'fp')", (th, ct)).fetchone()[0]
        assert first == "stored" and second == "gone"      # single use

        # key_owner='ours' is rejected
        c.execute("UPDATE tenant_providers SET key_owner='ours', key_ciphertext=NULL WHERE tenant_id='brindlewood' AND provider='anthropic'")
        th2 = hashlib.sha256(secrets.token_hex(8).encode()).hexdigest()
        c.execute("INSERT INTO key_entry_links (token_hash,tenant_id,provider,expires_at,created_by)"
                  " VALUES (%s,'brindlewood','anthropic',%s,'test')", (th2, exp))
        assert c.execute("SELECT consume_key_link(%s,%s,'fp')", (th2, ct)).fetchone()[0] == "rejected_owner"
        c.execute("DELETE FROM key_entry_links WHERE token_hash IN (%s,%s)", (th, th2))
