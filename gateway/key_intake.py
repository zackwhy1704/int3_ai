"""Single-use BYOK key-entry intake (Phase 2b) — the ONLY internet-facing component that
touches a provider key, and it is ENCRYPT-ONLY.

A tenant admin opens a one-time link (minted by `admin_keys.py mint-key-link`), pastes their
provider key into a no-JS form, and it lands ENVELOPE-ENCRYPTED in `tenant_providers`. The key
never appears in email, chat, logs, or our hands:

  * the intake holds only the keyvault PUBLIC key (keyvault.encrypt_v2) — it cannot decrypt any
    tenant key, and its DB role (key_intake) can only EXECUTE two SECURITY DEFINER functions +
    SELECT non-secret columns (never key_ciphertext);
  * flow is VALIDATE-THEN-CONSUME: rate-limit -> hash lookup (unused/unexpired) -> ONE cheap
    provider check (GET <base_url>/models, no redirects) -> on success, consume_key_link()
    atomically stores it (the atomic consume is the single-use/replay guard); 3 failed
    validations burn the link;
  * nothing secret is logged: events carry link_id = token_hash[:12] and a fingerprint
    sha256(key)[:8] (which the operator re-confirms with the admin out of band), never the token,
    key, or POST body.

MUST NOT be exposed without TLS: the key crosses the wire on POST. Bind to 127.0.0.1 behind the
stack locally; a hosting-phase TLS ingress terminates TLS AND must redact /key/<token> from access
logs. In hosting, keyvault v2 becomes Cloud KMS (encrypter-only IAM here, decrypter on the gateway).
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

import keyvault
import limits

log = logging.getLogger("key_intake")

_PREFIX = "cb_keylink_"
_MAX_FAILS = 3


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fingerprint(key: str) -> str:
    """Non-secret 8-hex fingerprint the operator re-confirms with the admin out of band."""
    return hashlib.sha256(key.encode()).hexdigest()[:8]


# --- DB boundary (overridable in tests) -------------------------------------
class LinkStore:
    """Connects as the least-privilege `key_intake` role. SELECT on non-secret columns only;
    all writes go through the SECURITY DEFINER functions."""

    def _conn(self):
        import psycopg
        return psycopg.connect(
            host=os.getenv("DB_HOST", "db"), port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("CONTROL_DB", "control"),
            user=os.environ["KEY_INTAKE_DB_ROLE"], password=os.environ["KEY_INTAKE_DB_PASSWORD"],
            autocommit=True, connect_timeout=5,
        )

    def lookup(self, token_hash: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT l.tenant_id, l.provider, l.used_at, l.expires_at<=now(), l.fail_count, p.base_url, p.key_owner"
                " FROM key_entry_links l LEFT JOIN tenant_providers p"
                "   ON p.tenant_id=l.tenant_id AND p.provider=l.provider"
                " WHERE l.token_hash=%s", (token_hash,)).fetchone()
        if not row:
            return None
        return {"tenant_id": row[0], "provider": row[1], "used": row[2] is not None,
                "expired": bool(row[3]), "fail_count": row[4], "base_url": row[5], "key_owner": row[6]}

    def record_failure(self, token_hash: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT record_key_link_failure(%s)", (token_hash,)).fetchone()[0]

    def consume(self, token_hash: str, ciphertext: str, fingerprint: str) -> str:
        with self._conn() as c:
            return c.execute("SELECT consume_key_link(%s,%s,%s)", (token_hash, ciphertext, fingerprint)).fetchone()[0]


_store: LinkStore = LinkStore()


def _validate_provider_key(provider: str, base_url: str, key: str) -> bool:
    """ONE cheap check: GET <base_url>/models with the key. No redirects followed (a redirect could
    exfiltrate the key to another host). No tokens billed. Overridable in tests."""
    url = base_url.rstrip("/") + "/models"
    headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"} if provider == "anthropic"
               else {"authorization": f"Bearer {key}"})
    try:
        r = httpx.get(url, headers=headers, timeout=15, follow_redirects=False)
        return r.status_code == 200
    except Exception:
        return False


_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; style-src 'unsafe-inline'",
    "X-Frame-Options": "DENY",
}


def _page(body: str, status: int = 200) -> HTMLResponse:
    html = (f"<!doctype html><meta charset=utf-8><title>Company Brain — provider key</title>"
            f"<body style='font-family:system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem'>{body}</body>")
    return HTMLResponse(html, status_code=status, headers=_HEADERS)


_GONE = "<h1>Link unavailable</h1><p>This key-entry link is invalid, already used, or expired. Ask your operator for a new one.</p>"


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/key/{token}", response_class=HTMLResponse)
    def form(token: str):
        link = _store.lookup(_hash(token))
        if not link or link["used"] or link["expired"]:
            return _page(_GONE, status=410)
        # No JS, no third-party assets; posts to the same path.
        return _page(
            f"<h1>Enter your {link['provider']} API key</h1>"
            f"<p>Tenant <b>{link['tenant_id']}</b>. The key is encrypted on arrival; it is never shown back, "
            f"emailed, or logged. Confirm the fingerprint shown after submit with your operator.</p>"
            f"<form method=post action='/key/{token}'>"
            f"<input type=password name=key autocomplete=off style='width:100%;padding:.6rem' placeholder='sk-...'>"
            f"<button type=submit style='margin-top:1rem;padding:.6rem 1rem'>Encrypt and store</button></form>")

    @app.post("/key/{token}")
    async def submit(token: str, request: Request):
        ip = request.client.host if request.client else "-"
        token_hash = _hash(token)
        link_id = token_hash[:12]
        try:
            limits.check_rate_limit(f"keyintake:{ip}")
            limits.check_rate_limit(f"keyintake:{link_id}")
        except Exception:
            log.warning("key_link.rejected link_id=%s reason=rate_limited", link_id)
            return _page("<h1>Too many attempts</h1><p>Wait a minute and retry.</p>", status=429)

        link = _store.lookup(token_hash)
        if not link or link["used"] or link["expired"]:
            log.info("key_link.rejected link_id=%s reason=gone", link_id)
            return _page(_GONE, status=410)

        form_data = await request.form()
        key = (form_data.get("key") or "").strip()
        if not key:
            return _page("<h1>No key entered</h1><p>Go back and paste your provider key.</p>", status=400)

        # VALIDATE first (never log the key); only a valid key is consumed+stored.
        if not _validate_provider_key(link["provider"], link["base_url"] or "", key):
            n = _store.record_failure(token_hash)
            log.info("key_link.rejected link_id=%s reason=validation_failed fail_count=%s", link_id, n)
            if n < 0 or n >= _MAX_FAILS:
                return _page("<h1>Key rejected</h1><p>This link is now closed after too many attempts. Ask your operator for a new one.</p>", status=403)
            return _page(f"<h1>Key rejected</h1><p>The provider did not accept that key ({n} of {_MAX_FAILS} attempts). Check it and try again.</p>", status=400)

        fp = _fingerprint(key)
        ciphertext = keyvault.encrypt_v2(key, tenant_id=link["tenant_id"], provider=link["provider"])
        outcome = _store.consume(token_hash, ciphertext, fp)
        if outcome == "stored":
            log.info("key_link.consumed link_id=%s provider=%s outcome=stored fingerprint=%s", link_id, link["provider"], fp)
        elif outcome == "replaced":
            log.warning("key_link.consumed link_id=%s provider=%s outcome=replaced fingerprint=%s", link_id, link["provider"], fp)
            log.warning("key_link.key_replaced link_id=%s tenant=%s provider=%s fingerprint=%s",
                        link_id, link["tenant_id"], link["provider"], fp)
        else:  # gone (race lost) or rejected_owner
            log.info("key_link.rejected link_id=%s reason=%s", link_id, outcome)
            return _page(_GONE, status=410)
        return _page(f"<h1>Key stored</h1><p>Your {link['provider']} key is encrypted and in place. "
                     f"Fingerprint <code>{fp}</code> — confirm this with your operator. This link is now closed.</p>")

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz():
        return PlainTextResponse("ok", headers=_HEADERS)

    return app


app = create_app()
