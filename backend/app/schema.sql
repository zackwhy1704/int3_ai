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
ALTER TABLE documents ADD COLUMN IF NOT EXISTS authority text NOT NULL DEFAULT 'document';
-- 'workflow' = a fact captured in a Hermes workflow and approved by the scope
-- owner (Phase 2); ranked below a formal document. Managed as a named constraint
-- so the allowed set can change idempotently even where the column pre-exists.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_authority_check;
ALTER TABLE documents ADD CONSTRAINT documents_authority_check
    CHECK (authority IN ('document', 'chat', 'note', 'workflow'));

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

-- Phase 2 capture: a fact proposed during a workflow, pending the scope owner's
-- approval. Lives in the TENANT database (the isolation boundary). A pending
-- proposal is NOT a claim and is never retrieved by brain_ask; only the scope's
-- owner may list/approve/reject it. On approval the backend writes a workflow
-- record (a documents row with authority='workflow', one embedded chunk, and a
-- claim pointing at that chunk) so the normal passage-first retrieval path finds
-- and cites it with ZERO changes to answer.py.
--
-- Provenance is server-derived: proposer_user_id and timestamps come from the
-- request principal / server clock, never from the model. session_label is
-- model-supplied DISPLAY-ONLY text and is never used for authorisation.
CREATE TABLE IF NOT EXISTS proposals (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id          text NOT NULL REFERENCES scopes(id),
    subject           text NOT NULL,
    attribute         text NOT NULL,
    condition         text,
    value             text NOT NULL,
    quote             text NOT NULL,
    proposer_user_id  text NOT NULL REFERENCES users(id),
    session_label     text,
    status            text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- free text so it can honestly hold the owner's user id (MCP path) OR
    -- 'operator:<name>' (admin CLI fallback); not an FK for that reason.
    decided_by        text,
    decided_at        timestamptz,
    resulting_claim_id int REFERENCES claims(id)
);
CREATE INDEX IF NOT EXISTS proposals_scope_status ON proposals(scope_id, status);
CREATE INDEX IF NOT EXISTS proposals_proposer_time ON proposals(proposer_user_id, created_at);

-- Self-approval is DENIED by default: a proposer cannot approve their own
-- proposal unless their scope opts in (a solo-owner scope may set this true).
ALTER TABLE scopes ADD COLUMN IF NOT EXISTS allow_self_approval boolean NOT NULL DEFAULT false;

-- The ONLY path that writes a workflow record. SECURITY DEFINER so it runs as its
-- owner (a dedicated writer role, set in admin.apply_capture_grants) — the runtime
-- role has EXECUTE on this and no direct INSERT/UPDATE on documents/chunks/claims.
-- It re-checks ownership, pending status, self-approval and the document-authority
-- 409 rule INSIDE the database, and builds the embedded/retrieved chunk text from
-- the CONFIRMED FIELDS (never the free-form quote, which is stored as evidence
-- only). The caller passes the embedding of that same statement.
CREATE OR REPLACE FUNCTION approve_workflow_proposal(
    p_proposal_id uuid, p_decided_by text, p_supersede_anyway boolean, p_embedding vector(384)
) RETURNS TABLE (claim_id int, document_id text, superseded_claim_id int, title text, statement text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    pr proposals%ROWTYPE;
    v_owner text; v_allow_self boolean; v_proposer_name text; v_statement text;
    v_existing_id int; v_existing_auth text; v_doc_id text; v_title text;
    v_chunk_id int; v_claim_id int; v_today date := current_date;
BEGIN
    SELECT * INTO pr FROM proposals WHERE id = p_proposal_id AND status = 'pending';
    IF NOT FOUND THEN RAISE EXCEPTION 'proposal not pending' USING ERRCODE = 'CB404'; END IF;
    SELECT owner_user_id, allow_self_approval INTO v_owner, v_allow_self FROM scopes WHERE id = pr.scope_id;
    IF v_owner IS DISTINCT FROM p_decided_by THEN
        RAISE EXCEPTION 'not scope owner' USING ERRCODE = 'CB403'; END IF;
    IF p_decided_by = pr.proposer_user_id AND NOT v_allow_self THEN
        RAISE EXCEPTION 'self-approval not allowed' USING ERRCODE = 'CBSLF'; END IF;
    SELECT cl.id, d.authority INTO v_existing_id, v_existing_auth
        FROM claims cl JOIN chunks c ON c.id = cl.source_chunk_id JOIN documents d ON d.id = c.document_id
        WHERE cl.scope_id = pr.scope_id AND cl.subject = pr.subject AND cl.attribute = pr.attribute
          AND cl.condition IS NOT DISTINCT FROM pr.condition AND cl.superseded_by IS NULL;
    IF v_existing_id IS NOT NULL AND v_existing_auth = 'document' AND NOT p_supersede_anyway THEN
        RAISE EXCEPTION 'document-authority conflict' USING ERRCODE = 'CB409'; END IF;
    SELECT name INTO v_proposer_name FROM users WHERE id = pr.proposer_user_id;
    v_proposer_name := coalesce(v_proposer_name, pr.proposer_user_id);
    v_statement := pr.subject || ' — ' || pr.attribute
                   || coalesce(' (' || pr.condition || ')', '') || ': ' || pr.value;
    v_doc_id := 'wf-' || p_proposal_id::text;
    v_title  := pr.scope_id || ' workflow — ' || v_today::text || ' — ' || v_proposer_name;
    INSERT INTO documents (id, scope_id, title, source, owner, effective_date, body, authority)
        VALUES (v_doc_id, pr.scope_id, v_title, 'workflow', v_proposer_name, v_today, v_statement, 'workflow');
    INSERT INTO chunks (document_id, scope_id, ord, text, embedding)
        VALUES (v_doc_id, pr.scope_id, 0, v_statement, p_embedding) RETURNING id INTO v_chunk_id;
    INSERT INTO claims (subject, attribute, condition, value, quote, valid_from, source_chunk_id, scope_id)
        VALUES (pr.subject, pr.attribute, pr.condition, pr.value, pr.quote, v_today, v_chunk_id, pr.scope_id)
        RETURNING id INTO v_claim_id;
    IF v_existing_id IS NOT NULL THEN
        UPDATE claims SET superseded_by = v_claim_id WHERE id = v_existing_id; END IF;
    UPDATE proposals SET status = 'approved', decided_by = p_decided_by, decided_at = now(),
        resulting_claim_id = v_claim_id WHERE id = p_proposal_id;
    RETURN QUERY SELECT v_claim_id, v_doc_id, v_existing_id, v_title, v_statement;
END $$;
