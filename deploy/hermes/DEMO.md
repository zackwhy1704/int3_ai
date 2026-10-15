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

**MSIX AppData virtualization (the official Windows app is MSIX).** The official
Windows Hermes app ships as an MSIX package (`win.target = ['msix']` in
`apps/desktop/electron-builder.config.cjs`). Windows redirects a packaged app's
`%APPDATA%`: *"the OS will open the file from the per-user, per-package location
first. If that location doesn't exist, then the OS will attempt to open the file
from the real AppData location."*
([Microsoft Learn](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes),
"AppData operations… / Write under AppData"). So writing only the real
`%APPDATA%\Hermes` works on first launch but is **shadowed** once the app writes its
own connections file into its private store. `connect-desktop.ps1` handles this: if
it finds a packaged install (via `Get-AppxPackage`, resolving the package family name
at runtime — no hard-coded publisher hash), it writes **both** the private
per-package copy
(`%LOCALAPPDATA%\Packages\<PackageFamilyName>\LocalCache\Roaming\<app>\connections.json`)
**and** real `%APPDATA%\<app>`, and prints which paths it used. A plain (non-packaged)
install just uses `%APPDATA%\Hermes`. If you switch users *after* the app has written
its own connection file, just re-run the command.

> **If you cannot verify without installing:** the exact private-store `<app>` folder
> name (Electron userData = productName: `Hermes` vs `Hermes Agent`) and the package
> family name are resolved at runtime; they are **not verified here** (no MSIX install
> on the build machine). The script probes the live package and existing folders.

> Using a dev (from-source) Hermes Desktop or a throwaway profile? Pass
> `-UserDataDir <dir>` and launch Desktop with `HERMES_DESKTOP_USER_DATA_DIR=<dir>`
> (this skips packaged detection and writes exactly that dir).

---

## Hermes Desktop version, and "do not update" for the pilot

The backend is pinned to a **published release**: **Hermes Agent v0.21.7** (tag
`v0.21.7` → commit `71d34bad1045610252eda02ee79549436896e784`,
`deploy/hermes/build-base.sh`). Release page:
<https://github.com/NousResearch/hermes-agent/releases/tag/v0.21.7>. Per its notes,
v0.21.7 ships an **official Windows Desktop** (Microsoft Store, MSIX, and the
installer) — so partners can install the stock app for the matching version without
building from source. (The GitHub release itself carries **no** binary asset; the
installers live in the Store / MSIX / the install one-liner.)

Auto-update at v0.21.7 is **user-initiated — no silent background install**:
- `electron-updater` is created with `autoDownload = false` and
  `autoInstallOnAppQuit = false` (`apps/desktop/electron/updater/mac-client.ts:49-50`),
  and there is no `checkForUpdatesAndNotify` / update timer / scheduler.
- There is a **passive** version check that can show an update banner
  (`updates.check`, default on — `hermes_cli/config_defaults.py`); it never installs
  on its own. "Check for Updates…" opens the panel; `hermes update` is explicit.

**To keep the pilot pinned:** (1) don't accept the update banner or click **Check for
Updates…**; (2) don't run `hermes update`; (3) install **Nous's official non-Store
installer** (the standalone download from the v0.21.7 release page, not the Microsoft
Store and not an App-Installer/MSIX feed) — its in-app updater is **user-initiated**
(`autoDownload=false`, `apps/desktop/electron/updater/mac-client.ts:49-50`), so the
machine stays on v0.21.7 until a human chooses to update. Do **not** ship our own
desktop build — pilots run Nous's official app; the `71d34bad` pin is for the
**backend** only, not a desktop we distribute. The Store and App-Installer/MSIX paths
auto-update and can't be pinned per-app — that drift is the risk (see below). **Do not
modify Hermes.**

### How the official Windows app updates, and keeping it on v0.21.7

The Windows app is distributed three ways (v0.21.7 notes:
<https://github.com/NousResearch/hermes-agent/releases/tag/v0.21.7> — "Microsoft Store
and MSIX, the installer"), each with a different update path:

- **Microsoft Store install** → updates **automatically through the Store**
  (release notes: "Microsoft Store installs update through the Store").
- **Out-of-store MSIX (App Installer)** → the generated `.appinstaller` sets
  `<UpdateSettings><OnLaunch HoursBetweenUpdateChecks="12"/>`
  (`scripts/bundles/release_artifacts.py:291-292`), so Windows App Installer
  **checks the feed on launch, at most every 12 h**, and updates when a newer package
  is published.
- **Bundled / standalone build** → the in-app updater, which is **user-initiated**
  (`autoDownload=false` / `autoInstallOnAppQuit=false`,
  `apps/desktop/electron/updater/mac-client.ts:49-50`; no scheduler).

**To hold a pilot machine on v0.21.7:** the Store and App-Installer MSIX paths update
on their own when Nous publishes a newer release, which a pilot cannot pin per-app —
**that auto-update drift is the risk.** So install **Nous's official non-Store
installer** (the standalone download from the v0.21.7 release page): its in-app updater
is user-initiated, so the machine stays on v0.21.7 until a human updates it. Do **not**
build or ship our own desktop to pin it — pilots run Nous's official app. If you must
use the Store/MSIX, you can only *mitigate* (pause Microsoft Store app updates, or keep
the `.appinstaller` feed unreachable). **These are mitigations, not a hard pin — not
verified on a packaged install here.**

`connect-desktop.ps1` helps you *notice* drift: when it finds a packaged install it
reads the installed version (`Get-AppxPackage … .Version`) and **warns if it is not
`v0.21.7`** (pass `-ExpectedVersion` to change the bar). Note Microsoft Store builds
carry a timestamp-based version quad, so a numeric mismatch there may be a Store build
rather than a wrong version — the warning says so.

---

## Pilot privacy & approval behaviour

**Usage stats are OFF by default — nothing is sent to Nous.** Stock Hermes shows a
"Share usage stats? … Share with Nous / Keep on this computer / No thanks" strip until
the choice is made. That setting is *backend* config (`telemetry.shared_metrics`, which
the desktop reads over the `shared_metrics` RPC — there is **no** desktop-side store, so
it can't be set from `connect-desktop.ps1`). `provision-user.sh` writes it **decided and
off** for every pilot user (`enabled: false`, `send: false`, `offer_version: 2`), so the
strip never appears and nothing is collected or transmitted. This matches Hermes's own
shipped default of `enabled=false / send=false` (`hermes_cli/config_defaults.py`);
`offer_version: 2` marks consent *decided* (`shared_metrics_consent.py`).
*If a user did opt in* ("Share with Nous"), Hermes sends **privacy-safe aggregate counts
— no messages, no files** — to `https://telemetry.nousresearch.com/v1/telemetry`. "Keep
on this computer" aggregates locally only; "No thanks" collects nothing.

**"Always allow" on a brain approval does NOT persist — every capture/approval asks
again.** The propose/approve confirmations are MCP *elicitations*, which Hermes treats as
per-call confirmations with "no pattern to remember" (`tools/approval_prompt.py:346`,
`allow_permanent=False`; the gateway wait does not persist either —
`tools/approval_gateway_wait.py:138`). So even if you click "Always allow", the next
proposal or approval prompts the human again — the two-person capture gate can't be
switched off by habit.

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

## Automated tests (so you don't have to run the demo by hand)

**`make demo` — THE demo (Hermes tier).** Drives the **real stock Hermes agent** through
the scenario over `/api/ws` (the desktop's exact path; `deploy/hermes/demo/hermes_chat.py`),
in a fresh isolated `DEPLOY_PROJECT=hermes-demo` stack that never touches `hermes-deploy`.
It checks prerequisites, generates every non-model secret, provisions the 3 users, runs
the scenario, prints a ✅/❌ table, and **leaves the stack up** so you can open the desktop
app. `make demo-down` removes it. This is "Hermes under the hood" literally. Pass criteria
use what Hermes **returned** plus DB rows and the backend route log.

**`make e2e` — component tier (bypasses Hermes).** A fast lower-tier check that drives the
BYOK gateway model + the brain over MCP **directly** (NOT through the Hermes agent /
`/api/ws`), for the same scenario + negatives, in a fresh isolated `DEPLOY_PROJECT=hermes-e2e`
stack. Use `make demo` for the real thing; this is the quick component check. Code:
[`deploy/hermes/e2e/`](e2e/). It runs in CI
([`.github/workflows/e2e.yml`](../../.github/workflows/e2e.yml)) with a low gateway
spend cap; a failed run uses GitHub's default failure notification and uploads the
transcript.

**Layer 2 — packaged-app smoke test (Windows, unattended, no clicks):**
One-time, by hand: install Nous's **official** Hermes Desktop (the non-Store installer,
see "keeping the desktop on v0.21.7" above), and make sure the demo stack + 3 users are
provisioned. Then just run:
```powershell
deploy\hermes\smoke-packaged.ps1
```
and **read the pass/fail table**. Without ever typing into or clicking the app, it:
detects the installed app and warns if it is not v0.21.7; for marcus then priya runs
`connect-desktop.ps1`, launches the app, and confirms **server-side** that the session
landed on that user's backend and **not** the other's (the signal is `GET /api/ws` in
the backend's route-proxy log — `deploy/hermes/gated/route_proxy.py`); screenshots the
screen; quits cleanly; and checks the telemetry setting is off. Screenshots + a
`results.csv` are saved to a dated folder it prints.

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
