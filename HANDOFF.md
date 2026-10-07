# Handoff: continuing Company Brain on a Windows machine

Written 2026-10-07, for the next Claude Code session (and the owner). Read `CLAUDE.md` first:
it holds the product brief, the architectural invariants and the decisions made with
their reasons. This file covers the current state, what was decided after the last
commit, and what to do next.

## 1. Where the code stands

| Ref | What it is |
|---|---|
| `main` = tag `demo-v1` (`dc70b64`) | The finished local partner demo (dropdown "sign-in" via an `X-User-Id` header, synthetic data). Still runs: `git checkout demo-v1 && docker compose up -d`. |
| `pilot/gate-5` (`17fe8dd`), current branch | Gate 5 of the hosted pilot: real identity and tenancy. |

There is **no git remote yet**. The `demo-v1` tag has never been pushed anywhere.

**Gates done and accepted by the owner:**
- **Demo gates 1–4 and demo hardening:**
  - scoped search;
  - answers whose citations can only point at retrieved passages;
  - refusal decided by a cross-encoder threshold;
  - an append-only claims table with condition-aware supersede and authority ranking;
  - a minimal web UI, a cached fallback when the model is unreachable, and `preflight.sh`.

**Gate 5 (built, not yet reviewed by the owner):**
- **`X-User-Id` removed from every code path.**
- **Google and Microsoft sign-in.** OIDC authorisation-code flow with PKCE, a state value bound to the browser, and a nonce; RS256 ID tokens are verified against the provider's published keys (JWKS).
- **Identity binding.** Microsoft accounts bind to `tid:oid` (protection against "nOAuth" email spoofing); Google accounts bind to `sub`.
- **Sessions.** Server-side; only the SHA-256 of the session id is stored. `__Host-` httpOnly cookie, 30 minutes idle and 8 hours absolute. CSRF token plus Origin check on every state-changing request.
- **Tenancy.** A control database plus one Postgres database per tenant (`t_<id>`), each reachable only by its own role. Postgres is the isolation boundary: CONNECT is revoked from PUBLIC, and tenant roles can read content and write only the answer cache. The API process never holds superuser credentials; those exist only in the `init` and `tools` containers.
- **Provisioning.** `python -m app.tenants.admin provision|configure|init`. A failed provision removes everything it created.
- **Tests: 72 passed against real sign-in.** Every signed-in test runs the full flow through a local test identity provider (`navikt/mock-oauth2-server`, dev profile only; the backend refuses to start with it configured when `ENV=production`).
  - The fixtures are three synthetic tenants: A `brindlewood`; B `hollowmere`, whose document ids and scope names collide with A's; and C `brindlewood_labs`, which shares A's email domain.
  - Suites: database boundary, cross-tenant, sign-in rules, ID-token forgery, CSRF, scoping, refusal, cache, supersede, plus a repo-wide grep for `X-User-Id`.
- **Shown working:**
  - raw `curl` breach attempts, where a valid tenant-A session reaching for B's and C's documents, brains or scope names gets 404;
  - Postgres refusing cross-tenant connections outright;
  - the sign-in flow walked in a browser.

**Not done in gate 5:**
- **Real Google and Microsoft accounts have never been tested,** because the owner has not yet provided OAuth client credentials. Everything so far went through the test identity provider.
- **Push the tag:** waits on a git remote.

## 2. Running it on Windows

Prerequisites:
- Docker Desktop (WSL2 backend is fine for a development machine).
- Git for Windows (which includes Git Bash, needed for `preflight.sh`).

1. **Line endings first.** The `.sh` and `.sql` files and the seed data must keep LF line endings. Before cloning, or before checking out again, run `git config --global core.autocrlf false`. Consider adding a `.gitattributes` with `* text=auto eol=lf`.
2. **Create `.env` from `.env.example`.** Never copy the old `.env` across: it holds a live Anthropic key and database secrets. Generate new secrets, e.g. in PowerShell:
   `-join ((1..32) | % { '{0:x2}' -f (Get-Random -Max 256) })`
   Fill in `ADMIN_DB_PASSWORD`, `APP_CONTROL_PASSWORD`, `TENANT_DB_KEY` and `ANTHROPIC_API_KEY`. Leave the `MOCK_OIDC_*` values as in the example.
3. Start the stack: `docker compose --profile dev up -d --build`
4. Provision the demo tenant:
   `docker compose run --rm tools python -m app.tenants.admin provision --id brindlewood --name "Brindlewood Supply Co." --seed /seed/brindlewood --google-domain brindlewood.example`
5. Run the tests: `docker compose run --rm tools pytest -v`. This provisions the three test tenants. Expect **72 passed**; use `-m "not llm"` to skip model calls.
6. Run `./preflight.sh` from Git Bash. Expect `PREFLIGHT: ALL PASS`.
7. Use it in a browser:
   - Open http://localhost:5173 and choose "Continue with Test Google".
   - Username: `google-priya`.
   - Claims: `{"email":"priya@brindlewood.example","email_verified":true,"hd":"brindlewood.example"}`.

**Recording results:** if any of these steps behaves differently on Windows, record it as a result; don't tune around it.

## 3. Measured results worth knowing (details in CLAUDE.md)

- **Refusal threshold.**
  - Embedding cosine could not separate supported from unsupported questions: a gap of +0.033 with bge-small, and +0.048 with bge-base.
  - A cross-encoder (`ms-marco-MiniLM-L-6-v2`) could: supported 2.56–10.00 against unsupported −11.43 to −6.91, a gap of +9.47 logits.
  - The threshold is 0. That was measured on 21 questions (`python -m app.calibrate`).
- **Supersede suite, 10 runs per pair:** 120/120 after one fix to the extractor prompt. Conditions that qualify *people* (e.g. "for board members") now count as conditions; before the fix, conditional-hotel failed 0/10.
- **Known instability:** condition text varies between runs ("board members travelling to london" 9 runs out of 10, "london, board members" once). That is acceptable while claims are baked into `seed/<tenant>/claims.json`; it is not acceptable in general.
- **Model API quirks** (`anthropic==1.9.0`, `claude-sonnet-5-5`):
  - a forced tool call is rejected;
  - `temperature` is rejected;
  - structured output therefore uses `output_config.format` with a JSON schema.

## 4. Direction decided after the gate 5 commit (NOT yet written into CLAUDE.md)

The owner discussed the delivery surface at length and chose to build:
**a desktop app with a full local agent, Windows first.**
- Most clients are on Windows Dell laptops; Mac comes second.
- The owner considered and rejected, for now, a zero-install web app. The comparison is useful context, so it is kept in the fallback list below.

**Update CLAUDE.md before building.** It still lists "desktop shell" and "agent runtime" as out of scope. Get the owner's confirmation of the wording.

Verified facts that shaped this (October 2026; re-verify because the projects move fast):
- **Hermes Agent** (NousResearch, MIT licence, Python):
  - Its quickstart and installation guide document **native Windows** (PowerShell installer; installs per-user to `%LOCALAPPDATA%\hermes\`; bundles pinned Python, Node, Git, ripgrep and FFmpeg). Its FAQ still says WSL2 only. The docs disagree, so treat it as unproven until it runs on a real Dell.
  - Supports custom model endpoints in the OpenAI API format, and tools over MCP.
  - Stores conversations, memory and skills locally.
- **OpenClaw** (MIT licence, Node): native Windows, a third-party Windows desktop packaging exists, and it supports many messaging channels. Its model-endpoint and MCP support were not yet verified.

**Plan agreed in discussion:**
1. **Agent spike, about one weekend, on a real x64 Windows machine.** Run the same checklist against Hermes and OpenClaw:
   - installs with no admin rights;
   - runs as a background process our app starts and stops;
   - uses our gateway as its model endpoint;
   - calls our "ask the brain" tool over MCP;
   - local memory can be switched off or relocated;
   - Windows Defender doesn't flag it.

   Keep whichever passes, and pin its version.
2. **Finish gate 5 with real Google and Microsoft sign-in,** once the owner supplies credentials.
3. **Model gateway:** an OpenAI-format endpoint on the backend that checks the user's token, rate-limits, meters, and holds the Claude key. Invariant: provider keys never reach client machines.
4. **Desktop sign-in.** This is the token-issuance path, so **show the owner the plan before writing code.**
   - The system browser handles the normal Google/Microsoft sign-in.
   - The backend sends a one-time code back to the app through a `companybrain://` deep link (a loopback address is the fallback).
   - The app exchanges the code using PKCE for a short-lived access token (about 15 minutes) plus a refresh token capped at 8 hours.
   - Tokens live in the Windows credential store; revocation is checked server-side on every request.
   - The cross-user and cross-tenant suites must also pass through this path.
5. **Tauri shell** around the existing React UI. WebView2 is already present on Windows 10 and 11.
6. **Bundle the agent as a background process:**
   - point it at the gateway;
   - give it the brain tool over MCP;
   - switch off or relocate its local memory, so company content doesn't persist on laptops (invariant: clients never hold a copy of a shared brain);
   - **no terminal or PowerShell tool in the pilot.** Endpoint security software treats an agent running PowerShell as malware behaviour.
7. **Build and installer:**
   - build with GitHub Actions on Windows (needs the git remote);
   - a per-user installer plus an MSI, so client IT can deploy it through Intune;
   - sign the code. An OV certificate with its key in a cloud store, or Microsoft's cloud signing service if Singapore is eligible. EV certificates no longer skip the SmartScreen reputation check.
   - Ship a pinned, signed copy of the agent. Never run its `iex (irm …)` script on client machines.
8. **Hosted backend in Singapore** (the safety subset of gate 7). Recommended stack:
   - Google Cloud: Cloud Run plus Cloud SQL (with pgvector) plus Secret Manager;
   - keep one instance always warm, because the image loads two machine-learning models;
   - the per-tenant role and database model works on Cloud SQL;
   - backups and a restore drill that is actually tested and timed.

   Caveat: model calls leave Singapore, so word "Singapore-hosted" carefully.

**Estimates given to the owner (part-time weekends):**
- **Partner preview:** unsigned, synthetic data only, backend tunnelled to the owner's laptop, for the business partner only. About 4–5 weekends.
- **Client-ready:** about 10–14 weekends, i.e. 2–3 months.

**Fallbacks recorded, in case client IT blocks installs:**
- a zero-install web app with server-side Hermes;
- per-user OAuth connectors (Google Drive and Gmail, or Microsoft 365 through Graph) as personal scopes, with draft-only actions;
- Office or Google Workspace add-ins, which IT deploys centrally;
- a Slack or Teams bot through Hermes' messaging gateway.

Desktop-only abilities the web cannot match: local files and apps, a truly device-only "My notes", OS hotkeys, and screen or clipboard context.

**Product-risk note raised with the owner.** The moat is the truth layer and measured accuracy, not the delivery surface. A chat-first UI looks like a generic chatbot. Consider a facts-first main screen ("what's true now", "sources disagree", "what changed") with chat secondary. A side-by-side test against plain Claude with all the documents was offered and has not been run yet.

## 5. Waiting on the owner

1. Google OAuth client and Entra app registration credentials. Redirect URIs: `http://localhost:5173/api/auth/callback/google` and `/callback/microsoft`, plus the future hosted domain.
2. A GitHub repo. Then push `main`, `demo-v1` and `pilot/gate-5`.
3. A real x64 Windows test machine, ideally a Dell like the clients use. An ARM Windows VM on a Mac is not representative.
4. Preview build or client-ready build.
5. 2–3 concrete tasks the local agent must do that `/api/ask` can't. These become the pilot's tests and decide which agent tools are needed.
6. Cloud provider (Google Cloud recommended) and a domain.
7. Whether the first clients' IT will allow installs at all.
8. Review of gate 5 itself. Its report was delivered, but the owner has not yet accepted it.

## 6. Open gaps carried forward

- No connection pooling: every request opens two new database connections. Address it in gate 7.
- Port 8000 is published on localhost for development.
- `preflight.sh` works only on the local stack, because it signs in through the test identity provider. The hosted version is gate 9.
- The remaining pilot gates in CLAUDE.md:
  - gate 6: document upload, a Drive connector, deletion propagation, a cost cap;
  - gate 7: deploy, backups, TLS, secrets, audit log, rate limits, zero-retention terms;
  - gate 8: admin screens, golden set, usage, delete-everything;
  - gate 9: operations and runbook.

  How these interleave with the desktop work is undecided. Ask the owner.

## 7. How to work (summary; CLAUDE.md is authoritative)

- **Gates:** stop at each one and report the commit history, the diff scope (flagging anything outside the plan) and real command output.
- **Status:** state which of "compiles / tests pass / ran locally / ran in a browser" holds for each claim. A measured failure is a result; never tune or retry until it goes green.
- **Plans first:** anything touching tokens, scope filtering or keys gets a plan reviewed by the owner before any code.
- **Permissions:** the cross-user and cross-tenant suites must pass 100% before anything merges.
- **Data:** real customer documents never appear in logs, error reports or test fixtures. All data in this repo is synthetic.
