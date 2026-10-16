# Company Brain — project context

## What we are building (current product)

A multi-tenant **Company Brain**: staff ask questions in plain language and get answers
drawn from their company's own documents and workflow history, with citations, scoped to
what each person is allowed to see. The brain also **learns from real work**: a fact that
comes up in a workflow can be proposed, and — only after a human with authority approves
it — captured as a dated, cited claim.

The way it reaches a user's machine is the thing that is new on this branch
(`spike/remote-thin-client`):

```
stock Hermes Desktop (thin client)          the user installs Nous's official Hermes app
   │  one connection (url + session_token)   and points it at a connect file — no custom
   ▼                                          desktop build, "Hermes under the hood"
per-user gated Hermes backend (container)    STOCK Hermes v0.21.7 in our gated container
   │  MCP (per-user brain token)              one isolated container per user; default-deny
   ▼                                          route + egress proxies; startup gate
Company Brain (brain + per-user MCP sidecar) the truth layer; the ONLY thing that holds
   │  OpenAI-compatible /v1/chat/completions  the brain token is the sidecar
   ▼
BYOK LLM gateway (custodian)                 the customer's own model key, metered + capped;
                                             provider keys never reach a client machine
```

Everything the user sees is the **real, stock Hermes agent** — we neither fork nor modify Hermes
(desktop or backend). What is ours is the **gating around stock Hermes** (the per-user container,
the default-deny route + egress proxies, the startup gate), the **brain**, and the **BYOK
gateway** behind it.

## What is genuinely ours (the intellectual property)
1. **Truth layer.** Documents and approved workflow facts become dated claims, append-only.
   A new claim that contradicts a current one supersedes it (old kept, with dates and
   verbatim quotes). Answers "what is true now", "what was true then", "where sources
   disagree".
2. **Capture-with-approval.** A fact proposed during a workflow is a *proposal*, not a
   claim. Only the scope **owner** may approve it; approval writes a workflow-authority
   claim that cites the proposer and the moment. This is the one place the brain grows
   from use, and it is gated by a human every time.
3. **Per-user isolation as a container boundary.** Each user's Hermes backend runs in its
   own container on its own internal network, reaches the brain only through its own MCP
   sidecar with its own token, and egresses only through a default-deny proxy.
4. **BYOK custodian gateway.** Every LLM call is proxied, metered, capped and scoped. The
   customer brings their own provider key; it lives in the gateway, never on a client.
5. **Measured accuracy + refusal.** A golden set and a measured refusal threshold gate
   prompt/model/retrieval changes; the numbers are reported, never guessed.

## Architectural invariants (reflect in code structure, not just comments)
- **Permissions are enforced server-side only.** The client never holds a copy of a
  shared brain. A laptop with a text editor must not be able to read another scope.
- **Scopes are memberships, not ranks** (company / department / project / personal); a
  user sees the union of theirs. Retrieval filters by the asker's scopes **before**
  anything reaches the model, and citations obey the same filter.
- **Citations are structural:** the output schema only admits ids from the retrieved set.
  Never a prompt instruction.
- **The claims table is append-only,** enforced by a DB trigger; only `superseded_by` may
  be set, once.
- **Capture is owner-gated in two load-bearing layers:** the app layer (`_require_owner`
  in `backend/app/capture.py`) and the SECURITY DEFINER DB function
  (`approve_workflow_proposal` in `backend/app/schema.sql`, which also enforces no
  self-approval). The CI demo removes both and asserts the behaviour regresses, then
  restores them (RED→GREEN). No bypass flag ships.
- **The isolation boundary is Postgres + the container network, not application code.**
  One database per tenant (`t_<id>`), reachable only by its own role; one container +
  one MCP sidecar per user; the brain token lives only in the sidecar (provisioning
  asserts it is absent from the Hermes container).
- **Nothing is fine-tuned.** Weights carry no permissions, cannot unlearn a document, and
  cannot honour a deletion request. The model is used only through the gateway with tools.
- **Provider keys never reach a client.** BYOK keys live in the gateway; clients get
  short-lived service tokens (`cb_svc_…`), metered and capped.
- **The refusal threshold is measured** against seeded questions and reported with real
  numbers, never guessed.

## This repo

- `backend/app/`: FastAPI brain. `capture.py` (propose/approve, owner gate), `schema.sql`
  (tenant schema incl. `approve_workflow_proposal`), `tenants/admin.py` (operator
  commands: provision, migrate), `auth/`, `answer.py`, `claims.py`, `retrieve.py`, `db.py`.
- `deploy/hermes/`: **the live system on this branch.** Per-user gated backend + MCP
  sidecar, the route/egress proxies, the startup gate, provisioning, the demo, and the
  component-tier e2e. Start at `deploy/hermes/README.md` and `deploy/hermes/DEMO.md`.
  - `docker-compose.yml` — the brain, gateway, db, egress proxy; per-user pieces are added
    by `provision-user.sh`. Parameterized by `DEPLOY_PROJECT` (default `hermes-deploy`).
  - `demo/` — **the Hermes-tier demo** (`make demo`): drives the real stock Hermes agent
    over `/api/ws` through the full scenario. `hermes_chat.py` is the faithful `/api/ws`
    client; `run_demo.sh` the scenario; `make-demo.sh` the one-command entry;
    `ci_regression.sh` the owner-check RED→GREEN proof.
  - `e2e/` — **component tier (bypasses Hermes)** (`make e2e`): drives the gateway model +
    brain MCP directly, a faster lower-tier check of the same scenario.
  - `connect-desktop.ps1` / `connect-desktop.sh` — point stock Hermes Desktop at a user's
    connect file (Windows / macOS). `smoke-packaged.ps1` — unattended packaged-app smoke.
- `seed/brindlewood/`: the synthetic demo tenant (marcus=finance, priya=operations,
  ada=operations owner). `seed/fixtures/`: synthetic test tenants.
- `gateway/`: the BYOK custodian gateway (OpenAI-compatible `/v1/chat/completions`).

## Running it

- **The demo (recommended):** `make demo` — prereq checks, generates every non-model
  secret, builds, brings up an **isolated** stack (`DEPLOY_PROJECT=hermes-demo`, never
  touches a running `hermes-deploy`), provisions marcus/priya/ada, runs the scenario
  through the real Hermes agent, prints a ✅/❌ table, and leaves the stack up so you can
  open the desktop app. `make demo-down` removes it. See `QUICKSTART.md`.
- **The LLM key is the one secret you provide**, in `deploy/hermes/.env`
  (`ANTHROPIC_API_KEY=…`). You edit that file yourself — never paste a key into chat.
- **Component-tier check:** `make e2e` (bypasses Hermes; `DEPLOY_PROJECT=hermes-e2e`).

## Desktop: the stock app
- **Pilots run Nous's official, stock Hermes Desktop** pointed at a per-user connect file. We do
  not build, fork, or ship a desktop. The desktop launches its own local `hermes serve` backend
  for *local* profiles (`apps/desktop/electron/backend-command.ts`), but when the active
  connection is our **remote** gated backend it routes to that backend and does not spawn a local
  child for it (`connection-config.ts:886-892`, `backend-start-failure.ts:28`) — the remote
  connection is the agent in the path. **No desktop fork is decided.**
- **Windows is the current target.** The connect flow is host-only (127.0.0.1 forwarder); a
  **hosted TLS pilot is the next step once the cloud account exists** (not paused by decision —
  just not built yet).

## How we work
- **Plan before large changes.** Anything touching token issuance, scope filtering, the
  owner gate, or key handling: the owner reviews the plan, not the diff.
- **Evals/tests gate merges.** Cross-user/cross-tenant permission tests pass 100%; refusal
  tests pass. The capture owner gate is enforced in two layers (by design) and the demo shows it
  blocking a non-owner on the real agent (N1a/N1b); the CI RED→GREEN regression that would *prove*
  it load-bearing is **UNVALIDATED** (manual-only; never run to a full GREEN→RED→GREEN).
- **Reports** state which of "compiles / unit tests pass / ran locally / ran in CI"
  applies to each claim, with real command output and `file:line`. A measured failure is a
  result; a tuned pass is not. If something doesn't work, say so, and say what you did not
  inspect.
- **Keep changes surgical.** No abstraction the current task doesn't need. **Do not modify
  Hermes** — the whole point is that it is the stock agent.
- **Synthetic data only.** Never a real company's documents, even with permission, and
  never real customer data in logs, fixtures or error reports.

## Out of scope for the spike — do not build
Self-serve signup, billing, SaaS onboarding, a second LLM provider wired into a tenant
allowlist (the gateway's OpenAI path is built + unit-tested but not evaluated, not
offered), connectors, a design system, a custom desktop build.
