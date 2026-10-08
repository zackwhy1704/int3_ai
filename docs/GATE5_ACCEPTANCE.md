# Gate 5 Acceptance Pack

**Branch:** `integrate`
**Time to complete:** ≤30 minutes (Docker must already be installed)
**Written:** 2026-10-08

This document tells the owner how to verify gate 5 acceptance on the `integrate`
branch. Gate 5 delivers real identity and tenancy; the demo `X-User-Id` header is
removed from every code path.

---

## 1. Prerequisites

### Software
- **Docker Desktop** with the WSL2 backend enabled (Windows). Verify:
  ```
  docker --version
  docker compose version
  ```
- **Git for Windows** (includes Git Bash, needed for `.sh` files).
- Internet access (Docker images download on first run).

### Line endings (Windows only)
Before cloning or checking out, ensure LF line endings:
```
git config --global core.autocrlf false
```

### Generate `.env`
Copy the example file and fill in secrets. Never re-use the demo `.env`.

```powershell
# In PowerShell — generate random hex secrets
function hex32 { -join ((1..32) | % { '{0:x2}' -f (Get-Random -Max 256) }) }

# Copy example
copy .env.example .env
```

Open `.env` and set these required values:
```
ADMIN_DB_PASSWORD=<hex32 output>
APP_CONTROL_PASSWORD=<hex32 output>
TENANT_DB_KEY=<hex32 output>
ANTHROPIC_API_KEY=<your Anthropic key>
```

Leave `MOCK_OIDC_*` values as in `.env.example` — they are used by the local
test identity provider.

---

## 2. Bring it up

```bash
git checkout integrate
docker compose --profile dev up -d --build
```

Wait for `backend` to show `Application startup complete` in the logs:
```bash
docker compose logs -f backend
```

Provision the demo tenant (Brindlewood Supply Co.):
```bash
docker compose run --rm tools python -m app.tenants.admin provision \
  --id brindlewood \
  --name "Brindlewood Supply Co." \
  --seed /seed/brindlewood \
  --google-domain brindlewood.example
```

Run all tests (provisions the three test tenants automatically):
```bash
docker compose run --rm tools pytest -v -m "not llm"
```

**Expected output:**
```
============ 79 passed in X.Xs ============
```

(72 original pilot tests + 7 new /v1/ tests from A5)

If any test fails, do not proceed. Record the failure output and report it.

---

## 3. Cross-tenant break-in attempts

These are the exact curl commands from HANDOFF.md §1. Each must return 404 (or 401),
never the content of tenant B.

First, sign in as Priya (tenant A) and capture the session cookie:

```bash
# Open http://localhost:5173 in a browser, click "Continue with Test Google",
# enter username "google-priya", submit the JSON claims shown in step 4,
# then extract the __Host-session cookie from your browser's DevTools.
SESSION_COOKIE="__Host-session=<paste value here>"
```

**Attempt 1: Access Hollowmere (tenant B) brains**
```bash
curl -s -o /dev/null -w "%{http_code}" \
  -H "Cookie: $SESSION_COOKIE" \
  "http://localhost:8000/api/brains"
```
Expected: `200` (returns brindlewood brains only — never hollowmere data)

**Attempt 2: Cross-tenant /v1/search with hollowmere brain_id**
```bash
curl -s -w "\n%{http_code}" \
  -X POST \
  -H "Cookie: $SESSION_COOKIE" \
  -H "Content-Type: application/json" \
  -H "Origin: http://localhost:5173" \
  -d '{"query": "refund policy", "brain_id": "company-wide-hollowmere"}' \
  "http://localhost:8000/v1/search"
```
Expected: `404` — brain_id is not in this user's scopes.

**Attempt 3: Direct document access with another tenant's doc ID**
```bash
curl -s -o /dev/null -w "%{http_code}" \
  -H "Cookie: $SESSION_COOKIE" \
  "http://localhost:8000/api/documents/hollowmere-doc-001"
```
Expected: `404` — tenant DB isolation; Priya's connection can only see brindlewood documents.

**Attempt 4: No session cookie**
```bash
curl -s -o /dev/null -w "%{http_code}" \
  "http://localhost:8000/api/brains"
```
Expected: `401` — no session cookie → not signed in.

---

## 4. Browser sign-in walkthrough (mock IdP)

This uses the `navikt/mock-oauth2-server` which runs on port 8081 in the `dev` profile.

1. Open **http://localhost:5173** in a browser.

2. Click **"Continue with Test Google"**.

3. In the mock IdP form that opens, enter:
   - **Username:** `google-priya`
   - **Claims (JSON):**
     ```json
     {
       "email": "priya@brindlewood.example",
       "email_verified": true,
       "hd": "brindlewood.example"
     }
     ```

4. Click **Sign in** (or **Submit**).

5. You should be redirected back to `http://localhost:5173` and see:
   - Priya's name in the UI header.
   - The company-wide and operations brains open (accessible).
   - One locked brain (Finance or Leadership).

6. Repeat with **`google-marcus`** and claims:
   ```json
   {
     "email": "marcus@brindlewood.example",
     "email_verified": true,
     "hd": "brindlewood.example"
   }
   ```
   Marcus should see Finance brain open and Operations locked.

7. Ask a question in each brain. Verify:
   - Marcus's answer never cites the operations document.
   - Priya's answer never cites the finance document.
   - An unsupported question returns "No reliable source found".

---

## 5. What still needs real Google/Microsoft OAuth

The mock IdP is for development and testing only. The backend **refuses to start**
with `MOCK_OIDC_URL` set when `ENV=production`.

### Google OAuth client
Create in **Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client IDs**:
- **Application type:** Web application
- **Authorised redirect URIs:**
  - `https://<your-domain>/api/auth/callback/google` (production)
  - `http://localhost:5173/api/auth/callback/mock-google` (dev — already works)
- Note the **Client ID** and **Client Secret**.
- Set in `.env`:
  ```
  GOOGLE_CLIENT_ID=<your client id>
  GOOGLE_CLIENT_SECRET=<your client secret>
  ```
- Configure the tenant's Google domain:
  ```bash
  docker compose run --rm tools python -m app.tenants.admin configure \
    --id brindlewood --google-domain <your-google-workspace-domain>
  ```

### Microsoft Entra (Azure AD) app registration
Create in **Azure portal → Azure Active Directory → App registrations → New registration**:
- **Redirect URI (Web):**
  - `https://<your-domain>/api/auth/callback/microsoft` (production)
- Note the **Application (client) ID** and **Directory (tenant) ID**.
- Create a **Client secret** under Certificates & secrets.
- Set in `.env`:
  ```
  MS_CLIENT_ID=<your client id>
  MS_CLIENT_SECRET=<your client secret>
  ```
- Configure the tenant's Entra tenant ID:
  ```bash
  docker compose run --rm tools python -m app.tenants.admin configure \
    --id brindlewood --ms-tenant-id <your-entra-tenant-id>
  ```

---

## 6. pip-audit output (known advisories)

Docker is not available in this environment, so `pip-audit` cannot be run against
the built images. Expected status based on the locked requirements:

### backend image
`requirements.txt` pins `authlib==1.6.0`. **No python-jose** (D3 decision).
- `python-jose 3.3.0` CVE-2024-33663: **not present** — removed (D3).
- `pytest 8.3.4`: past the advisory version.
- `authlib 1.6.0`: no known CVEs at time of writing.

Expected `pip-audit` output:
```
No known vulnerabilities found
```

### gateway image
`requirements.txt` pins `authlib==1.6.0`. **No python-jose**.
- Same status as backend.

Expected `pip-audit` output:
```
No known vulnerabilities found
```

### To run pip-audit manually (once Docker is available):
```bash
docker compose run --rm tools pip-audit -r /requirements.txt
# or in the gateway container:
docker compose run --rm gateway pip-audit -r /app/requirements.txt
```

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `pytest` fails with DB connection error | Stack not fully up | Wait for `db` healthcheck: `docker compose ps` |
| Backend refuses to start | `MOCK_OIDC_URL` set + `ENV=production` | Remove `ENV=production` from `.env` for local dev |
| Gateway refuses to start | `GATEWAY_AUTH=none` + `ENV=production` | Dev only: remove `ENV=production` |
| Cross-tenant attempt returns 200 instead of 404 | Session leak | Report immediately — this is a security failure |
| `79 passed` not reached | New test failure | Record output, do not proceed |
