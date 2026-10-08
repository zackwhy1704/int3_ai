# Company Brain — project context

## What we are building
A "company brain": staff ask a question in plain language and get an answer drawn
from their company's own documents and chat history, with citations.

The market is crowded (Glean at ~US$300M ARR; Microsoft and Google bundle their own;
Onyx is free and open source), so we compete on one narrow promise:

> "Glean finds your documents. We tell you which one is still true."

Only two things are genuinely ours to build:
1. **Truth layer.** Documents are extracted into dated claims, append-only. When a new
   claim contradicts a current one, the old one is superseded, not overwritten. Both
   are kept with their dates and verbatim quotes. The product can answer "what is true
   now", "what was true then" and "where do our sources disagree".
2. **Measured accuracy.** Each customer supplies ~30 real questions with correct answers.
   That golden set gates every prompt, model and retrieval change, and the score is
   shown to the customer.

Everything else (connectors, chunking, hybrid search, permission sync) is commodity:
borrow it, don't write it.

**Customers:** Southeast Asian companies of 20–500 staff, with knowledge scattered
across Google or Microsoft files, chat tools and WhatsApp. Sold to operations leaders.
Singapore-hosted. Deployed in days, not the 8–14 weeks of an enterprise rollout.

**Team:** one part-time engineer (nights and weekends) and a business partner who
sells. No funding. Borrow aggressively, keep the operable surface small, and never
build a second thing before the first has users.

## Architectural invariants (not negotiable — reflect them in code structure, not just comments)
- PERMISSIONS ARE ENFORCED SERVER-SIDE ONLY. The client never holds a copy of
  a shared brain. A laptop with a text editor must not be able to read another
  scope.
- Scopes are MEMBERSHIPS, not ranks: company-wide, department, project,
  personal. A user sees the union of theirs. A department head sees their own
  department, not every department.
- Retrieval filters by the asker's scopes BEFORE anything reaches the model.
  Citations obey the same filter — an answer that hides its sources is a leak
  waiting to be reverse-engineered.
- Citations are STRUCTURAL: the output schema only admits ids from the
  retrieved set. Never a prompt instruction.
- The claims table is append-only, enforced by a database trigger. Only
  superseded_by may be set, once.
- Same-vs-changed is decided by a SEPARATE, narrow binary call on two value
  strings — never folded into extraction. (In a prior project, folding it in
  produced 57% false "changed" verdicts on reworded facts; a dedicated call
  fixed it.)
- Answer caching keys on the asker's resolved scope set, never on question
  text alone.
- Nothing is fine-tuned. Weights carry no permissions, cannot unlearn a
  document, and cannot honour a deletion request.
- Deletion propagates: remove a source and its chunks, embeddings and derived
  claims go within one sync cycle.
- The retrieval similarity threshold governing refusal is MEASURED against the
  seeded questions and reported to the owner with the real numbers, never guessed.
  Report the similarity of true hits against noise, then recommend a value. (In a
  prior project the guessed value was wrong: true hits scored 0.62, noise 0.23.)

## Target product stack (not the demo)
**Own (the intellectual property):**
- Truth layer and claim extractor: a Python service.
- Control plane: tenants, users, scopes, licences and short-lived token issuance.
  Python and FastAPI.
- Model gateway: every LLM call is proxied, metered and cached. Provider keys never
  reach client machines.
- Eval harness: golden sets, the change-judge suite, and refusal and permission tests.

**Borrow:**
- Ingestion, connectors, hybrid search and permission sync: a fork of Onyx (MIT).
  - It is forked, not vendored.
  - Our services live outside the fork, so upstream merges stay clean and
    ownership is unambiguous.
- Desktop agent runtime: an open-source MIT project (Hermes Agent or OpenClaw).
  We wrap it; we don't write one.

**Data and infrastructure:**
- PostgreSQL + pgvector, one engine for rows and vectors.
- One database per customer, not shared tables with row filters.
- S3-compatible object storage in the Singapore region.
- A Postgres-backed job queue, with Redis later.
- Containers and infrastructure-as-code. A new tenant is one scripted step.
- Desktop shell: Tauri (a Rust core with a TypeScript and React UI). Electron is the
  fallback only if Tauri proves painful.
- Web console: TypeScript and React.

## This repo: the hosted pilot
The local partner demo is finished and lives at git tag `demo-v1` (it proved scoping,
citations with refusal, and change tracking). This branch line builds the hosted pilot:
one real customer, real documents, real users, in gates 5–9, stopping at each for review:
5. Real identity and tenancy. 6. Getting real documents in. 7. Deploy (Singapore).
8. Running it for someone else (admin, golden set, usage). 9. Operations.

**Layout:**
- `backend/app/`: FastAPI. `auth/` (sign-in, sessions, `principal`), `tenants/admin.py`
  (operator commands), `db.py` (connections), `answer.py`, `claims.py`, `retrieve.py`.
- `web/`: React + TS, served by the Vite dev server (proxies `/api`).
- `seed/brindlewood/`: the synthetic demo tenant. `seed/fixtures/`: synthetic test tenants.
- `docker-compose.yml`, `preflight.sh`, `README.md`.

**Running it:** `docker compose --profile dev up -d` (includes the local test sign-in
server). Operator commands and tests run in the tools container, the only place with
Postgres superuser credentials: `docker compose run --rm tools <command>`.

**Running the tests:** `docker compose run --rm tools pytest -v` (stack up; `-m "not llm"`
skips model calls). Provision a tenant:
`docker compose run --rm tools python -m app.tenants.admin provision --id acme --name "Acme" --admin-email ops@acme.example`.

**Identity and tenancy (gate 5):**
- The demo's `X-User-Id` header is removed from every code path (gate 5). Identity comes
  only from Google or Microsoft sign-in (OIDC code flow + PKCE, state bound to the
  browser, nonce; ID token verified against the provider's JWKS, RS256 only).
- Every request: session cookie → control DB session → tenant → tenant DB (as the
  tenant's own role) → active user → memberships → scopes. Nothing else the client
  sends is consulted. Scopes are re-resolved per request, so removals apply next request.
- **The isolation boundary is Postgres, not application code.** One database per tenant;
  CONNECT revoked from PUBLIC; each tenant DB accepts only its own role `t_<id>`; the
  API's control role cannot open tenant DBs. Tenant roles have SELECT on content and
  write only the answer cache. The API process never holds superuser credentials.
  Tests in `test_db_boundary.py` are the first line; code-level tests are the second.
- Microsoft: never trust the email claim alone (nOAuth). The tenant names its Entra
  tenant (tid); identities bind to `tid:oid` (Google: `sub`) at first sign-in and must
  match afterwards.
- Sessions: server-side, SHA-256 of the id stored, `__Host-` httpOnly Secure SameSite=Lax
  cookie, 30 min idle / 8 h absolute. CSRF: per-session token header plus Origin check
  on every state-changing request.
- **PILOT CONSTRAINT, not permanent design: one email belongs to exactly one tenant**
  (email is the identity key). Chosen because supporting several tenants per email needs
  a tenant picker and widens the attack surface; revisit when a real user needs it.
- Per-tenant sign-in policy (`google_domain`, `ms_tenant_id`) is data, set with
  `python -m app.tenants.admin configure`, never a code change.
- The local test sign-in server (navikt/mock-oauth2-server) is dev/test only; the backend
  refuses to start with it configured when `ENV=production`.

**Decisions:**
- **LLM: one provider, Claude via the Anthropic API.** There is no local-model switch.
  Two model paths would give the supersede test two different pass rates, and you
  couldn't tell which to believe.
- **Embeddings: fastembed with `BAAI/bge-small-en-v1.5` (384 dimensions), run
  locally.** Chosen over an API embedding model so search works with no key and no
  network. The model downloads when the image is built, and every retrieval
  experiment is free and repeatable. It governs retrieval quality, so revisit it if
  the recall of true hits is poor.
- **Refusal: gated on a cross-encoder (`Xenova/ms-marco-MiniLM-L-6-v2`), threshold 0.**
  Embedding cosine didn't separate supported from unsupported questions (gap +0.033),
  while the cross-encoder did (gap +9.47 logits). Re-measure with
  `docker compose run --rm tools python -m app.calibrate`.
- **Claim key is (scope, subject, attribute, condition).** A conditional claim never
  supersedes an unconditional one, in either direction; they coexist.
- **Claim-selection rule: authority, not order of arrival.** Each source has an
  authority: a formal document outranks a chat message, which outranks an
  unattributed note. When sources dated the same day state the same value, the
  highest-authority one is the claim of record and the others are "unchanged".
  Known limit: a later formal restatement of a value first announced in chat doesn't
  take over the record, because the claims table is append-only and it isn't a change.
- **Known key instability: condition text varies between runs.** The same passage
  produced condition "board members travelling to london" in 9 runs out of 10 and
  "london, board members" in the other. Different text means a different key, so a
  later claim can miss the one it should supersede. This is fine for the demo, where
  claims are extracted once and baked into `seed/<tenant>/claims.json`. It is not fine in
  general: the product needs canonical conditions.
- **DEMO SIMPLIFICATION, not doctrine: supersede only within the same scope.** This
  avoids showing a user a superseded claim whose replacement is hidden from them. In a
  real company Finance legitimately corrects a company-wide number, so the product
  needs cross-scope supersede with a visibility rule. Don't carry this rule forward.

**Seed and test data:** fully synthetic. Never a real company's documents, even with
permission, and never real customer documents in logs, error reports or fixtures.

**Tests that gate every merge:**
- **Cross-user and cross-tenant permissions,** against real sign-in. Must pass 100%.
- **Refusal.** Unsupported questions never produce an answer.
- **Supersede.** Report the real pass rate over repeated runs; never retry until green.

## Out of scope for the pilot — do not build
Self-serve signup, billing, multi-tenant SaaS features, a desktop shell (Tauri or
Electron), an agent runtime, an Onyx fork, the model gateway, connectors other than the
one gate 6 names, a second LLM provider, a design system.

## How we work
- **Plan before large changes.** For anything touching token issuance, scope
  filtering or key handling, the owner reviews the plan, not the diff.
- **Evals gate merges.** Golden sets must hold, cross-user permission tests must pass
  100%, and refusal tests must pass.
- **Reports** state which of "compiles / unit tests pass / ran locally / ran in a
  browser" applies to each claim, with real command output. A measured failure is a
  result; a tuned pass is not. If something doesn't work, say so.
- **At each gate,** report the commit history, the diff scope (flagging anything
  outside the planned surface) and real command output.
- **Keep changes surgical and simple.** No abstraction the current task doesn't need.

## Integration decisions

D1. Approved: branch `integrate` from origin/pilot/gate-5 (e390924).
D2. Desktop (int3_desktop) is paused until the token plan in Phase B is approved.
D3. JWT library: one shared verifier on authlib, upgraded to latest release (≥1.6.12). python-jose is removed from every requirements file. Do not use jose.
D4. Gate 5 acceptance happens on `integrate`, after Phase A, via the evidence pack in step A6.
