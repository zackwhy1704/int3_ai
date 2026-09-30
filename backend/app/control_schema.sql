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
