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

## This repo: the local demo
Its only job is to prove three things to the business partner, in this order:
1. **Scoping.** Priya (Operations) and Marcus (Finance) ask "what's our refund window
   for enterprise customers?" and get answers from different source sets. Marcus sees
   a Finance-only document; Priya's answer never cites or paraphrases it.
2. **Citations and refusal.** Every answer links the passages it used. A question with
   no supporting source returns "no reliable source found" and suggests an owner. It
   never invents an answer.
3. **Change tracking.** The refund policy exists in two dated versions (14 → 30 days).
   The answer gives the current value and the previous one, with both dates and both
   verbatim quotes.

**Layout:**
- `backend/`: FastAPI, psycopg, fastembed.
- `web/`: React + TS, served by the Vite dev server.
- `seed/`: synthetic documents, users and test questions.
- `docker-compose.yml` and `README.md`.

**Running it:** `docker compose up --build` is the only command needed.

**Running the tests:** `docker compose exec backend pytest -v` (the stack must be up).

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
  `docker compose exec backend python -m app.calibrate`.
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
  claims are extracted once and baked into `seed/claims.json`. It is not fine in
  general: the product needs canonical conditions.
- **DEMO SIMPLIFICATION, not doctrine: supersede only within the same scope.** This
  avoids showing a user a superseded claim whose replacement is hidden from them. In a
  real company Finance legitimately corrects a company-wide number, so the product
  needs cross-scope supersede with a visibility rule. Don't carry this rule forward.

**Sign-in:** a dropdown of three seeded users:
- Priya (Operations): company-wide + operations.
- Marcus (Finance): company-wide + finance.
- Ada (CEO): company-wide + leadership.

The user id travels in the `X-User-Id` header, and the server resolves the scopes.
`X-User-Id` is demo only — replaces authentication, trivially spoofable, never ships.

**Seed data:** fully synthetic, for the fictional company Brindlewood Supply Co.
Never use a real company's documents, even with permission.

**Build order.** Stop at each gate and report:
1. Compose, schema, seed, and a scope-filtered search endpoint.
2. Answers with structural citations, and refusal below a measured retrieval threshold.
3. Claims extraction and supersede, with the change surfaced in the answer.
4. A minimal web UI: user dropdown, brain list with one locked brain, ask box, and
   an answer with citations and the change flag.

**Tests that gate the demo:**
- **Cross-user permissions.** No answer for user A cites or contains text from a
  scope A isn't in. Must pass 100%.
- **Refusal.** Unsupported questions never produce an answer.
- **Supersede.** A reworded but unchanged fact is not a change; a genuinely changed
  value is. Report the real pass rate over repeated runs, and never retry until it
  goes green.

## Out of scope for the demo — do not build
Authentication and SSO, multi-tenancy or per-tenant databases, a desktop shell (Tauri
or Electron), an agent runtime (Hermes or OpenClaw), connectors to real services or
live ingestion, an Onyx fork, the model gateway or metering, billing, an admin UI, a
conflict-resolution workflow, a design system, the setup wizard, cloud or S3, a
second LLM provider (e.g. Ollama), and nginx or any production web server.

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
