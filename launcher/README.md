# int3 launcher (M1 — Windows alpha)

A small desktop app (Tauri 2, ~1.7 MB installer) that signs you in with an invite link, shows
the brains you can use **by name only**, and launches the **stock** Hermes Desktop already
connected to your own company-brain backend. It never modifies Hermes: it writes Hermes's own
`connections.json` (v2 registry) and starts the app. Identifier `ai.int3.launcher` (final: the
WebView2 profile and the installer's upgrade identity hang off it; never change it).

| | |
|---|---|
| Sign in | paste the invite link (`https://<server>/invite#cb_inv_…`), single use |
| Library | brain names from `GET /launcher/me` |
| Launch | `POST /launcher/launch` → a 12 h desktop token for *your* backend, written into Hermes's registry as **int3 - &lt;user&gt;@&lt;tenant&gt;**; Hermes started |
| Account | name, company, role, server health, signed-in devices (sign out others), **admin only:** set the company model key (opens the key-entry page in the browser) |
| Switch user / Sign out | revokes this device on the server, removes our entries from Hermes's registry (Hermes falls back to its local connection), forgets the refresh token |
| No Hermes | **Get Hermes Desktop** opens Nous's product page, `https://hermes-agent.nousresearch.com/desktop` (linked from Hermes v0.22.6 `website/docs/user-guide/desktop.md:25`; it carries the Microsoft Store listing and the `.appinstaller`). On Windows builds below 22621 the launcher says Hermes needs Windows 11 22H2 (`apps/desktop/BUILDING.md:14`). |

What it keeps on this computer: `%APPDATA%\int3\launcher.json` = `{"server": "<origin>"}`, and
the refresh token in Windows Credential Manager (`int3Launcher`). No brain content, no provider
keys. The page never sees a token: every call is a Rust command (`src-tauri/src/main.rs`), and
WebView2 autofill is off (`tauri.conf.json`) so the invite link is never saved by the webview.

## What Hermes itself keeps on disk (not the launcher's to control)

Stock Hermes Desktop keeps a **durable cache of recent transcript tails** in its own Local
Storage so a reopened chat paints instantly: `apps/desktop/src/store/transcript-tail-cache.ts`
@v0.22.6, up to 50 sessions (`MAX_ENTRIES`), at most 256 KB per entry, keyed per connection,
no setting to turn it off. So the last answers a user saw from their brain **are on that
laptop** inside Hermes's profile (`%APPDATA%\Hermes\Local Storage` for a packaged install), even
after the launcher signs out. Observed in the M1 no-brain-content scan.

Decision for the MVP (owner, 2026-11-04): **document, no wipe.** Wiping Hermes's Local Storage
on sign-out would also erase Hermes's own UI preferences and is a write into Hermes's data. Told
to partners in `docs/PARTNER_GUIDE.md`. Revisit before any customer with a shared-laptop or
data-at-rest requirement (options: wipe on sign-out, ask Nous for a setting).

## Install (partners)

See `docs/PARTNER_GUIDE.md`. Short version: download `int3_<version>_x64-setup.exe`, click
**More info → Run anyway** on the SmartScreen prompt (unsigned), install the official Hermes
Desktop (Windows 11 22H2+), paste your invite link, **Launch Hermes**.

## Build and test (developers)

```bash
cd launcher
npm ci                                  # @tauri-apps/cli 2.12.1 (exact)
npm test                                # cargo test: registry merge/refusals, invite parsing, version policy, OS build parse
./node_modules/.bin/tauri build         # -> src-tauri/target/release/bundle/nsis/int3_<version>_x64-setup.exe
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

### Testing an official Microsoft Store Hermes (Windows 11 22H2+)

```bash
bash launcher/e2e/stack-up.sh                       # once, if no launcher-mode stack is up (INVITE_DIR=…)
bash deploy/hermes/store-test.sh --user priya       # invite -> clipboard (never printed); or --out <file>
# install int3 + Store Hermes, paste the invite, Launch; note the version the launcher shows
bash deploy/hermes/store-test.sh --record <package-version> --release v0.22.6
```

`--record` writes `hermes_desktop_builds`; from then on the launcher reports that build with the
release's state (`supported` for v0.22.6) instead of "untested".

## Local mode (M1)

For a tenant with `launch_mode = 'local'`, Launch does not connect Hermes to a hosted backend. It sets up
the user's own Hermes and points it at the int3 brain (docs/LOCAL_MODE_DESIGN.md):

1. fetch `/launcher/local/policy`;
2. apply it with Hermes's own `hermes config set`;
3. write the int3 block into `SOUL.md`, and `HERMES_WRITE_SAFE_ROOT` into Hermes's `.env`;
4. read `config.yaml` back and send it to `/launcher/local/posture`;
5. only on an ok verdict, write `INT3_BRAIN_TOKEN` and start Hermes.

While the launcher runs, it re-reports within about 15 s of any change to `config.yaml`, `.env` or
`plugins/`. Sign-out removes the token from `.env`.

| Dev/test override | Default |
|---|---|
| `CB_LAUNCHER_HERMES_HOME` | `%LOCALAPPDATA%\hermes` |
| `CB_LAUNCHER_HERMES_CLI` | the MSIX alias `%LOCALAPPDATA%\Microsoft\WindowsApps\hermes.exe`, else `hermes` |
| `CB_LAUNCHER_WORKSPACE` | `%USERPROFILE%\int3-workspace` |
| `CB_LAUNCHER_HERMES_SCHEME` | `hermes` (dev Desktop registers `hermes-dev`) |

## Known limits (alpha)

- Windows only. Unsigned installer (SmartScreen click-through); no auto-update.
- Packaged Hermes is detected via MSIX only (Store / out-of-store App Installer), which needs
  Windows 11 22H2. Hermes on Windows 10 exists only as Nous's source install (`install.ps1`),
  which the launcher does not detect.
- An MSIX install has not been exercised end to end yet (the dev machine is Windows 10); the
  e2e used a from-source v0.22.6 build through the dev-command hook.
- Every installed Hermes build is "untested" until its package version is recorded (above). Untested
  warns on screen and still launches, showing the exact version; only "blocked" builds are refused.
- A dev-command Hermes is not restarted by the launcher; a packaged one is (after you confirm).
