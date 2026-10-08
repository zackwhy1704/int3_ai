# Reconciliation plan: `main` → `integrate`

Written 2026-10-08. **This is a plan only.** No merge or rebase will happen until the
owner approves this document. Every claim below names file:line evidence; "unverified"
means something I can read but cannot run.

---

## 1. Branch ancestry

| Branch | HEAD | Merge-base with the other |
|---|---|---|
| `main` | `1cbdb4c` | `dc70b64` (demo-v1 tag) |
| `origin/pilot/gate-5` | `e390924` | `dc70b64` (demo-v1 tag) |

Both branches diverged from `dc70b64` (demo hardening). Neither branch contains
commits from the other; there is no common ancestor beyond the demo.

Commits unique to `main` (6 commits, oldest first):

| SHA | Subject |
|---|---|
| `018c692` | Add gateway service + update CLAUDE.md for desktop layer |
| `57337a5` | Add engineering standards + Gate 0 blockers to CLAUDE.md |
| `7a8b828` | Gate 2: rewrite gateway with full tool-call support and fix critical blockers |
| `2b9a415` | Gate 3: implement /v1/ brain API endpoints for desktop MCP integration |
| `2354efb` | Gate 0 item 2: citation invariant — server-side validate-claims endpoint |
| `1cbdb4c` | Gate 4: implement RS256 OIDC token verification (backend + gateway) |

Commits unique to `origin/pilot/gate-5` (2 commits):

| SHA | Subject |
|---|---|
| `17fe8dd` | Gate 5: real identity and tenancy |
| `e390924` | Handoff notes for continuing on Windows |

---

## 2. What each branch has that the other needs

### pilot/gate-5 has — main does not

**Identity and tenancy** (`17fe8dd`):
- `backend/app/auth/` — OIDC code-flow + PKCE via `authlib`, RS256 only,
  `email_verified` enforced, `sub` (Google) / `tid:oid` (Microsoft) binding,
  nonce per login, `LoginRejected` exception. (`backend/app/auth/oidc.py:1-187`)
- `backend/app/auth/principal.py` — `Principal` dataclass; every request resolves
  session → control DB → tenant DB (as `t_<id>` role) → user → memberships → scopes.
  Nothing else the client sends is consulted. (`principal.py:1-95`)
- `backend/app/auth/sessions.py` — server-side sessions; only the SHA-256 of the
  session id is stored; `__Host-` cookie; 30-min idle + 8-h absolute.
  (`sessions.py:1-94`)
- `backend/app/auth/routes.py` — `/api/auth/login`, `/api/auth/callback`,
  `/api/auth/logout`. (`routes.py:1-83`)
- `backend/app/control_schema.sql` — control DB schema: `tenants`, `sessions`,
  `login_state`. (`control_schema.sql:1-45`)
- `backend/app/tenants/admin.py` — operator provisioning: `provision`, `configure`,
  `init`; failed provision rolls back. (`admin.py:1-189`)
- `backend/app/config.py` — single location for ports, cookie name, `APP_ORIGIN`,
  `ENV`. (`config.py:1-33`)
- `backend/app/db.py` — `control_conn()` + `tenant_conn(tenant_id)` using the
  tenant's own role `t_<id>`. (`db.py:1-51`)
- **72 tests** including cross-tenant DB boundary, forgery, CSRF, session rules.
  (`tests/test_auth.py`, `test_csrf.py`, `test_db_boundary.py`, `test_id_token.py`,
  `test_tenancy.py`)
- `authlib==1.3.2` (not `python-jose`). (`backend/requirements.txt:10`)
- `docker-compose.yml` — `init`, `tools`, `mock-oidc` services; per-tenant DB
  provisioning; profile-gated dev tooling. (`docker-compose.yml`)
- Seed layout `seed/brindlewood/` + fixtures for two collision tenants
  (`hollowmere`, `brindlewood_labs`). (`seed/fixtures/`)

**What pilot lacks:**
- The `gateway/` directory does not exist on `origin/pilot/gate-5`
  (`git ls-tree origin/pilot/gate-5 --name-only` shows no `gateway`).
- No `/v1/` API (desktop MCP endpoint).
- No `validate-claims` endpoint.

### main has — pilot does not

| Commit | Useful content | Keep / drop decision |
|---|---|---|
| `7a8b828` | `gateway/` — full OpenAI↔Anthropic proxy, tool-call translation, streaming, fail-closed auth | **Keep as-is.** Pilot has no gateway at all. The gateway is a separate service (no Python imports cross the boundary) so it can be transplanted cleanly. |
| `2b9a415` | `backend/app/v1.py` — `/v1/search`, `/v1/claims/{id}`, `/v1/sources` | **Keep the endpoints; replace the auth layer.** See §3. |
| `2354efb` | `backend/app/v1.py` — `POST /v1/validate-claims` | **Keep with redesign.** The current check (exists + in-scope) does not satisfy the structural-citation invariant; a retrieval-bound redesign is required before accepting it (see §4). |
| `1cbdb4c` | `gateway/oidc.py`, `backend/app/oidc.py`, `gateway/auth.py` (RS256 verification) | **Gateway oidc.py: keep, rewrite async.** The backend copy will be deleted; pilot's `authlib`-based `auth/oidc.py` supersedes it. |
| `018c692`, `57337a5` | `CLAUDE.md` updates (engineering standards, Gate 0 list, gateway section) | **Keep in merged CLAUDE.md.** Pilot's CLAUDE.md has a superset of the product brief; the engineering standards section is largely shared but needs merging. |

---

## 3. Conflict files — file by file

These files exist on both branches with incompatible changes:

### `backend/app/main.py`
- **pilot** (`17fe8dd`): mounts `auth_router`, uses `Principal = Depends(principal)`,
  removes `X-User-Id`. (`main.py:1-80`)
- **main** (`2b9a415`): adds `v1.router` but still has the demo `X-User-Id` auth shape
  via `scopes.py`. (`main.py` at `2b9a415`)
- **Resolution:** base on pilot's version; add `app.include_router(v1.router)`.
  `v1.py`'s internal `authed_user` dependency is replaced with `Principal`
  (see §3 `backend/app/v1.py`).

### `backend/app/v1.py`
- **main only** (no equivalent in pilot): `/v1/search`, `/v1/claims/{id}`,
  `/v1/sources`, `/v1/validate-claims`. Auth is self-contained with `BRAIN_AUTH`
  env var; uses `from .scopes import resolve, user_scopes, user_by_email`.
  (`v1.py:1-200+`)
- `scopes.py` is **deleted in pilot** (`git show origin/pilot/gate-5:backend/app/scopes.py`
  returns nothing). `v1.py`'s imports break immediately on `integrate`.
- **Resolution:**
  - Replace the `authed_user` dependency in `v1.py` with `Principal = Depends(principal)`.
    The caller is then always a verified tenant user with scopes already resolved;
    `user_by_email`, `user_scopes`, `BRAIN_AUTH`, `BRAIN_DEV_USER`, `OIDC_AUDIENCE`
    are all removed from this file.
  - The `/v1/search` SQL uses `user_scopes(conn, user_id)` — rewrite to use
    `p.scopes` from `Principal`.
  - `validate-claims` also gets `Principal`; scope list comes from `p.scopes`.
  - `v1.py` depends on `scopes.py:resolve` for the cross-scope filter — replace
    with `p.resolve(brain_id)` which pilot's `Principal` already implements
    (`principal.py:26-31`).

### `backend/app/scopes.py`
- **pilot:** deleted entirely. Functionality replaced by `Principal` and the
  tenant DB role boundary.
- **main:** imported by `v1.py` for `resolve`, `user_scopes`, `user_by_email`,
  and by `main.py` for the demo `X-User-Id` path.
- **Resolution:** delete `scopes.py` on `integrate`. All callers replaced as
  described above. `user_by_email` is no longer needed: pilot resolves identity
  from `sub`, not `email`.

### `backend/app/db.py`
- **pilot** (`17fe8dd`): `control_conn()` + `tenant_conn(tenant_id)` with the
  tenant role. (`db.py:1-51`)
- **main** (demo): single `connect()` against one database. (`db.py`)
- **Resolution:** base on pilot's version. `v1.py` will receive a `psycopg.Connection`
  through `Principal.conn` — no direct `connect()` call needed in `v1.py`.

### `backend/app/oidc.py`
- **main only:** copy of `gateway/oidc.py` using `python-jose`. Deleted on
  `integrate`. Pilot uses `authlib` in `auth/oidc.py`.
- **Resolution:** delete `backend/app/oidc.py`. The gateway retains `gateway/oidc.py`
  independently (the two services never import each other — `gateway/` is a
  separate service).

### `backend/app/schema.sql`
- **pilot** (`17fe8dd`): per-tenant schema (no `users.email` column; identity is
  `sub`-based, email stored in session only). Added `ALTER TABLE users ADD COLUMN
  IF NOT EXISTS email text UNIQUE` in main's `1cbdb4c`.
- **Resolution:** base on pilot's per-tenant schema. The `email` column addition
  is dropped — pilot stores email in the session, not in the users row directly,
  because multiple providers can share an email address.

### `backend/app/seed.py`
- **pilot:** uses `tenants.admin.provision` API; seed data lives in
  `seed/brindlewood/`.
- **main:** flat single-DB seed. Also added `email` column seeding (`1cbdb4c`).
- **Resolution:** base on pilot's version. The email seeding is dropped.

### `seed/users.yaml`
- **pilot:** moved to `seed/brindlewood/users.yaml`; no email column.
- **main:** added `.example.com` emails (`1cbdb4c`).
- **Resolution:** keep pilot's path and shape; drop email additions.

### `backend/requirements.txt`
- **pilot:** `authlib==1.3.2` (no `python-jose`).
- **main:** `python-jose[cryptography]==3.3.0` (CVE-2024-33663) + no `authlib`.
- **Resolution:** pilot's `authlib==1.3.2` only. `python-jose` removed.
  The gateway has its own `requirements.txt` and keeps using `python-jose` for now;
  that file is addressed separately in Step 1 of the implementation (upgrade to
  ≥3.4.0 or migrate to `authlib` for consistency).

### `docker-compose.yml`
- **pilot:** adds `init`, `tools`, `mock-oidc` services, per-tenant DB model,
  `APP_DB_URL` construction.
- **main:** adds `gateway` service and its env block.
- **Resolution:** base on pilot's version; add the `gateway` service block from
  `7a8b828`. The `BRAIN_DEV_USER` / `BRAIN_AUTH` env vars in the backend service
  are removed.

### `CLAUDE.md`
- **pilot:** includes product brief, demo decisions, gate 5 layout, running
  instructions (docker compose `--profile dev`).
- **main:** adds engineering standards section (§ "Engineering standards"), gateway
  section, desktop section, Gate 0 blocker list.
- **Resolution:** hand-merge. Keep pilot's layout/running instructions and
  gate-5-era identity/tenancy description; keep main's engineering standards and
  gateway and desktop sections verbatim; update the demo "out-of-scope" list in
  light of HANDOFF §4.

### `web/src/App.tsx`
- **pilot:** has session-aware sign-in UI, CSRF token handling, provider buttons.
- **main:** minimal status-only UI (Gate 1 demo).
- **Resolution:** keep pilot's version unchanged. The desktop has its own
  `int3_desktop` UI; the web console is pilot's concern.

---

## 4. Items that require redesign before they can be merged

These are **not** resolvable by cherry-picking:

### 4a. `validate-claims` (main `2354efb`) — REMOVED, not ported

The current `validate-claims` endpoint checks "claim ID exists in scope" but does
not verify that the claim was in the set retrieved for the current answer
(`v1.py:validate_claims`). It is NOT ported to `integrate`.

**CORRECTION:** `/v1/validate-claims` is REMOVED. It is replaced by a
retrieval-bound design (`/v1/validate`):
- `/v1/search` and `/v1/brain_ask` store the retrieved claim-ID set in the
  CONTROL DB against a `retrieval_id` (UUID, 1-hour expiry, keyed to
  `session_id + tenant_id`).
- `POST /v1/validate` requires `{retrieval_id, claim_ids}`. The retrieval row
  must match `session_id == p.session_id AND tenant_id == p.tenant_id`. Each
  claim_id must be in `retrievals.claim_ids`.
- Returns `{valid: list[int], invalid: list[int]}`.
- Hallucinated or out-of-retrieval IDs are always invalid; the UI shows
  "unverifiable citation".

### 4b. Gateway `oidc.py` — sync JWKS fetch inside async handler

`gateway/oidc.py:_fetch_jwks` calls `httpx.get` (sync) inside an `async` FastAPI
handler (`gateway/auth.py:verify_token` is `async`). This blocks the event loop
during every JWKS refresh.

Also: every token with an unknown `kid` triggers a fetch, with no rate limit.
The audit measured 6 fetches from 5 garbage tokens.

**Required before merge:**
- Use `httpx.AsyncClient` in `_fetch_jwks`; make `verify_token` `async`.
- JWKS refetch rule (CORRECTED): refetch only when token's kid is absent from the
  cache AND at most once per 60s. Garbage/expired/wrong-aud tokens NEVER trigger
  a refetch — these conditions are only detectable after decoding, by which point
  the kid was already in the cache. The 60s debounce applies only to cache-miss
  events (unknown kid).

These two fixes are small and self-contained; they belong in the same commit as
the gateway is transplanted onto `integrate`.

### 4c. `python-jose==3.3.0` CVE

`backend/requirements.txt` and `gateway/requirements.txt` (main) both pin
`python-jose==3.3.0`. CVE-2024-33663 is fixed in 3.4.0. The RS256-only
restriction limits exploitation risk, but the pin must be updated.

**Resolution (CORRECTED — D3 decision):** migrate both gateway and backend to
`authlib>=1.6.12`. `python-jose` is removed from every requirements file.
The gateway cannot use pilot's OIDC (pilot uses session cookies; gateway/v1 need
bearer tokens — that is Phase B). Until then: `GATEWAY_AUTH=none` in the dev
compose profile only; the gateway refuses to start when `ENV=production` with
`GATEWAY_AUTH=none`.

### 4d. `/v1/search` — refusal gate missing, superseded claims visible

Main's `v1.py` SQL inner-joins chunks to claims but does not run the cross-encoder
refusal gate (`rerank.py`). It also returns superseded claims alongside current
ones (`_SEARCH_SQL` at `v1.py`).

**Required before merge:**
- `/v1/search` applies the same refusal threshold as `/api/ask` (cross-encoder
  score > 0 per `calibrate.py` measurement).
- Superseded claims are not returned in results; the superseding claim's id is
  included in the result so the caller can show "updated to [claim:N]" without
  being told to cite an id they were not shown.

### 4e. Desktop token issuance plan (HANDOFF §4 item 4)

HANDOFF.md §4 item 4 says: **"show the owner the plan before writing code"** for
the desktop sign-in token-issuance path. The current `int3_desktop` `auth.rs`
bypasses this: it sends Google id_tokens directly to the backend as Bearer tokens,
which is incompatible with pilot's session-cookie model.

**Action required before desktop work resumes:** write the token plan only (no
code) and stop for owner review. The plan must cover:
- system-browser sign-in → `companybrain://` deep link → PKCE code exchange
- short-lived access token (~15 min) + refresh token (≤8 h) issued by the
  control plane, not Google directly
- server-side revocation
- how a long-running Hermes agent refreshes the token without restarting
- where tokens live on the device (Windows Credential Manager, not a file)

---

## 5. Proposed `integrate` branch construction

```
git checkout -b integrate origin/pilot/gate-5
```

Then port in this order (one commit each):

1. **Transplant `gateway/`** from main `7a8b828`:
   - Copy `gateway/` verbatim.
   - Upgrade `gateway/requirements.txt`: `python-jose>=3.4.0` (or migrate to
     `authlib` — owner to decide).
   - Fix JWKS fetch: `async httpx`, `kid`-based refetch with 60-s debounce.
   - Add `gateway` service to pilot's `docker-compose.yml`.
   - Tests: forged-token, expired-token, wrong-aud unit tests for gateway auth.

2. **Port `/v1/` endpoints** from main `2b9a415` + `2354efb`:
   - Drop `authed_user`; wire `Principal = Depends(principal)`.
   - Replace `scopes.py` calls with `p.scopes` / `p.resolve(brain_id)`.
   - Apply refusal gate + current-claims-only filter to `/v1/search`.
   - Replace `validate-claims` with retrieval-bound design (§4a above).
   - Tests: cross-tenant search, cross-scope search, forged token, retrieval-bound
     citation validation.

3. **Merge `CLAUDE.md`** — hand-merge as described in §3.

4. **Delete dead code** from main that does not survive reconciliation:
   `backend/app/oidc.py`, `backend/app/scopes.py` (already absent in pilot),
   `BRAIN_AUTH`/`BRAIN_DEV_USER` env vars from compose.

5. **Write desktop token plan** (§4e) — document only, no code, stop for review.

---

## 6. What is NOT proposed

- Carrying main's email-based user lookup (`user_by_email`) forward. Pilot binds
  to `sub`; email can change and is not a stable identity key.
- Carrying `BRAIN_AUTH=dev` / `BRAIN_DEV_USER` silent defaults forward. Local dev
  uses the `mock-oidc` server (pilot's `devtools/mock_login.py`).
- Merging `int3_desktop` auth work until the token-issuance plan (§4e) is
  reviewed. `int3_desktop/src-tauri/src/auth.rs` and its callers will need
  significant rework.
- Any new features beyond what is listed in §5. The reconciliation scope is
  exactly: gateway, /v1/ endpoints on pilot's identity layer, CLAUDE.md merge.

---

## 7. Files outside the planned surface — flagged per standards

Main commits touched these files beyond the work they were labelled as:

| Commit | File | Note |
|---|---|---|
| `1cbdb4c` | `backend/app/schema.sql` | Added `email` column — conflicts with pilot schema; dropped. |
| `1cbdb4c` | `seed/users.yaml`, `backend/app/seed.py` | Added email seeding — dropped; pilot has different seed path. |
| `2b9a415` | `backend/app/main.py` | Added `v1.router` but did not remove demo `X-User-Id` path — will be addressed in step 2. |

---

**Stop here. No merge or rebase until the owner approves this document.**
Items needing explicit owner decisions before any code is written:

- [ ] Approve the `integrate` base choice (`origin/pilot/gate-5`).
- [ ] Confirm that the desktop work (`int3_desktop main`) is paused until the
      token-issuance plan in §4e is written and reviewed.
- [ ] Choose: upgrade `gateway/requirements.txt` to `python-jose>=3.4.0`, or
      migrate gateway to `authlib` for consistency with the backend?
- [ ] Review gate 5 itself (HANDOFF §5 item 8) — pilot's 72 tests were not
      reviewed by the owner; this is prerequisite to building on it.
