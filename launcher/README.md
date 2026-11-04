# Company Brain launcher (M1 — Windows alpha)

A small desktop app (Tauri 2, ~1.7 MB installer) that signs you in with an invite link, shows
the brains you can use **by name only**, and launches the **stock** Hermes Desktop already
connected to your own Company Brain backend. It never modifies Hermes: it writes Hermes's own
`connections.json` (v2 registry) and starts the app.

| | |
|---|---|
| Sign in | paste the invite link (`https://<server>/invite#cb_inv_…`), single use |
| Library | brain names from `GET /launcher/me` |
| Launch | `POST /launcher/launch` → a 12 h desktop token for *your* backend, written into Hermes's registry; Hermes started |
| Account | name, company, role, server health, signed-in devices (sign out others), **admin only:** set the company model key (opens the key-entry page in the browser) |
| Switch user / Sign out | revokes this device on the server, removes our entries from Hermes's registry (Hermes falls back to its local connection), forgets the refresh token |

What it keeps on this computer: `%APPDATA%\CompanyBrain\launcher.json` = `{"server": "<origin>"}`,
and the refresh token in Windows Credential Manager (`CompanyBrainLauncher`). No brain content,
no provider keys. The page never sees a token: every call is a Rust command
(`src-tauri/src/main.rs`), and WebView2 autofill is off (`tauri.conf.json`) so the invite link is
never saved by the webview.

**Not the launcher's to control:** stock Hermes Desktop keeps its own durable cache of recent
transcript tails in its Local Storage (`src/store/transcript-tail-cache.ts`, up to 50 sessions).
See the M1 report.

## Install (partners)

1. Download `Company Brain_<version>_x64-setup.exe` from the GitHub release.
2. Windows shows **"Windows protected your PC"** because the installer is not code-signed yet.
   Click **More info**, check *Application: Company Brain_…_x64-setup.exe*, then **Run anyway**.
3. The installer is per-user (no admin prompt) and goes to `%LOCALAPPDATA%\Company Brain`.
4. Install the official Hermes Desktop (Microsoft Store or Nous's release page) if you do not have it.
5. Open Company Brain, paste the invite link you were sent, click **Launch Hermes**.

## Build and test (developers)

```bash
cd launcher
npm ci                                  # @tauri-apps/cli 2.12.1 (exact)
npm test                                # cargo test: registry merge/refusals, invite parsing, version policy
./node_modules/.bin/tauri build         # -> src-tauri/target/release/bundle/nsis/*.exe
```

Contract test against Hermes's own registry normalizer (needs a hermes-agent checkout at the
backend's pinned tag with `npm ci` done, and Node ≥ 26 for v0.22.6):

```bash
(cd src-tauri && CB_CONTRACT_OUT=/tmp/cb-contract cargo test emit_contract_fixtures)
HERMES_DESKTOP_SRC=<hermes-agent>/apps/desktop CB_CONTRACT_OUT=/tmp/cb-contract \
  <hermes-agent>/node_modules/.bin/tsx contract/registry-contract.mts
```

Local end-to-end against the compose stack: `e2e/stack-up.sh` brings up an isolated project in
`INGRESS_MODE=launcher` with priya + ada and writes one invite link per user. To launch a
from-source Hermes instead of an installed MSIX, start the launcher with
`CB_LAUNCHER_HERMES_EXE`, `CB_LAUNCHER_HERMES_ARGS_JSON`, `CB_LAUNCHER_HERMES_CWD` and
`CB_LAUNCHER_USER_DATA_DIR` (see `src-tauri/src/hermes.rs`); `CB_LAUNCHER_DATA_DIR` and
`CB_LAUNCHER_KEYRING_SERVICE` isolate a test run's own state.

## Known limits (alpha)

- Windows only. Unsigned installer (SmartScreen click-through above); no auto-update.
- Packaged Hermes is detected via MSIX only (Store / out-of-store App Installer). An MSIX
  install has not been exercised end to end on this machine (none installed); the e2e used a
  from-source v0.22.6 build through the dev-command hook.
- Every installed Hermes build is "untested" until its package version is recorded in
  `hermes_desktop_builds` (control DB); "blocked" builds are refused.
- A dev-command Hermes is not restarted by the launcher; a packaged one is (after you confirm).
