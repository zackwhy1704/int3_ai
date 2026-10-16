# Quickstart — watch the real Hermes agent run the Company Brain demo

**Audience: a tester (or a tester's Claude Code) with repo access.** One command brings up
an isolated stack and drives the **real, stock Hermes agent** through the Company Brain
demo. "Hermes under the hood" is literally true: every step in the demo goes over the same
`/api/ws` connection the desktop app uses.

> **The one secret you provide is the LLM key, and you put it in a file yourself.**
> Edit `deploy/hermes/.env` and set `ANTHROPIC_API_KEY=sk-ant-…`. **Never paste a key
> into chat, and never ask anyone to** — not into Claude Code, not into a terminal that
> logs. `make demo` generates every *other* secret (DB passwords, KEKs) for the throwaway
> stack on its own.

## 1. Prerequisites

- **Docker running**, with **Compose v2** (`docker compose version`). Docker Desktop ships
  it; on Linux: `sudo apt-get install docker-compose-plugin`.
- **≥ 8 GB free memory** for the stack (local embedding model + Postgres + per-user
  containers). `make demo` checks this and tells you if you're short.
- **`bash`.** On Windows use **Git Bash** (or WSL). `make` is macOS/Linux only — on Windows run
  the scripts directly (shown below). See Troubleshooting for the one Git Bash gotcha (handled
  in-repo).
- **`deploy/hermes/.env` with your model key.** If the file is missing:
  `cp deploy/hermes/.env.example deploy/hermes/.env`, then edit it and set
  `ANTHROPIC_API_KEY=sk-ant-…` yourself.

`make demo` runs these checks first and, on failure, prints the exact fix and stops.

## 2. Run it

**macOS / Linux:**
```sh
make demo
```

**Windows (Git Bash has no `make`)** — run the script directly:
```sh
bash deploy/hermes/demo/make-demo.sh
```

This (in an **isolated** project `DEPLOY_PROJECT=hermes-demo` — it never touches a running
`hermes-deploy`): checks prerequisites → generates any missing non-model secrets → builds
images → brings the stack up → provisions three synthetic users (marcus=finance,
priya=operations, ada=operations **owner**), each with your key under a **$3/day cap** (the
`DEMO_CAP_USD` default in `make-demo.sh`) → runs the scenario **through the real Hermes agent over
`/api/ws`** → prints a ✅/❌ table. It **leaves the stack up** so you can open the desktop app
(next section). Remove it with:

```sh
make demo-down                                                   # macOS / Linux
DEPLOY_PROJECT=hermes-demo bash deploy/hermes/e2e/teardown.sh    # Windows (Git Bash)
```

### What it costs

`make demo` runs the **real** Claude Sonnet agent, so it spends real money on **your** Anthropic key.
The cost **is measured**: the gateway records every call's `spend_usd` and `request_count` per
tenant (`tenant_usage`, `gateway/limits.py`), and each call's token counts to a usage log
(`gateway/upstream.py`). `make demo` prints a **USAGE** block at the end — per-user tokens
(input/output/cache-read/cache-write) and per-tenant `spend_usd` + `request_count`.

The last validation run (GitHub 38846845310) metered **≈ $0.46** at the placeholder price table
(input/output `$3`/`$15` per Mtok, cache read `0.1×`, cache write `1.25×` — all marked **UNVERIFIED**
in `gateway/limits.py`; set real prices via `GATEWAY_PRICES`). Prompt caching is doing the heavy
lifting: most input tokens are cache *reads* billed at `0.1×`, reused across the demo's sessions.
(An earlier figure of `$2.46` was a metering bug that billed cache tokens at the full input rate;
fixed.) A run is bounded by the gateway's **per-tenant daily cap, default `$3`** (`DEMO_CAP_USD` in
`make-demo.sh`). Set a hard spend limit on your Anthropic account as the real backstop — the gateway
cap is a per-run ceiling, not an account budget. Raise `DEMO_CAP_USD` deliberately if needed.

## 3. Read the table

Each row is a claim about what Hermes *returned* (its final text / the approval prompts it
showed), backed by DB rows and the backend route log — not by us calling the brain
directly. In order:

1. **marcus** asks a finance question → Hermes answers, **cited to finance**.
2. **priya** (operations) asks the *same* finance question → Hermes **refuses**, and none
   of the finance canary strings leak.
3. **priya** teaches an operations fact → the Hermes **approval prompt shows the exact
   statement**; she approves it (the desktop "Run").
4. **priya tries to approve her own / someone's proposal** → Hermes says she can't (she is
   not the scope owner).
5. **ada** (owner) approves priya's proposal → the prompt shows the statement **and the
   proposer**.
6. **priya re-asks** → Hermes now answers, **cited to the operations workflow record**.
7. Declining a teach leaves **0 proposal rows**; `fetch https://example.com` is **blocked**
   (default-deny egress); **isolation checks pass**.

A non-zero exit means at least one row failed; the table shows which.

> **Component-tier check (bypasses Hermes):** `make e2e` (macOS/Linux) — or on Windows
> `bash deploy/hermes/e2e/make-e2e.sh` — runs the same scenario by driving the gateway model +
> brain MCP **directly**, without the Hermes agent. It's the faster lower-tier check; `make demo`
> is the real thing. Remove it with `make e2e-down`, or on Windows
> `DEPLOY_PROJECT=hermes-e2e bash deploy/hermes/e2e/teardown.sh`.

## 4. (Optional) See it in the desktop app

The demo already proves the agent path; this step just lets you watch it in the GUI.

**Install the official, stock Hermes Desktop, pinned to the demo's version.** The installer is
Nous's, in the `NousResearch/hermes-agent` repo (not this repo). On Windows, download it from the
v0.21.7 tag to `%TEMP%` and run it with the desktop stage, pinned to the commit:

```powershell
$u = 'https://raw.githubusercontent.com/NousResearch/hermes-agent/v0.21.7/scripts/install.ps1'
Invoke-WebRequest -Uri $u -OutFile "$env:TEMP\hermes-install.ps1"
& "$env:TEMP\hermes-install.ps1" -IncludeDesktop -Commit 71d34bad1045610252eda02ee79549436896e784
```

`-IncludeDesktop` (`scripts/install.ps1:33` in hermes-agent) adds the desktop stage; `-Commit`
pins it to tag v0.21.7 (`:962`); the build produces **`release\win-unpacked\Hermes.exe`** (`:1232`).
That unpacked build is a standalone app (not the Store package), which is what you want for a
pinned demo. Then point it at a user's connect file:

```powershell
# writes the connection into the app's data dir and (re)starts it at the demo backend
deploy\hermes\connect-desktop.ps1 -Tenant brindlewood -User priya
```

On macOS use `deploy/hermes/connect-desktop.sh --tenant brindlewood --user priya`.

The connect file (`deploy/hermes/connect/hermes-demo/brindlewood-<user>.connect.json`,
written by provisioning) carries `url = http://127.0.0.1:<port>` and a `session_token`.
The desktop dials that port (a host-ingress forwarder → the user's gated backend).

**About updates / pinning (Windows).** The official app is distributed as an **MSIX**
(`apps/desktop/electron-builder.config.cjs:236`, `target: ['msix']`). The Microsoft Store
and out-of-store MSIX (App Installer) paths **auto-update** — the App Installer feed checks
on launch at most every 12 h (`updater/app-installer.ts`;
`scripts/bundles/release_artifacts.py:291-292`) — so they **cannot be pinned per app**. A
`win-unpacked` standalone build updates only via an explicit update action
(`updater/index.ts:8-9`). (Note: `apps/desktop/electron/updater/mac-client.ts` is
**macOS-only** — it gates on `platform !== 'darwin'` at `:21` and uses `MacUpdater()` at
`:38` — so its `autoDownload=false` is **not** evidence about Windows.)

## Does the desktop run its own backend, or does the remote connection replace it?

Answered from the Hermes source (`apps/desktop/electron`, tag v0.21.7):

- **Yes, the desktop can start its own local backend.** For a **local** profile it launches
  a headless `hermes serve` on loopback — `serveBackendArgs()` returns
  `['serve','--host','127.0.0.1','--port','0']` (`backend-command.ts:26`; intro at `:3`).
  There is **one** such backend per host, multiplexed across local profiles
  (`backend-discovery.ts:3`).
- **But an active *remote* connection bypasses it.** When the active/primary connection is a
  remote gateway (our per-user gated backend), the desktop **routes requests to that remote
  backend** and does **not** spawn a local child for it — "Route through that gateway … instead
  of spawning a fresh local backend" (`connection-config.ts:886-892`), using the
  "(cloud) primary backend rather than spawning a local child" (`backend-start-failure.ts:28`).

So for the demo's connect-file (remote) connection, **the agent that answers is our gated
container** — the local `hermes serve` is not in that connection's request path (and if no
local profile is active, none is spawned). That is why the demo can claim the stock Hermes
agent, running in our sandbox, is doing the work.

## Troubleshooting

- **Out of memory / builds killed / `cannot allocate memory`.** The stack wants ≥ 8 GB
  free. **First, stop any running `hermes-deploy` containers** (Docker Desktop → Containers →
  Stop the `hermes-deploy` stack) — running it beside the demo stack exhausts host memory. The
  demo is isolated (`hermes-demo`) and won't touch `hermes-deploy`, but both running at once is
  two full stacks. Then close other apps, or use a machine with more RAM.
- **Port already in use.** The demo binds host ports **9331/9332/9333** (off the deploy
  stack's 9131–9133 and the e2e stack's 9231–9233). If something else holds them, stop it
  or change the ports in `deploy/hermes/demo/make-demo.sh`. `make demo-down` frees the
  demo's ports.
- **Git Bash mangles container paths** (`exec /opt/proxy/startup-gate.sh … exit 127`, paths
  rewritten to `C:/Program Files/Git/opt/…`). The repo scripts already
  `export MSYS_NO_PATHCONV=1` to stop this; if you run a command by hand, set it too.
- **`ANTHROPIC_API_KEY is not set`.** Edit `deploy/hermes/.env` and set it yourself (see the
  box at the top). The demo will not prompt you for it and you should not paste it anywhere.
- **Nothing happens / the agent hangs on approval.** The scenario answers approval prompts
  for you (Run/Deny). If a step times out, re-run `make demo`; the scenario is idempotent
  and tags its taught facts per run.

## All data is synthetic

Every tenant, user, document and fact in the seed and the demo is **invented**. There is no
real company's data anywhere in this repo, in logs, or in fixtures — by rule, never.
