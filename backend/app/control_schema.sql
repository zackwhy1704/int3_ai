-- Control plane: which tenant a verified identity belongs to, and live sessions.
-- Holds no customer documents.

CREATE TABLE IF NOT EXISTS tenants (
    id            text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9_]{1,38}$'),
    name          text NOT NULL,
    -- Per-tenant sign-in policy, editable without a code change
    -- (python -m app.tenants.admin configure ...).
    google_domain text,   -- if set, Google sign-ins must carry this hosted domain (hd)
    ms_tenant_id  text,   -- Microsoft sign-ins are accepted only from this Entra tenant
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Pilot constraint: one email belongs to exactly one tenant (email is the key).
-- The provider subject is bound on first sign-in and must match afterwards, so a
-- changed or spoofed email claim cannot take over an existing identity.
CREATE TABLE IF NOT EXISTS identities (
    email       text PRIMARY KEY CHECK (email = lower(email)),
    tenant_id   text NOT NULL REFERENCES tenants(id),
    user_id     text NOT NULL,
    google_sub  text UNIQUE,
    ms_subject  text UNIQUE,   -- "<tid>:<oid>"
    invited_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS login_attempts (
    state         text PRIMARY KEY,
    nonce         text NOT NULL,
    code_verifier text NOT NULL,
    provider      text NOT NULL,
    expires_at    timestamptz NOT NULL,
    used_at       timestamptz
);

-- Only the SHA-256 of the session id is stored.
CREATE TABLE IF NOT EXISTS sessions (
    id_hash     text PRIMARY KEY,
    email       text NOT NULL REFERENCES identities(email) ON DELETE CASCADE,
    tenant_id   text NOT NULL REFERENCES tenants(id),
    user_id     text NOT NULL,
    csrf_token  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);

-- Service tokens: non-interactive bearer credentials for the /v1/ machine
-- surface (the MCP server). A token maps to exactly one tenant + acting user;
-- scopes are still resolved per-request from that user's memberships, so the
-- token's reach is never broader than the user's. Only the SHA-256 of the token
-- is stored, as with sessions. No session row is created — the token resolves
-- straight to (tenant, user). Revoked by setting revoked_at (kept, not deleted,
-- for audit).
CREATE TABLE IF NOT EXISTS service_tokens (
    token_hash  text PRIMARY KEY,
    tenant_id   text NOT NULL REFERENCES tenants(id),
    user_id     text NOT NULL,
    label       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

-- Per-tenant provider configuration for the gateway (BYOK custodian).
-- One row per (tenant, provider): the provider's base_url, who owns the key
-- (ours|theirs), the model allowlist, the envelope-encrypted provider key, and
-- a daily spend cap (mandatory when key_owner='ours' — enforced by the gateway,
-- not a DB constraint, so a bundled tenant with no cap refuses to serve).
-- key_ciphertext is an envelope blob (gateway/keyvault.py); the plaintext key
-- is never stored here. The gateway reads this table (SELECT only) and decrypts
-- with the KEK from its env; the admin CLI (operator creds) writes it.
CREATE TABLE IF NOT EXISTS tenant_providers (
    tenant_id       text NOT NULL REFERENCES tenants(id),
    provider        text NOT NULL,
    base_url        text NOT NULL,
    key_owner       text NOT NULL CHECK (key_owner IN ('ours', 'theirs')),
    model_allowlist text[] NOT NULL DEFAULT '{}',
    key_ciphertext  text,
    daily_cap_usd   numeric,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, provider)
);

-- Retrieval-bound citation tracking (A4).
-- Stores the set of claim_ids and chunk_ids returned by /v1/search or
-- /v1/brain_ask, keyed to session_id + tenant_id. The /v1/validate endpoint
-- checks that a cited claim_id is in the retrieval set for the current
-- session, preventing hallucinated or out-of-retrieval citations.
--
-- Security: session_id and tenant_id bind the retrieval to the principal
-- that performed it. A different session or tenant cannot validate against
-- this row (tested by test_v1_validate_foreign_retrieval,
-- test_v1_validate_foreign_principal).
CREATE TABLE IF NOT EXISTS retrievals (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  text NOT NULL,
    tenant_id   text NOT NULL,
    claim_ids   integer[] NOT NULL,
    chunk_ids   integer[] NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL DEFAULT now() + interval '1 hour'
);
CREATE INDEX IF NOT EXISTS retrievals_session ON retrievals(session_id, tenant_id);
CREATE INDEX IF NOT EXISTS retrievals_expires ON retrievals(expires_at);

-- Per-tenant daily spend counter (item e: per-tenant daily cap).
-- The gateway reads this before each call (fail-fast 429 when the tenant has
-- reached its daily_cap_usd) and increments it after, so the cap survives a
-- gateway restart — an in-memory counter would reset and let spend run past the
-- cap, which matters most when key_owner='ours' (we pay).
--
-- spend_usd is an ESTIMATE from an approximate per-model price table: the cap is
-- a guardrail and this feeds the client-facing usage view, not an invoice.
--
-- Privilege: this is the ONLY control-DB table the least-privilege gateway role
-- may write (GRANT SELECT, INSERT, UPDATE in admin.init()). The gateway still
-- holds no write on service_tokens or tenant_providers — a compromised gateway
-- can inflate a usage row but cannot mint a token or rewrite a key/allowlist
-- (enforced at boot by service_auth.assert_least_privilege).
CREATE TABLE IF NOT EXISTS tenant_usage (
    tenant_id       text NOT NULL REFERENCES tenants(id),
    day             date NOT NULL,
    spend_usd       numeric NOT NULL DEFAULT 0,
    request_count   integer NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, day)
);

-- Single-use BYOK key-entry links (Phase 2b). A one-time token (sha256 at rest, like
-- service_tokens) lets a tenant admin POST their provider key to the ENCRYPT-ONLY intake
-- service over TLS. No token and no key are stored here — only the hash + non-secret metadata.
-- The intake never writes this table or tenant_providers directly: it calls the SECURITY DEFINER
-- functions below (owned by the NOLOGIN key_writer role), so a compromised intake can neither
-- read a key (it holds only the keyvault PUBLIC key) nor bypass the owner/single-use checks.
CREATE TABLE IF NOT EXISTS key_entry_links (
    token_hash  text PRIMARY KEY,
    tenant_id   text NOT NULL REFERENCES tenants(id),
    provider    text NOT NULL,
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    fail_count  integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text NOT NULL
);
CREATE INDEX IF NOT EXISTS key_entry_links_expires ON key_entry_links(expires_at);

-- NOLOGIN role that OWNS (and thus whose privileges back) the SECURITY DEFINER functions.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'key_writer') THEN CREATE ROLE key_writer NOLOGIN; END IF;
END $$;
GRANT SELECT, UPDATE ON key_entry_links TO key_writer;
GRANT SELECT, UPDATE (key_ciphertext, updated_at) ON tenant_providers TO key_writer;

-- validate-then-consume: the intake validates the key FIRST, then calls this to store it
-- atomically. Locks the link row; requires it unused + unexpired; requires the provider row with
-- key_owner='theirs' (rejects 'ours'); sets used_at; writes the v2 ciphertext. Returns the
-- outcome so the caller can log the event — never the key. SET search_path pins resolution.
CREATE OR REPLACE FUNCTION consume_key_link(p_token_hash text, p_ciphertext text, p_fingerprint text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_tenant text; v_provider text; v_had boolean;
BEGIN
  SELECT tenant_id, provider INTO v_tenant, v_provider FROM key_entry_links
    WHERE token_hash = p_token_hash AND used_at IS NULL AND expires_at > now() FOR UPDATE;
  IF NOT FOUND THEN RETURN 'gone'; END IF;                 -- used, expired, or no such link
  SELECT (key_ciphertext IS NOT NULL) INTO v_had FROM tenant_providers
    WHERE tenant_id = v_tenant AND provider = v_provider AND key_owner = 'theirs' FOR UPDATE;
  IF NOT FOUND THEN RETURN 'rejected_owner'; END IF;       -- no provider row, or key_owner='ours'
  UPDATE key_entry_links SET used_at = now() WHERE token_hash = p_token_hash;
  UPDATE tenant_providers SET key_ciphertext = p_ciphertext, updated_at = now()
    WHERE tenant_id = v_tenant AND provider = v_provider;
  RETURN CASE WHEN v_had THEN 'replaced' ELSE 'stored' END;
END $$;

-- Count a failed validation; burn (mark used) the link after 3. Returns the new count, or -1
-- if the link is already gone.
CREATE OR REPLACE FUNCTION record_key_link_failure(p_token_hash text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_n integer;
BEGIN
  UPDATE key_entry_links SET fail_count = fail_count + 1
    WHERE token_hash = p_token_hash AND used_at IS NULL AND expires_at > now()
    RETURNING fail_count INTO v_n;
  IF NOT FOUND THEN RETURN -1; END IF;
  IF v_n >= 3 THEN UPDATE key_entry_links SET used_at = now() WHERE token_hash = p_token_hash; END IF;
  RETURN v_n;
END $$;

ALTER FUNCTION consume_key_link(text, text, text) OWNER TO key_writer;
ALTER FUNCTION record_key_link_failure(text) OWNER TO key_writer;
REVOKE EXECUTE ON FUNCTION consume_key_link(text, text, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION record_key_link_failure(text) FROM PUBLIC;
