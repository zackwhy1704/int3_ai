# Company Brain — 5-minute desktop demo

**Audience: whoever runs the demo (you), on this Windows laptop.** Everything here
is synthetic (fictional company "Brindlewood Supply Co."). The demo shows two gates:
(1) answers are *scoped* — you only get what your role can see; (2) the brain only
learns a new fact after a human **proposes** it and a **different** human **approves** it.

---

## Who's who (straight from the seed — no hand edits)

`seed/brindlewood/users.yaml` provisions three people and four scopes:

| User     | Role            | Member of                 | Owns (can approve) |
|----------|-----------------|---------------------------|--------------------|
| `marcus` | Finance Director| company-wide, **finance** | finance            |
| `priya`  | Head of Operations | company-wide, **operations** | — (proposes only) |
| `ada`    | CEO             | company-wide, leadership, **operations** | company-wide, leadership, **operations** |

So in **operations**, **priya proposes** and **ada approves** — a real two-person gate
(proposer ≠ approver). `marcus` is the finance contrast: he can see finance, priya can't.

---

## One-time setup (~2 min)

From the repo root, with Docker running:

```bash
# 1. Bring up the shared stack (control DB + brain + BYOK gateway + egress proxy)
docker compose -f deploy/hermes/docker-compose.yml up -d --build

# 2. Provision the three demo users (sidecar + gated backend + connect file each)
#    Needs deploy/hermes/.env (ADMIN_DB_PASSWORD, TENANT_KEY_KEK, ANTHROPIC_API_KEY).
set -a; . deploy/hermes/.env; set +a
bash deploy/hermes/provision-user.sh brindlewood marcus 9131 --key-owner theirs
bash deploy/hermes/provision-user.sh brindlewood priya  9132 --key-owner theirs
bash deploy/hermes/provision-user.sh brindlewood ada    9133 --key-owner theirs
```

Each user gets a connect file at `deploy/hermes/connect/brindlewood-<user>.connect.json`
(`url` = `http://127.0.0.1:<port>`, `session_token` = the backend ingress token).
Provisioning also starts a small per-user **host-ingress forwarder** (`fwd-<tenant>-<user>`,
a socat container) so `http://127.0.0.1:<port>` actually reaches the gated backend: the
backend sits on an internal, egress-locked Docker network that cannot publish a host port
directly, so the forwarder bridges just that one ingress port (the backend stays isolated).

> Localhost only. These backends are reachable only on this Docker host over plain
> HTTP until the hosted TLS ingress (Phase 3).

### Point Hermes Desktop at a user — one command

```powershell
# stock install (userData = %APPDATA%\Hermes):
deploy\hermes\connect-desktop.ps1 brindlewood marcus
# switching users is the same one command:
deploy\hermes\connect-desktop.ps1 brindlewood priya
deploy\hermes\connect-desktop.ps1 brindlewood ada
```

`connect-desktop.ps1` writes the user's connection into Hermes Desktop's
`connections.json` and makes it the active (primary) connection. It stores the
session token as a **plaintext envelope** `{ "encoding": "plain", "value": … }` — the
only non-UI way to add a connection, and legitimate because Hermes's keychain
encryption is opt-in and **OFF by default** (`apps/desktop/electron/secret-storage-policy.ts`;
`decryptDesktopSecret` at `apps/desktop/electron/main.ts:8567` returns plain
envelopes verbatim). The script prints a warning; it is for this **local, synthetic**
demo only. The supported alternative is Hermes Desktop's in-app "Add Connection"
dialog (paste the `url` + `session_token` from the connect file).

> Using a dev (from-source) Hermes Desktop or a throwaway profile? Pass
> `-UserDataDir <dir>` and launch Desktop with `HERMES_DESKTOP_USER_DATA_DIR=<dir>`.

---

## Hermes Desktop version, and "do not update" for the pilot

The pinned backend is commit **`73162b00eefde3794bed0afb53d84a19c0eed230`**
(`deploy/hermes/build-base.sh:12`). `git describe --tags` on it returns
**`rc.5-v0.21.7-458-g73162b00`** — i.e. it is an **untagged dev commit**, 458 commits
after the release tag `rc.5-v0.21.7` (the pinned base image's CLI self-reports
`Hermes Agent vunknown (2026.9.24)`). **There is no published Desktop release exactly
at this commit**; the Desktop that matches the pinned backend is one **built from the
same commit `73162b00`**. The nearest tagged release is `rc.5-v0.21.7`.

Auto-update is **user-initiated only — there is no silent background updater to
disable**:
- `electron-updater` is created with `autoDownload = false` and
  `autoInstallOnAppQuit = false` (`apps/desktop/electron/updater/mac-client.ts:49-50`).
- The only trigger is the **"Check for Updates…"** menu item, which just opens the
  updates panel (`sendOpenUpdatesRequested()`, `apps/desktop/electron/main.ts` ~6715).
- There is no `checkForUpdatesAndNotify`, no update timer/scheduler, and nothing runs
  `hermes update` on boot.

**To keep the pilot pinned:** (1) don't click **Check for Updates…**; (2) don't run
`hermes update`; (3) run exactly the build made from `73162b00`. (A package whose
install stamp is `updateMechanism: 'external'` — `apps/desktop/electron/install-stamp.ts:85`
— is marked externally-managed and never self-updates.) **Do not modify Hermes.**

---

## The demo (≈5 minutes)

Reset first so step 3 starts from "not known" (see **Reset** below).

### Step 1 — Finance user gets a cited answer
**Connect as `marcus`** (`connect-desktop.ps1 brindlewood marcus`, then open Desktop).
**Type:** `When are invoices payable?`
**Say:** "Marcus is in Finance, so the brain answers from finance documents — and it
cites its source."
**Expect:** a cited answer, e.g. *"Invoices are payable within 30 days of the invoice
date…"*, citing **Customer payment terms and credit control** (`answered_from: finance`).
**If it fails** (refused): confirm you're connected as marcus and the finance docs
seeded — `docker exec hermes-deploy-db-1 psql -U brain -d t_brindlewood -tAc "select count(*) from documents where scope_id='finance'"` should be > 0.

### Step 2 — Same question, Operations user, refused (nothing leaks)
**Switch to `priya`** (`connect-desktop.ps1 brindlewood priya`, relaunch/reconnect).
**Type the same:** `When are invoices payable?`
**Say:** "Priya is in Operations, not Finance. The brain refuses and leaks nothing —
no numbers, no document names."
**Expect:** a refusal — *"I couldn't find this in the knowledge base…"*; no payment
figures, no finance document titles.
**If it fails** (it answers): you're likely still connected as marcus — re-run the
switch command and confirm the active connection, then ask again.

### Step 3 — Operations user teaches a new fact (confirmation shows the exact statement)
**Still as `priya`. Type:** `Remember for operations that the loading bay now opens at 6am on weekdays.`
**Say:** "Priya is teaching the brain. Before anything is stored, Hermes shows her the
exact statement it will save and asks her to confirm."
**Expect:** an elicitation/confirm prompt showing **exactly**:
```
Capture into 'operations' — this exact statement will be stored and searchable:

    loading bay — opening time: 6am on weekdays

Your evidence (not stored as the searchable text): The loading bay now opens at 6am on weekdays.

Confirm?
```
Click **Confirm/Accept**. Result: a pending proposal (nothing is searchable yet).
**If it fails** (no prompt): the model must call `propose_claim`; rephrase as an
explicit instruction to remember/save a fact for operations. Declining the prompt
stores nothing (by design).

### Step 4 — The owner approves (confirmation shows statement + proposer)
**Switch to `ada`** (`connect-desktop.ps1 brindlewood ada`, relaunch/reconnect).
**Type:** `Show me pending operations proposals and approve the loading bay one.`
**Say:** "Ada owns Operations. She — a *different* person from Priya — reviews and
approves. The confirmation shows the statement and who proposed it."
**Expect:** an elicitation/confirm prompt showing **exactly** (timestamp varies):
```
Approve into 'operations' the statement:

    loading bay — opening time: 6am on weekdays

Evidence: The loading bay now opens at 6am on weekdays.
Proposed by: priya at 2026-10-13T12:05:14Z

Confirm?
```
Click **Confirm/Accept**. Result: a workflow record titled
**`operations workflow — <today> — Priya N.`** is written.
**If it fails** (403 / "only the scope owner may review"): you're not connected as
ada (the owner). Re-run the switch command. If priya tries this, she's correctly
refused — she proposes, she doesn't approve.

### Step 5 — Operations user re-asks, now a cited answer
**Switch back to `priya`. Type:** `What time does the loading bay open?`
**Say:** "Now that Ada approved it, the same question Priya couldn't answer a minute
ago comes back as a cited answer — citing the workflow record, not a guess."
**Expect:** *"The loading bay opens at 6am on weekdays."* citing
**`operations workflow — <today> — Priya N.`** (`answered_from: operations`).
**If it fails** (still refused): the approval may not have completed — redo step 4 as
ada; confirm `docker exec hermes-deploy-db-1 psql -U brain -d t_brindlewood -tAc "select title from documents where authority='workflow'"` shows the record.

---

## Screenshots (captured from a real run through the GUI)

Driving stock Hermes Desktop (a dev build from the pinned source) against this synthetic
deploy stack, all five steps as captured in [demo-screenshots/](demo-screenshots/):

1. [`1-finance-cited.png`](demo-screenshots/1-finance-cited.png) — marcus: cited finance answer ("Customer payment terms and credit control").
2. [`2-operations-refused.png`](demo-screenshots/2-operations-refused.png) — priya: the same question refused, nothing leaked.
3. [`3-teach-confirm.png`](demo-screenshots/3-teach-confirm.png) — priya: the capture confirmation shows the exact statement before it is stored.
4. [`4-owner-approve.png`](demo-screenshots/4-owner-approve.png) — ada (the owner): the approval shows the statement **and** the proposer (priya).
5. [`5-reask-cited.png`](demo-screenshots/5-reask-cited.png) — priya: the re-ask is now answered and cited to "operations workflow — <date> — Priya N.".

## Reset between runs

Step 3 only lands if the loading-bay fact is *not* already known. To start clean,
rebuild the deploy DB from the seed (this also removes any earlier demo workflow
records — no hand-deleting rows):

```bash
docker rm -f $(docker ps -aq --filter name=mcp-brindlewood- --filter name=hermes-brindlewood-) 2>/dev/null
docker volume rm $(docker volume ls -q --filter name=hermes-home-brindlewood-) 2>/dev/null
docker network rm $(docker network ls -q --filter name=u-brindlewood-) 2>/dev/null
docker compose -f deploy/hermes/docker-compose.yml down -v
docker compose -f deploy/hermes/docker-compose.yml up -d --build
# then re-provision the three users (setup step 2) and re-point Desktop (connect-desktop.ps1)
```

Or, to keep the demo but make step 3 land, just teach a *different* fact (change the
loading-bay sentence) each run.
