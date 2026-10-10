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
