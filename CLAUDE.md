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

## Engineering standards

### Read before writing
- Read the files you will change and their callers before proposing a change. Quote
  file:line when you claim something about the code. Never describe behaviour you have
  not read or run.
- Diagnose before fixing: for a bug, state the observed failure, the cause, and the
  evidence (command output) before editing.
- If a task contradicts an Architectural invariant or a Gate 0 item, stop and say so.
  Do not work around it.

### Architecture: separate I/O from logic (this is what "MVC" means here)
Neither repo is a classic MVC app. The rule that matters is: **transport and I/O at
the edges, pure logic in the middle, so the middle is unit-testable without Docker,
a database, a network, or an LLM.**

| Layer | Owns | Must not |
|---|---|---|
| Edge (FastAPI routes, Tauri commands, MCP tool handlers, React components) | parsing input, auth context, mapping to/from wire format | contain business rules, SQL, or prompt text |
| Service / domain (pure functions, plain classes) | decisions: scope resolution, refusal gate, citation validation, supersede rule, token handling | import FastAPI, psycopg, fetch, Tauri, React |
| Adapters (repositories, LLM client, HTTP clients, Hermes process) | talking to the outside world | make decisions |

- Dependencies point inward. Adapters are passed in (function args, FastAPI
  `Depends`, constructor injection), never imported as module globals inside logic.
- **Do not refactor the existing demo into layers for its own sake.** "No abstraction
  the current task doesn't need" still holds. Apply the layering to new code, and to
  an existing module only when you are already changing it and need a seam to test it.
  Propose any cross-module restructure as a plan first.

### Contracts are generated, never hand-copied
- The backend's FastAPI OpenAPI schema is the single source of truth for every HTTP
  contract. TypeScript types for web/ and int3_desktop are generated from it
  (e.g. `openapi-typescript`) and committed; CI fails if regeneration produces a diff.
- MCP tool input schemas are derived from one zod schema per tool (`McpServer.registerTool`
  with a zod shape), not a hand-written JSON schema alongside a separate zod parser.
- Model IDs, ports and base URLs live in one config location per service, read from
  env. No string literal for a model ID anywhere else.

### Testing standard
Three tiers. Every PR states which tiers it ran, with real output.

| Tier | Runs | Needs | Budget |
|---|---|---|---|
| unit | every save / every PR | nothing external — no Docker, DB, network, LLM | < 30 s per repo |
| integration | every PR in CI | Docker (Postgres+pgvector), mocked LLM | < 5 min |
| eval (`-m llm`) | before a gate, on demand | real Anthropic key | report pass rates, never retry to green |

Coverage — measured, not chased:
- **Changed lines in a PR: ≥ 80 % line coverage** (enforce with `diff-cover` /
  equivalent). This is the gate. A whole-repo number is reported, not enforced.
- **Invariant-critical code: 100 % branch coverage AND a named test per invariant.**
  This list is: scope resolution, retrieval scope filter, citation validation,
  refusal gate, answer-cache keying, claim append/supersede, gateway auth, token
  storage, BrainProvider error mapping. A test name says which invariant it guards
  (`test_cache_key_differs_when_scopes_differ`).
- Coverage of glue (React layout, Tauri `main`, Dockerfiles) is not a goal. Do not
  write tests that only execute lines; every test asserts a behaviour.
- A failing or flaky test is reported as a result. Never delete, skip, loosen an
  assertion or raise a threshold to get green without the owner's explicit OK.

Test design rules:
- Arrange-Act-Assert; one behaviour per test; no logic (loops/ifs) that hides which
  case failed — use parametrisation.
- Mock at adapter boundaries only (LLM client, DB repository, HTTP, Hermes process).
  Never mock the unit under test or the scope-filter SQL itself.
- Every bug fix lands with a test that failed before the fix. Show it failing.
- Security-relevant tests are negative tests: forged token rejected, foreign scope
  absent, out-of-set citation refused.

### Change hygiene
- Small PRs, one concern each. Flag anything you touched outside the planned surface.
- Format + lint + typecheck must pass locally before you report done
  (Python: `ruff check`, `ruff format --check`, `mypy` on touched modules;
  TS: `tsc --noEmit`, `eslint`; Rust: `cargo fmt --check`, `cargo clippy -D warnings`).
- Reports use the ladder: does-not-compile / compiles / unit pass / integration pass /
  ran locally / ran on Windows CI. Claim only the highest rung you actually reached.
- Secrets: never in code, config templates written to disk, logs, or test fixtures.

## int3_ai specifics (backend, gateway, web)

### Layout target (apply as modules are touched)
```
backend/app/
  api/          FastAPI routers: parse, auth dependency, call service, shape response
  services/     answer, claims, scopes, refusal — pure logic, adapters injected
  adapters/     db (repositories, connection pool), llm, embed, rerank
  schema.sql    (migrations tool when multi-tenant work starts, not before)
gateway/
  routes.py  auth.py  translate.py (pure OpenAI<->Anthropic mapping)  upstream.py (httpx)
```
- `translate.py` is pure and has table-driven unit tests for: system messages
  (string and content-part arrays, multiple), tool definitions, assistant
  `tool_calls`, `role: tool` results, streaming tool-call deltas, stop-reason mapping
  (`end_turn→stop`, `max_tokens→length`, `tool_use→tool_calls`), error passthrough.
- Use FastAPI lifespan, not `@app.on_event`. Use a connection pool, not a new
  connection per call.
- Test deps (`pytest`, `pytest-cov`, `diff-cover`, `ruff`, `mypy`) go in a
  `requirements-dev.txt`; tests are not copied into production images.

### Tooling
- `pytest -m unit` (new marker) must run on a laptop with no Docker.
  Unit tests use `app.dependency_overrides` and fake adapters.
- Integration tests keep the existing `TestClient` + Compose DB, with the LLM adapter
  replaced by a deterministic fake. Only `-m llm` touches the real API.
- Add `.github/workflows/ci.yml`: ruff, mypy, unit, integration (Compose service
  container), diff-coverage gate, OpenAPI-types drift check. There is no CI today.

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
- Desktop agent runtime: Hermes Agent (NousResearch, MIT).
  We wrap it; we don't write one. See `int3_desktop`.

**Data and infrastructure:**
- PostgreSQL + pgvector, one engine for rows and vectors.
- One database per customer, not shared tables with row filters.
- S3-compatible object storage in the Singapore region.
- A Postgres-backed job queue, with Redis later.
- Containers and infrastructure-as-code. A new tenant is one scripted step.
- Desktop shell: Tauri (a Rust core with a TypeScript and React UI). Electron is the
  fallback only if Tauri proves painful.
- Web console: TypeScript and React.

## Repositories

| Repo | Purpose |
|---|---|
| `int3_ai` | **This repo** — backend, gateway, web console, Docker/Cloud Run |
| `int3_desktop` | Tauri desktop app, Hermes sidecar, MCP tools, installer |

## Gateway service (`gateway/`)

Every LLM call from any client (web or desktop) is proxied through this service.
Provider API keys live in Google Secret Manager and are never sent to client machines.

**Stack:** Python + FastAPI, exposes a POST `/v1/chat/completions` endpoint
(OpenAI-compatible). Validates the caller's OIDC Bearer token before forwarding.

**Local dev:** runs as a Docker Compose service on port 8001.
**Production:** deployed as a separate Cloud Run service (stateless, scales to zero).

```
gateway/
  main.py          # FastAPI app, /health + /v1/chat/completions
  auth.py          # OIDC Bearer token validation (Gate 4)
  proxy.py         # Translates OpenAI format → Anthropic API, proxies response
  Dockerfile
  requirements.txt
```

**Environment variables:**
- `ANTHROPIC_API_KEY` — injected from Google Secret Manager in production
- `GATEWAY_AUTH=none` — skip token validation in local dev (Gate 2); remove before Gate 4
- `PORT` — defaults to 8001

## Cloud deployment

**Provider:** Google Cloud Run + Cloud SQL (Singapore region, `asia-southeast1`).

**Rationale:** target customers use Google Workspace, so Google OIDC and Drive/Gmail
connectors are native. Cloud Run is simple to deploy (one `gcloud run deploy`),
scales to zero between pilot sessions, and Cloud SQL managed Postgres fits the
per-tenant database model.

**Database:** one Cloud SQL instance, one database per tenant (not one instance per
tenant — too expensive at pilot scale). Tenant databases are created by the control
plane provisioning script.

**Secrets:** all in Google Secret Manager — `ANTHROPIC_API_KEY`, session secrets,
OIDC client credentials.

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
- `gateway/`: OpenAI-compatible LLM proxy (see Gateway section above).
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

## Gate 0: verified blockers

Each item was reproduced or read in source on 2026-10-07. Do not "fix" the
architectural ones (1–3) on your own — write up options and stop.

1. **The Hermes sidecar premise does not hold as written.** Hermes Agent is a Python
   app supported on Linux/macOS/WSL2, not native Windows; there is no standalone
   Windows binary. It is configured through `~/.hermes/config.yaml` (`mcp_servers:`
   mapping) and `API_SERVER_*` env vars, not the JSON shape in
   `agent/hermes_config.json`. `scripts/download-hermes.js` points at an asset name
   that the script itself marks unconfirmed. → Spike: confirm against Hermes's own
   repo/docs, then present options (e.g. desktop calls backend `/api/ask` directly
   for Gates 1–5 and defers an agent runtime to Gate 6; package Python+Hermes;
   another runtime). Owner decides.
2. **The Hermes path breaks the structural-citation invariant.** Through Hermes, the
   final answer is written by an LLM from tool text, and citation is a prompt
   instruction in `brain_search`'s description. The backend already returns answers
   whose citations are schema-constrained. Any design must keep citations structural
   end to end.
3. **Desktop ↔ backend contract does not exist.** `RemoteBrainProvider` calls
   `POST /v1/search`, `GET /v1/claims/{id}`, `GET /v1/sources` with a Bearer token;
   the backend serves `GET /api/search`, `POST /api/ask`, `GET /api/documents/{id}`
   with `X-User-Id`, and takes one `brain_id`, not a scope list. Define the contract
   in the backend first (Part A, generated types).
4. **Gateway does not start in its container.** `gateway/Dockerfile` runs
   `uvicorn main:app` from inside the package; `main.py` uses relative imports →
   `ImportError: attempted relative import with no known parent package`.
5. **Gateway auth fails open.** `GATEWAY_AUTH` defaults to `"oidc"`, whose
   implementation is a stub that accepts any non-empty token (returns
   `"stub-unverified"`). Until real verification exists, any mode other than an
   explicit `none` in local Compose must reject every request.
6. **Gateway drops tool calling.** `tools` is not forwarded and `role: tool` /
   `tool_calls` messages are passed to Anthropic unmodified, so any agent using MCP
   tools through the gateway cannot work. Also: `finish_reason` carries Anthropic
   values, text blocks are joined with spaces, the streaming error frame is not
   valid JSON, and nothing is metered or cached despite the invariant.
7. **Desktop build is broken.** `npm run agent:build` fails with TS5097 (`.ts`
   import extensions without `allowImportingTsExtensions`/rewrite); `tauri.conf.json`
   references `icons/*` that don't exist; the updater is configured under
   `bundle.updater` (v1 shape) — v2 needs `plugins.updater`,
   `bundle.createUpdaterArtifacts`, and the `tauri-plugin-updater` crate/package.
   Hermes config points at `agent/mcp/server.js`, which is never produced or bundled,
   and runs it with `node`, which customer machines won't have.
8. **Token-on-disk contradiction.** Gate 4 plans to write the OIDC token into a
   config file and env vars for child processes; the invariant says never on disk.
   Pick a mechanism that honours the invariant (short-lived token passed in memory,
   or a local token broker) before Gate 4.
9. **Smaller desktop defects:** `main.rs` duplicates `lib.rs` instead of calling
   `run()`; the Hermes child is never killed on app exit; `mutex.lock().unwrap()`;
   `streamChat` splits SSE per network read without buffering partial lines and
   without `decode(…, {stream: true})`.
10. **Model ID drift:** `claude-sonnet-5-5` (backend, .env.example) vs
    `claude-sonnet-4-6` (gateway default, hermes_config). One config source.
11. **Scope check:** int3_ai/CLAUDE.md lists the gateway, a desktop shell and an agent
    runtime as out of scope for the demo, and says never build a second thing before
    the first has users. Both now exist. Confirm with the owner which document is
    current before building further on either.

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
