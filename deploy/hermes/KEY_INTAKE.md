# BYOK key-entry link — operator runbook (Phase 2b)

A tenant admin enters their own provider key through a **one-time link**; it lands
envelope-encrypted in the gateway and never appears in email, chat, logs, or our hands.

## One-time setup (per deployment)

1. **Generate the keyvault v2 keypair** (asymmetric; intake can only encrypt):
   ```
   python -m keyvault gen-keypair
   ```
   Set `KEYVAULT_PUBLIC_KEY` on the **intake** service only, and `KEYVAULT_PRIVATE_KEY` on the
   **gateway** only. The intake must NOT get the private key or `TENANT_KEY_KEK` — then it cannot
   decrypt any tenant key (proven by `tests/test_keyvault.py::test_intake_holding_only_public_key_cannot_decrypt`).
2. **Provision the `key_intake` DB role:** set `KEY_INTAKE_DB_PASSWORD` and run `admin init`. The
   role gets EXECUTE on `consume_key_link` / `record_key_link_failure` and SELECT on non-secret
   columns only — never `key_ciphertext`.

## Per-tenant flow

1. Ensure a provider row exists for BYOK: `admin_keys.py set-provider --tenant T --provider anthropic
   --base-url https://api.anthropic.com/v1 --key-owner theirs ...`.
2. **Mint a link** (default TTL 2h, hard max 24h):
   ```
   KEY_INTAKE_BASE_URL=https://keys.example python admin_keys.py mint-key-link --tenant T --provider anthropic
   ```
   It prints a single-use URL. If a key is already set it prints a WARNING (using the link REPLACES
   the key and emits `key_link.key_replaced`).
3. **Deliver the link out of band** to the admin (not a shared channel where avoidable). It must be
   opened **over TLS**.
4. The admin opens it, pastes the key into a no-JS form, submits. The intake validates the key with
   one cheap `GET <base_url>/models` (no redirects), then stores it. The page shows a **fingerprint**
   (`sha256(key)[:8]`).
5. **Re-check the fingerprint:** confirm the fingerprint the admin sees matches the one in the
   `key_link.consumed` log line, out of band. This catches a wrong/forwarded link setting the wrong
   key (a forwarded link is **exfiltration**: whoever uses it points the tenant's prompts at *their*
   provider account — short TTL + single use + fingerprint re-check + TLS are the mitigations).

Reuse, expiry, or 3 failed validations → the link is closed (generic "unavailable"); mint a new one.

## Security properties

- **Encrypt-only intake:** holds only the public key; cannot read any stored key.
- **DB authority:** all writes go through SECURITY DEFINER functions owned by a NOLOGIN `key_writer`;
  the intake role can only EXECUTE them + SELECT non-secret columns. `consume_key_link` rejects
  `key_owner='ours'` and is atomic single-use (the replay guard).
- **No secret in logs:** events carry `link_id` (a hash prefix) + the fingerprint, never the token,
  key, or POST body. Responses set `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
  `CSP default-src 'none'; form-action 'self'`, `X-Frame-Options: DENY`.

## TLS — mandatory

**Do not expose the intake without TLS.** The key crosses the wire on POST. Locally, bind it to
loopback behind the stack:
```
KEYVAULT_PUBLIC_KEY=... KEY_INTAKE_DB_ROLE=key_intake KEY_INTAKE_DB_PASSWORD=... \
CONTROL_DB=control DB_HOST=127.0.0.1 \
uvicorn key_intake:app --host 127.0.0.1 --port 8099
```
Hosting phase: a TLS ingress terminates TLS **and must redact `/key/<token>` from access logs**
(the token is in the path). keyvault v2 becomes Cloud KMS — encrypter-only IAM for the intake,
decrypter for the gateway.
