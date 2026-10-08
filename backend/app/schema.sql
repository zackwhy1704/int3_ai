CREATE EXTENSION IF NOT EXISTS vector;

-- Tenant database schema. Applied by app/tenants/admin.py when a tenant is provisioned.
CREATE TABLE IF NOT EXISTS users (
    id     text PRIMARY KEY,
    name   text NOT NULL,
    title  text NOT NULL,
    email  text NOT NULL UNIQUE CHECK (email = lower(email)),
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS scopes (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    kind          text NOT NULL,
    description   text NOT NULL,
    owner_user_id text NOT NULL REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS memberships (
    user_id  text NOT NULL REFERENCES users(id),
    scope_id text NOT NULL REFERENCES scopes(id),
    PRIMARY KEY (user_id, scope_id)
);

CREATE TABLE IF NOT EXISTS documents (
    id             text PRIMARY KEY,
    scope_id       text NOT NULL REFERENCES scopes(id),
    title          text NOT NULL,
    source         text NOT NULL,
    owner          text NOT NULL,
    effective_date date NOT NULL,
    body           text NOT NULL
);

-- How much a source is trusted as the record of a fact: a formal document
-- outranks a chat message, which outranks an unattributed note.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS authority text NOT NULL DEFAULT 'document'
    CHECK (authority IN ('document', 'chat', 'note'));

-- scope_id is copied from the document so retrieval filters on the chunk row itself.
CREATE TABLE IF NOT EXISTS chunks (
    id          serial PRIMARY KEY,
    document_id text NOT NULL REFERENCES documents(id),
    scope_id    text NOT NULL REFERENCES scopes(id),
    ord         int  NOT NULL,
    text        text NOT NULL,
    embedding   vector(384) NOT NULL
);

-- Append-only: a value is never edited. A change inserts a new row and points
-- the old row's superseded_by at it.
CREATE TABLE IF NOT EXISTS claims (
    id              serial PRIMARY KEY,
    subject         text NOT NULL,
    attribute       text NOT NULL,
    value           text NOT NULL,
    quote           text NOT NULL,
    valid_from      date NOT NULL,
    superseded_by   int  REFERENCES claims(id),
    source_chunk_id int  NOT NULL REFERENCES chunks(id),
    scope_id        text NOT NULL REFERENCES scopes(id),
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- NULL condition = the default rule. A claim with a condition ("annual contract")
-- is a different key and never supersedes, or is superseded by, the default.
ALTER TABLE claims ADD COLUMN IF NOT EXISTS condition text;

CREATE OR REPLACE FUNCTION claims_append_only() RETURNS trigger AS $$
BEGIN
    IF (NEW.subject, NEW.attribute, NEW.condition, NEW.value, NEW.quote, NEW.valid_from,
        NEW.source_chunk_id, NEW.scope_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.subject, OLD.attribute, OLD.condition, OLD.value, OLD.quote, OLD.valid_from,
        OLD.source_chunk_id, OLD.scope_id, OLD.created_at) THEN
        RAISE EXCEPTION 'claims are append-only; only superseded_by may be set';
    END IF;
    IF OLD.superseded_by IS NOT NULL THEN
        RAISE EXCEPTION 'claim % is already superseded', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Last real response per (resolved scope set, question). Served, flagged as cached,
-- only when a live model call fails. The key includes the scopes, so one user's
-- cached answer is never served to a user with different access.
CREATE TABLE IF NOT EXISTS answer_cache (
    key        text PRIMARY KEY,
    question   text NOT NULL,
    scopes     text[] NOT NULL,
    response   jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS claims_append_only ON claims;
CREATE TRIGGER claims_append_only BEFORE UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION claims_append_only();
