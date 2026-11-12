# int3 local mode: local agent, hosted brain

**Status: design for review. Nothing here is built.** The running hosted pilot (`int3-pilot`, tenant
`marcuspilot`) is untouched and becomes the future "secure hosted mode".

Source of every Hermes claim: Hermes Agent **v0.23.1** (`b65acf3fdc`, released 10 Nov 2026), cited as
`path:line` at that tag. Our server still pins v0.22.6. Our own code is cited as `int3_ai/path:line`.

---

## In plain language

**What changes.** Today, each person's assistant runs on our server. In local mode it runs on their own
PC, inside the official Hermes app they install from the Microsoft Store. Only two things stay on our
server:

- the **company brain**, the shared knowledge with its sources and permissions; and
- each person's **memory**: what the assistant remembers about them between conversations.

**How people use it.** They open the int3 app, sign in, and click Launch. Hermes opens, already
connected to their company brain and their memory. They choose one AI model: a Claude key, an OpenAI
key, their ChatGPT sign-in, or a model running on their own PC.

**What happens when someone signs out or stops paying.** The brain and the memory stop answering, right
away. Hermes itself keeps working as a plain assistant. Their past chats stay on their PC; that's
accepted for the pilot.

**Keeping company knowledge from leaking.** In the pilot we switch off the assistant's riskiest
abilities by default: running commands, running code, browsing the web, and web search. The brain only
ever answers within the person's own permissions. Every question is rate-limited and recorded. People
who use a model on their own PC get a fully private path: the brain sends back the relevant passages
only, and their own model writes the answer, so company text never goes to a cloud AI.

**What we can't promise.** The PC belongs to the person. Someone determined can switch those abilities
back on. We can make that hard to do by accident, notice when it happens, and stop the brain answering
on a weakened setup, but we can't make it impossible. If that isn't acceptable for a customer, the
hosted mode is the answer.

**Chat apps.** On day one the assistant can be reached from chat apps where the PC connects *out*:
Slack, Telegram, Discord, email and Signal. Each bot only answers its owner. Apps that need to push
messages *in* (Teams, WhatsApp Business, LINE, SMS) come later, through our server.

---

## 0. Owner decisions and M1 status (2026-11-13)

**Decisions** (from the design review):
1. Detect-and-refuse is accepted for the pilot; hosted mode is the high-security tier.
2. **File writes stay on, confined** to `%USERPROFILE%\int3-workspace` via `HERMES_WRITE_SAFE_ROOT` in
   Hermes's `.env`. The launcher refuses the folder if it resolves into OneDrive or another synced folder,
   or if it is a link (see 3.1).
3. **Passages-only for ALL models in local mode**, with `/v1/validate` citation checks. No server-side
   model key in local mode; server-written answers stay a hosted-mode feature (brain tokens get 403 on
   `/v1/brain_ask`).
4. Memory: kept while the tenant is active; users view and delete theirs in int3; purged 30 days after
   cancellation (configurable); deletions audited. (M2)
5. Messaging day 1: Slack (Socket Mode) and Telegram (polling), owner-only allowlist. Signal dropped;
   email later; Teams/WhatsApp via our day-2 relay plugin. (M4)
6. Brain token about 24 h; revocation checked on every call; on expiry Hermes shows "reopen int3".
7. The extra lockdown list is adopted, minus `file` (decision 2).

**The two open checks, resolved at v0.23.1:**
- **`hermes config set` writes lists and mappings.** Run on this PC against a scratch `HERMES_HOME`:
  - `agent.disabled_toolsets '[terminal, ...]'` is stored as a YAML list and replaced wholesale;
  - the `mcp_servers.int3_brain` mapping is stored with `${INT3_BRAIN_TOKEN}` kept literal;
  - a wrong shape is refused (`hermes_cli/config.py:3135-3147,3165,3186-3205`);
  - each call takes about 1.2 s.
- **Providers deep link: `hermes://open/settings?tab=providers`.** Delivered to a running v0.23.1 Desktop
  (dev scheme `hermes-dev://`), it opened Settings › Providers › Accounts (`apps/desktop/src/lib/hermes-open-target.ts:1-29`,
  `apps/desktop/src/app/settings/index.tsx:123`). Windows protocol registration for the Store's `hermes://`
  was not exercised (dev runs skip it).

**Write confinement, verified at v0.23.1.**
- `HERMES_WRITE_SAFE_ROOT` is read on every check (`agent/file_safety.py:223-229`) and enforced on
  write, patch, delete and move (`tools/file_operations.py:1310,1349,1530,1630`).
- Running Hermes's own guard on Windows: outside paths, `..` escapes, a sibling folder sharing the prefix,
  a junction to outside and `HERMES_HOME\.env` are all **denied**; case and slash variants inside the root
  are **allowed**.
- **Reads are not confined** by this variable. The file tools can read other user files and send them to
  the chosen model.

**Revised effort:**

| Milestone | Scope | Effort |
|---|---|---|
| M1 | Launcher config writer + SOUL + brain MCP entry; multi-user MCP endpoint (passages-only + validate); report + refusal; Providers link; per-user limits + audit | **Built** (about 6 days) |
| M2 | Memory plugin (stdlib), memory API + schema, view/delete in int3, 30-day purge job, deletion audit | 7–9 days |
| M3 | Ollama path: pin auxiliary/compression/vision to the local model, no fallback, refuse if cloud keys are present | 3–4 days |
| M4 | Slack + Telegram setup in int3 (owner allowlist), Telegram webhook mode refused | 4–5 days |
| Test | First partner session (Win11 Store) | 2–3 days |
| | **Remaining pilot total** | **about 3–4 weeks** |

**M1 as built** (on `spike/remote-thin-client`; the hosted pilot is untouched):

| Piece | Where |
|---|---|
| Brain token kind `cb_bt_` (24 h), `tenants.launch_mode`, `launcher_posture`, `brain_audit` | `backend/app/control_schema.sql`, `backend/app/launcher/tokens.py` |
| Policy + server-side verdict | `backend/app/launcher/local_policy.py` |
| `/launcher/local/policy`, `/posture`, `/launch` (edge-allowlisted) | `backend/app/launcher/routes.py`, `deploy/hermes/gated/launcher-edge.allowlist` |
| Brain-token auth on `/v1`: live revocation, posture, limits, route allowlist, audit | `backend/app/launcher/brain_auth.py`, `backend/app/auth/principal.py`, `backend/app/api/v1.py` |
| Multi-user MCP endpoint (no own credential) | `deploy/hermes/brain-mcp/local_server.py`, compose profile `local-mode` |
| Launcher local mode: apply, read back, report, token, start, file watcher, Choose AI model | `launcher/src-tauri/src/local.rs`, `main.rs`, `ui/` |

**New findings from the M1 run:**
- **Telemetry prompt:** Hermes shows its own "Share with Nous / No thanks" consent prompt even with
  `telemetry.shared_metrics.enabled: false`. A "Share" click isn't in the posture report yet.
- **Other AI sign-ins on the PC:** Hermes finds other AI tools' sign-ins (it showed this PC's Claude Code
  account as "Connected · managed by its own CLI"). A user's personal subscription can become the model.
  That's a model choice, not a brain leak, but partners should know.
- **Extras in dev builds:** a source-built Hermes installs provider and MCP extras through its own package
  manager on first use. The Store bundle ships them (`apps/desktop/BUILDING.md:34`), so this is a
  dev-only gap.

## 1. Decision summary

| Topic | Decision | Why |
|---|---|---|
| Agent | Stock Hermes Desktop, local backend | No Hermes containers per user. Config and plugins only; no code changes |
| Brain | Our MCP endpoint over HTTPS: one multi-user service | Remote MCP is a supported Hermes feature (`website/docs/reference/mcp-config-reference.md:15-75`) |
| Brain auth | **A: static header** `Authorization: Bearer ${INT3_BRAIN_TOKEN}`, short-lived, rotated by the launcher | Revocation is server-side and immediate; nothing in Hermes to change; launcher stays the only login |
| Memory | Our memory-provider plugin; built-in memory **off** | Supported, public plugin API. Off is two config keys; replacing the toolset would also gate off our tools (§3) |
| Config | Launcher writes it with `hermes config set` while Hermes is stopped, then reads it back | Supported CLI (`website/docs/user-guide/configuration.md:50-67`); routes secrets to `.env` |
| Tool lockdown | `agent.disabled_toolsets` plus a minimal `platform_toolsets.cli` | The subtraction applies last, even to the coding posture and Desktop's own fold-ins (§1) |
| Tamper | **Detect and refuse, not prevent** | The PC is the user's. Server-side scopes, rate limits and audit are the real boundary |
| Answers | Cloud models: server-written answers (current). Ollama: passages plus validated citation ids | Keeps structural citations where we can; keeps content local where we must (§4) |
| Messaging day 1 | Slack, Telegram, Discord, Email, Signal (outbound-only), owner-only allowlist | Verified in source (§5) |
| Messaging day 2 | Our own platform plugin that pulls from our relay; revisit Hermes Relay when it's stable | Hermes Relay is experimental and its connector isn't public (§5) |

---

## 2. Architecture

```
Client PC (Windows 11 22H2+)                                   int3 server (HTTPS only)
┌───────────────────────────────────────────────┐            ┌────────────────────────────────────┐
│ int3 launcher                                 │  sign-in   │ launcher edge  /launcher/*          │
│  sign in · library · launch · log out ────────┼──────────▶│  device session, brain token mint   │
│  writes config + INT3_BRAIN_TOKEN, installs   │            │                                     │
│  the int3 memory plugin, starts Hermes        │            │ brain MCP endpoint (multi-user)     │
│                                               │            │  token → tenant/user → scopes       │
│ Stock Hermes Desktop + its LOCAL backend      │   MCP      │  rate limit · audit · ask/search    │
│  agent loop, chat history (stays on PC)  ─────┼──────────▶│  /validate (citation ids)           │
│  tools: brain (MCP), clarify, todo, memory*   │            │                                     │
│  memory provider = int3 plugin           ─────┼──────────▶│ memory API (per user, per tenant)   │
│                                               │  HTTPS     │                                     │
│  model: Claude / OpenAI / ChatGPT / Ollama ───┼──▶ the     │ brain DB (per tenant) · control DB  │
│  messaging gateway (optional, pull only) ─────┼──▶ chat    └────────────────────────────────────┘
└───────────────────────────────────────────────┘    platforms
* memory tools come from our plugin; the built-in memory store is off
```

What's gone compared with hosted mode: the per-user Hermes containers, ingress proxies, route allowlist
and egress proxy. What's new: one multi-user MCP endpoint, the memory API, and a client-side config the
launcher owns.

---

## 3. Investigation

### 3.1 Local config

**Where config lives.**
- Store/MSIX Desktop resolves `HERMES_HOME` as: the `HERMES_HOME` env var, then the Windows user env
  var from the registry, then the default `%LOCALAPPDATA%\hermes`
  (`apps/desktop/electron/data-paths.mjs:26-33,41-66`; the backend agrees,
  `hermes_constants.py:56-64,194-200`).
- **No AppData virtualization at v0.23.1.** The MSIX manifest disables file-system and registry write
  virtualization and requests `unvirtualizedResources` (`apps/desktop/electron-builder.config.cjs:276-278`,
  enforced by `apps/desktop/scripts/msix-virtualization.test.mjs:72-78`). Store Hermes therefore reads
  and writes the real `%LOCALAPPDATA%\hermes`, which simplifies the launcher. Its M1 code writes both
  real and per-package copies of Desktop's `connections.json`; that stays harmless.
- Layout: `config.yaml` (settings), `.env` (secrets), `auth.json` (OAuth provider credentials),
  `plugins/`, sessions and logs (`website/docs/user-guide/configuration.md:20-30`,
  `website/docs/user-guide/windows-native.md:249-258`).

**Exact keys the launcher sets:**

```yaml
memory:
  memory_enabled: false            # built-in MEMORY.md off   (hermes_cli/config_defaults.py:1296)
  user_profile_enabled: false      # built-in USER.md off     (:1297)
  nudge_interval: 0                # no built-in review nudges (:1306)
  provider: int3                   # our plugin                (:1309)
agent:
  disabled_toolsets: [terminal, code_execution, browser, web, search, x_search, computer_use,
                      image_gen, video_gen, tts, connections, delegation, cronjob,
                      desktop_ui, kanban]       # (:266); names from toolsets.py:72-153
approvals:
  mode: manual                     # smart | manual | off (website/docs/user-guide/security.md:30-62)
  cron_mode: deny
  unattended_mode: deny
mcp_servers:
  int3_brain:
    url: "https://<brain-id>.app.int3labs.app/mcp"
    headers: {Authorization: "Bearer ${INT3_BRAIN_TOKEN}"}   # ${VAR} from .env (mcp-config-reference.md:77-90)
    tools: {include: [ask_brain, search_brain, validate_citations, propose_claim,
                      list_proposals, approve_proposal, reject_proposal]}   # (:114-155)
    timeout: 60
telemetry:
  shared_metrics: {enabled: false, send: false}   # already the default (config_defaults.py:2311-2318)
fallback_providers: []             # no silent second model (:24)
skills:
  inline_shell: false              # already the default (:1452)
```

- **Toolset names.**
  - The four you named: `terminal` (`toolsets.py:101`), `code_execution` (`:151`), `browser` (`:110-114`),
    and `web` / `search` (`:72-73`).
  - I propose adding:
    - `x_search`, `computer_use`, `image_gen`, `video_gen`, `tts` and `connections`, which send data to
      third parties;
    - (`file` stays on: writes confined by `HERMES_WRITE_SAFE_ROOT`, decision 2 in section 0);
    - `delegation` and `cronjob`, which spawn unattended agents;
    - `desktop_ui`, whose `drive_preview` and `read_terminal` let the agent drive Desktop's browser pane
      and read its terminal (`toolsets.py:135-141`);
    - `kanban`.
  - That leaves the brain, our memory tools, `clarify`, `todo`, `vision` (local image to the chosen
    model), `skills` (read/view) and `session_search` (local history).
- **Do not** list `memory` in `disabled_toolsets`. Gating the `memory` toolset off also gates off the
  external provider's tools and prompt block (`agent/memory_manager_tools.py:72-80`). The built-in tool
  hides itself once both store flags are false (`tools/memory_tool.py:312-318`).
- **Identity and routing.** Don't edit the user's `SOUL.md`. Our plugin's `system_prompt_block()` is the
  supported place for a static instruction like "for company questions use the int3 brain"
  (`agent/memory_provider.py:140-142`).

**Does Desktop overwrite or reject these? Can they change while Hermes runs?**
- Desktop's Tools toggles write only `platform_toolsets.cli` (`hermes_cli/web_routers/tools.py:273-301`).
  They never touch `agent.disabled_toolsets`, which every Desktop session subtracts last, including from
  the coding posture and client fold-ins (`tui_gateway/agent_factory.py:335-341,402-438,446-462,678`;
  `toolsets.py:447-458`). Upgrades respect it too: migration 45 skips toolsets already in the subtraction
  list (`hermes_cli/config_migrations.py:582-596`).
- Desktop's settings can edit `approvals.mode` (`apps/desktop/src/app/settings/constants.ts:236`), offer a
  YOLO toggle (`apps/desktop/src/app/contrib/controller.tsx:758`) and change `memory.provider`
  (`apps/desktop/src/app/settings/config-field.tsx:180`). The web dashboard has a raw config editor
  (`hermes_cli/web_routers/analytics.py:34-60`).
- Changes while running: config is re-read when a session is built (`tui_gateway/agent_factory.py:418-427`).
  MCP servers are built from config at load (`tools/mcp_tool_config.py:699-731`) and reconnect on
  `/reload-mcp` or restart (`tools/mcp_tool_lifecycle.py:127`). The memory provider is chosen at agent
  init (`agent/agent_init.py:1205-1225`).
- **Launcher rule:** write config only while Hermes is stopped (the M1 launcher already stops and restarts
  it), then read it back with `hermes config get` and refuse to hand out a brain token if anything differs.

**Re-enabling disabled tools: prevent or detect? Detect.**
- The user owns `%LOCALAPPDATA%\hermes`. They can edit it, or open the raw editor.
- What we can do:
  1. Layering, so a casual UI toggle doesn't work (above).
  2. **Posture reporting.** Our plugin reads the effective config at `initialize()` and on each
     `on_turn_start` (`agent/memory_provider.py:121-133,178-190`). It sends the server a posture
     fingerprint covering:
     - the disabled toolsets;
     - `approvals.mode` and YOLO;
     - the `mcp_servers` names;
     - `memory.provider`;
     - the installed plugins;
     - the Hermes version.
  3. **Server refusal.** The brain endpoint refuses a device whose last posture is missing, stale (more
     than 10 minutes) or weakened. Memory goes read-only and the user sees why.
- This is spoofable by someone willing to edit our plugin; it stops accidents and casual changes, not a
  determined insider. That limit is stated in the partner guide. Hosted mode covers customers who need more.

### 3.2 Brain auth over remote MCP

| | A: static header, short-lived token | B: Hermes MCP OAuth |
|---|---|---|
| Hermes support | `headers` with `${VAR}` from `.env` (`mcp-config-reference.md:52,77-90`) | `auth: oauth`, PKCE, DCR or CIMD, device flow (`:335-420`) |
| Who logs in | The launcher only (our gatekeeper) | Hermes opens a browser to our authorization server: a second login |
| Revocation | Immediate: every call is checked server-side, as the hosted ingress does today (`int3_ai/deploy/hermes/ingress/ingress_auth.py:13-21`) | Immediate for calls; Hermes keeps a refresh token and retries |
| Token on disk | `%LOCALAPPDATA%\hermes\.env` (plain text, user-only) | `mcp-tokens\int3_brain.json` with a refresh token (`:350`) |
| Rotation into a running Hermes | Needs `/reload-mcp` or a restart: headers resolve at config load | Automatic refresh |
| Server work | Reuse the launcher token tables, plus introspection | A full OAuth authorization server (CIMD/DCR, PKCE, device flow) |

**Recommendation: A for the pilot.**
- The token is a per-device brain token (24 h, revocable), minted by `/launcher/launch` like today's
  desktop token. The MCP endpoint checks it on every call, with a 30-second cache like the ingress, so
  sign-out or non-payment reaches a running Hermes within 30 seconds.
- Expiry mid-session shows as "relaunch from int3". The launcher re-mints at each launch.
- The same token authenticates the memory plugin, so there's one secret on disk; sign-out deletes it from
  `.env`.
- B is the day-2 option if long unattended sessions (messaging bots running for days) need silent renewal.
- Rejected: application declarations, a runtime JSON file that `_live_endpoint` overlays
  (`tools/mcp_tool_transport.py:96-124`). They need a plugin to call `register()`; "core does not read
  plugin YAML automatically" (`website/docs/developer-guide/plugins/application-declarations.md:19`). They
  also assume a desktop app with a PID (`:84-87`).

### 3.3 The memory-provider plugin

- **Interface.**
  - `agent/memory_provider.py:84-267`. Required: `name`, `is_available()` (no network), `initialize()` and
    `get_tool_schemas()` (`:109-133,165-167`).
  - We'd also implement:
    - `prefetch()`/`queue_prefetch()`, for fast cached recall (`:144-150`);
    - `sync_turn()`, to persist turns without blocking (`:157-163`);
    - `handle_tool_call()` (`:169-171`);
    - `system_prompt_block()` (`:140-142`);
    - `on_session_end()` (`:197`);
    - `shutdown()` (`:173`);
    - `unavailable_reason()` (`:136-138`).
  - Threading must use `ctx_bound`/`spawn_context_thread` (`:21-33`), and be multi-profile safe (`:84-102`).
- **Composition.** The manager is "builtin provider (always first) plus at most one external provider"
  (`agent/memory_manager.py:200-203`). With both built-in store flags false, no `MEMORY.md`/`USER.md` is
  loaded (`agent/agent_init.py:1185-1203`). The external provider is still resolved
  (`:1205-1225`). That makes it a replacement, not a parallel store.
- **Install location.** `%LOCALAPPDATA%\hermes\plugins\int3\` with `__init__.py`
  (`register(ctx)` → `ctx.register_memory_provider(...)`) and `plugin.yaml`
  (pattern: `plugins/memory/retaindb/plugin.yaml`, `retaindb/__init__.py:491-493`;
  layouts: `website/docs/developer-guide/memory-provider-plugin.md:15-63`).
  - Stdlib only (urllib, json), so it has no dependency surface and never joins Hermes's managed
    dependency set (`:617-624`).
  - Hermes updates don't delete or rewrite `$HERMES_HOME/plugins/**` (`:622`).
- **Activation.** `memory.provider: int3`. Discovery never imports a provider until it's named
  (`memory-provider-plugin.md:35`). Only `plugins.disabled` can park it (`plugins/memory/__init__.py:558-572`).
- **Offline or revoked.**
  - `is_available()` is true when a token exists; no network check (`agent/memory_provider.py:116-118`).
  - Recall failures return "" and the turn proceeds.
  - Writes are queued in memory only, dropped after a bound, and never written to disk.
  - On 401 the plugin stops calling and shows "memory is off: signed out of int3".
  - Hermes warns once when a provider is unavailable (`agent/agent_init_memory_provider.py:40-55`).
  - No memory content is cached on disk by the plugin.
- **API stability.** `agent.memory_provider` and `agent.memory_manager` are on the public plugin API list
  (`website/docs/developer-guide/plugins/public-api.md:33-34`). Six commits touched it between v0.22.6 and
  v0.23.1; all added optional hooks or kwargs (`git log v0.22.6..v0.23.1 -- agent/memory_provider.py`). We
  read new kwargs with `kwargs.get`, as the contract asks (`agent/memory_provider.py:126-130`).
- **Install and update.** The launcher ships the plugin inside its installer and copies it on each launch
  if the version differs. It writes `memory.provider: int3`, then checks `hermes memory status` or the
  startup warning.

### 3.4 Model setup

| Choice | Hermes provider | Where the secret lives |
|---|---|---|
| Claude API key | `anthropic` | `.env` `ANTHROPIC_API_KEY` (`configuration.md:63-67`) |
| OpenAI API key | `openai` | `.env` `OPENAI_API_KEY` |
| ChatGPT sign-in | `openai-codex`, "ChatGPT or Codex Subscription" (`hermes_cli/providers.py:32,145`); device code by default (`website/docs/integrations/providers.md:18,91-94`) | `auth.json` (`hermes_cli/auth.py:4,477-481`) |
| Ollama / OpenAI-compatible local | custom endpoint (`base_url`) | No secret |

**Recommendation.**
- **Keys and OAuth never pass through int3.** The launcher opens Hermes's own Providers settings and the
  user enters the key or signs in there. Desktop handles `hermes://open/<path>` deep links
  (`apps/desktop/src/lib/hermes-open-target.ts:1-29`, registration at `apps/desktop/electron/main.ts:17345-17375`).
  The settings page has a `providers` section (`apps/desktop/src/app/settings/index.tsx:364`). The exact
  deep-link path is **unverified** (open question 3); the fallback is a one-line instruction.
- **For Ollama the launcher writes the non-secret model config itself**, because there's no key. It also
  pins the auxiliary model to the same endpoint. Auxiliary tasks (compression, vision) default to the main
  model, but the "auto" chain can fall back to OpenRouter when a key exists
  (`website/docs/integrations/providers.md:111-113`, `hermes_cli/config_defaults.py:724-736`).
  So: `auxiliary.*.provider` = the local endpoint, `fallback_providers: []`, and posture refuses the
  passages-only tool if any cloud provider key is present.

### 3.5 Messaging

- **Process.** The messaging gateway is a separate process, `hermes gateway run|start`. Desktop's local
  backend doesn't run it, though Desktop can restart it (`hermes_cli/web_routers/actions.py:160`,
  `apps/desktop/src/api/system.ts:151`).
- **Runs with Desktop closed.** Yes, if installed as a Scheduled Task at login (`hermes gateway install`),
  which happens only on an explicit answer (`website/docs/user-guide/windows-native.md:212-243`).
  Revocation still applies: its brain and memory calls use the same token.
- **Allowlist.** `<PLATFORM>_ALLOWED_USERS` env vars (for example `allowed_users_env: "TELEGRAM_ALLOWED_USERS"`,
  `plugins/platforms/telegram/gateway_env.py:24`). Without an allowlist the default is deny unless the
  policy is `allowlist` (`gateway/authz_mixin.py:543-549`). The launcher sets the owner's own id, collected
  in the setup step, and `dm_policy: allowlist`. Agent replies have no agent-callable `send_message` tool
  (`toolsets.py:183-184`).
- **Pull/push, from adapter source.** Pull means the PC only dials out; push means the platform must reach
  a listener.

| Platform | Inbound | Evidence |
|---|---|---|
| Slack | **pull** (Socket Mode) | `plugins/platforms/slack/adapter.py:2,21` |
| Telegram | **pull** (long polling); push only if `TELEGRAM_WEBHOOK_URL` is set | `plugins/platforms/telegram/adapter.py:2970`, webhook `:2910-2928` |
| Discord | **pull** (gateway websocket via discord.py) | `plugins/platforms/discord/adapter.py:1446` |
| Email | **pull** (IMAP), SMTP out | `plugins/platforms/email/adapter.py:7,121` |
| Signal | **pull**, but needs a local `signal-cli` daemon (Java) | `plugins/platforms/signal/adapter.py:1-3` |
| Matrix | pull (mautrix client) | `plugins/platforms/matrix/adapter.py:2` |
| Mattermost | pull (websocket) | `plugins/platforms/mattermost/adapter.py:488` |
| ntfy | pull (HTTP stream) | `plugins/platforms/ntfy/adapter.py:1` |
| DingTalk | pull (stream websocket) | `plugins/platforms/dingtalk/adapter.py:72` |
| QQ Bot | pull (websocket) | `plugins/platforms/qqbot/adapter.py:314` |
| WeCom (app) | pull (websocket) | `plugins/platforms/wecom/adapter.py:236` |
| Weixin | pull (long poll) | `plugins/platforms/weixin/adapter.py:1,56` |
| WhatsApp (Web/Baileys) | pull, via a local Node bridge | `plugins/platforms/whatsapp/adapter.py:1-2` |
| Feishu/Lark | pull by default (`websocket`); webhook optional | `plugins/platforms/feishu/adapter.py:1399,1504` |
| Google Chat | pull (Pub/Sub streaming pull) | `plugins/platforms/google_chat/adapter.py:144,704-716` |
| IRC | pull (outbound socket) | `plugins/platforms/irc/adapter.py:14,116` |
| SimpleX | pull, via a local daemon websocket | `plugins/platforms/simplex/adapter.py:1-4` |
| Photon (iMessage) | pull, via a local sidecar gRPC stream | `plugins/platforms/photon/adapter.py:1-6` |
| Buzz (Nostr) | pull (websocket subscription) | `plugins/platforms/buzz/adapter.py:2-5` |
| Teams | **push** (Bot Framework HTTP) | `plugins/platforms/teams/adapter.py:409-410` |
| WhatsApp Business Cloud | **push** (webhook) | `plugins/platforms/whatsapp_cloud/adapter.py:285-286` |
| LINE | **push** (webhook) | `plugins/platforms/line/adapter.py:449-450` |
| SMS (Twilio) | **push** (webhook) | `plugins/platforms/sms/adapter.py:136-137` |
| WeCom callback | push | `plugins/platforms/wecom/callback_adapter.py:147` |
| BlueBubbles | push (from a local BlueBubbles server) | `plugins/platforms/bluebubbles/adapter.py:222` |
| Raft | push (wake endpoint) | `plugins/platforms/raft/adapter.py:334-336` |
| A2A | push (HTTP server) | `plugins/platforms/a2a/adapter.py:23,124` |
| Webhook, Graph webhook, API server (core) | push | `gateway/platforms/webhook.py:284`, `msgraph_webhook.py:143`, `api_server.py:132` |

Your five are confirmed pull. Signal needs a local Java `signal-cli` daemon, which is a heavier install
than the others.

**Day 2 relay (design note).**
- Hermes Relay has the right shape: the gateway dials out and inbound rides that socket
  (`website/docs/user-guide/messaging/relay.md:14-35`). But it's experimental and "MAY CHANGE without a
  deprecation cycle" (`gateway/relay/__init__.py:3-8`), and its connector is a separate Nous service
  (`website/docs/developer-guide/relay-connector-contract.md:9-20`) that isn't public
  (`gh repo view NousResearch/gateway-gateway` → not found).
- **Recommendation: our own platform plugin** (platforms are a plugin type, `plugins/platforms/*`). It
  long-polls our server, which receives Teams/WhatsApp Cloud/LINE/SMS webhooks and queues them per user.
  It's a supported extension point, our server holds the platform credentials, and there's no dependency
  on an unstable contract.
- Re-evaluate Hermes Relay once it's out of experimental status.

### 3.6 Leak analysis (with our defaults)

| Path out of the PC | What closes or limits it | Residual |
|---|---|---|
| Model provider (cloud) | The user's own choice and key. Brain answers are server-written for cloud users anyway | Chat content goes to their provider. Accepted |
| Model provider (Ollama) | Passages-only tool; auxiliary pinned local; `fallback_providers: []`; posture refuses if a cloud key exists | A user can add a cloud key later; posture catches it at the next turn |
| Auxiliary model calls | Pinned to the main/local provider (§3.4) | — |
| Messaging sends | Owner-only allowlist; replies go to the owner's own chat | Brain answers reach that platform's servers (Slack, Telegram…). Stated in the guide |
| Other MCP servers | Posture includes `mcp_servers` names; unknown servers → brain refuses | Detect only: a user can add one in settings |
| Skills / plugin installs | Install deep links need confirmation (`website/docs/reference/mcp-config-reference.md:441-455`); `skills.inline_shell: false`; posture includes the plugin list | A plugin runs in-process: full escape. Detect only |
| Cron / automations | `cronjob` and `delegation` disabled; `approvals.cron_mode: deny` | Scheduled jobs created in the UI still run with the same tool limits |
| Terminal, code, browser, web | `agent.disabled_toolsets` (subtracted last) | Detect only if the user edits config |
| File writes into OneDrive / synced folders | Writes confined to `%USERPROFILE%\int3-workspace`; the launcher refuses a synced or linked folder | Reads are not confined; the user can copy and paste. Accepted |
| Clipboard / copy-paste / screenshots | None; the user's own action | Accepted, as with any app |
| Desktop browser/terminal panes | `desktop_ui` disabled, so the agent can't drive them | The user can use them directly |
| Local chat history | Stays on the PC. Accepted for the pilot | Hermes's transcript cache too |
| TTS / image / video generation | Toolsets disabled (they call third-party APIs) | — |
| Hermes update checks | Version check only; no content | — |
| Hosted memory | Our server, per user, deletable (§3.8) | — |

### 3.7 Version drift

- Store Hermes updates itself (already v0.23.1 against our v0.22.6 pin).
- At each launch the launcher reads:
  - the Hermes version (`hermes --version` through the MSIX execution alias,
    `website/docs/user-guide/windows-native.md:62`);
  - the config schema version (`_config_version`, currently 53, `hermes_cli/config_defaults.py:2700`).
- It checks both against our compat table, the M0 `hermes_compat` extended with per-release checks:
  1. **config keys**: every key in §3.1 still exists in that version's defaults (checked when we add a
     release);
  2. **toolset names**: still valid;
  3. **MCP entry**: `headers` and `tools.include` semantics unchanged;
  4. **plugin**: loads and reports available.
- **On mismatch:**
  - unknown version → warn and allow, but posture must pass;
  - a failed read-back or plugin load → the launcher doesn't mint a brain token, says why, and Hermes still
    runs plain;
  - a known-bad version → blocked.
- **Weekly CI-free check:** build the latest Hermes tag from source, run the plugin and config contract
  tests against it (as M1 did for `normalizeRegistry`), and record the result.

### 3.8 Server changes

| Piece | Reused | New |
|---|---|---|
| Auth | Launcher device sessions, tokens, revocation, launcher edge (`int3_ai/backend/app/launcher/*`) | A `brain` token kind; introspection with a 30-second cache |
| Brain MCP | `brain-mcp/server.py` tools (`int3_ai/deploy/hermes/brain-mcp/server.py:75-138`) | One multi-user HTTPS endpoint: the token resolves the user per request (replaces one sidecar per user) |
| Answers | `/v1/brain_ask` (`int3_ai/backend/app/api/v1.py:394`) | — |
| Passages-only | `/v1/search` plus `retrievals` (`:167`, `:132-152`) and `/v1/validate` (`:467-517`), bound to session and tenant, with expiry | MCP tools `search_brain` and `validate_citations`; passage ids in output |
| Rate limits | Gateway sliding window (`int3_ai/gateway/limits.py:1-11,82`); capture limit (`int3_ai/backend/app/capture.py:92`) | Per-user brain-query limits (default 30/min, 500/day), Postgres-backed |
| Audit | `retrievals` | `brain_audit`: user, device, tool, question, answer or passage ids, refused, posture hash, time. Per tenant, retention 90 days |
| Memory store | Tenant DB + pgvector (`int3_ai/backend/app/schema.sql:1,56`) | `user_memories` (below) plus the memory API |

**Memory schema.**
- Lives in the tenant DB: `user_memories(id, user_id, kind ['fact','preference','turn_summary'], content,
  embedding vector(384), source_session, created_at, deleted_at)`.
- Every query filters by the token's `user_id`; the API never takes a user id from the client.
- Deletion is a hard delete, either by the user via a memory tool or by an admin for offboarding. Removing a
  user deletes all their rows.

**Citations without server-side generation (Ollama).**
1. `search_brain` returns passages tagged `[P1]..[Pn]` plus a `retrieval_id`.
2. The local model must cite tags. Our plugin's `sync_turn` sees the final answer
   (`agent/memory_provider.py:157-163`) and calls `/v1/validate` with the cited ids.
3. Invalid ids are logged and flagged on the next turn.
- This is **advisory**: a plugin can't edit an answer already shown. It's structural on the server (only
  retrieved ids validate) but not enforced at display. **Recommendation: both modes.** Cloud users keep
  server-written answers (structural, threshold-gated). Ollama users get passages with validated, advisory
  citations.

**Server size and cost.**
- Local mode has no per-user containers: brain (~500 MiB measured), DB, one MCP endpoint and the memory API.
- That fits `e2-standard-2` (2 vCPU, 8 GB) at about **$60/month** (listing price $0.0827/h × 730, unverified
  in Google's calculator), for hundreds of users. The limit is reranker CPU per query, not memory, and isn't
  measured yet.
- Hosted mode today: `e2-highmem-2` at about $92/month for 9–12 users (`deploy/gcp/RUNBOOK.md`).

---

## 4. Milestones and effort

Effort is engineering days for one person, from experience with this codebase. It doesn't include waiting
on partner sessions.

| # | Milestone | Pilot or day 2 | Effort |
|---|---|---|---|
| L1 | Multi-user brain MCP endpoint, brain token kind, introspection, rate limits, audit table | Pilot | 5–7 days |
| L2 | Memory API, schema and deletion; int3 memory plugin (stdlib) and its tests | Pilot | 6–8 days |
| L3 | Launcher: config writer via `hermes config set`, read-back, plugin install, posture, compat per version, Providers deep link | Pilot | 5–7 days |
| L4 | Passages-only tools, citation validation via the plugin, Ollama config pinning | Pilot | 3–4 days |
| L5 | Messaging setup for Slack, Telegram, Discord and Email (owner allowlist); Signal documented, not wizard-driven | Pilot | 3–4 days |
| L6 | Test pass (Win10 source build) and first partner session (Win11 Store) | Pilot | 3–4 days |
| | **Pilot total** | | **about 5–7 weeks** |
| D1 | Relay: our platform plugin plus the server webhook queue (Teams, WhatsApp Cloud, LINE, SMS) | Day 2 | 10–15 days |
| D2 | Optional Docker sandbox to bring back terminal and code | Day 2 | 5–10 days |
| D3 | MCP OAuth (option B) for long-running unattended gateways | Day 2 | 5–8 days |

---

## 5. Risks and open questions for you

**Risks.**
1. **The PC isn't ours.** The tool lockdown is detection plus refusal, not prevention (§3.1). If a customer
   needs prevention, they need hosted mode.
2. **Store auto-updates.** A Hermes release can rename a key or toolset. The compat check and weekly
   source-build check catch it, but a partner may hit it first.
3. **Hosted memory is a new data store** of personal content. It needs a retention and deletion policy
   before real customers.
4. **Messaging replies leave via the platform** (Slack, Telegram…) even when the model is local.
5. **Signal** needs a local Java daemon, which is a support burden.

**Open questions.**
1. Is "detect and refuse" acceptable for the pilot, given the plain-language caveat?
2. Disabling `file` as well: the agent can't save documents. OK for the pilot?
3. May the launcher open Hermes's Providers page by deep link? The exact path is unverified and needs a test.
4. For cloud-model users, should brain answers stay server-written (our model cost), or switch to
   passages-only (their model writes)?
5. Memory retention: how long, and should users see and delete their memories in int3?
6. Messaging: which of the five go in the first wizard? I'd suggest Slack and Telegram first.

---

## 6. Test plan

We have no Windows 11 PC; the dev PC is Windows 10, which can't install the MSIX.

1. **Windows 10, Hermes v0.23.1 built from source** (as done for v0.22.7). This covers:
   - config writes and read-back;
   - layering: toggle tools in the UI and confirm they stay off;
   - plugin load, offline behaviour and revocation mid-session;
   - posture refusal after a manual config edit;
   - the passages-only path with Ollama, with Windows `pktmon` capture proving there are no calls to cloud
     model hosts;
   - Telegram (polling) with a synthetic bot.
2. **Contract tests against each Hermes tag** (no Windows needed): load config with Hermes's own loader;
   import and exercise the plugin against `agent.memory_provider`; resolve toolsets with Hermes's resolver.
3. **Server tests:** token kinds, introspection, rate limits, the audit row per call, memory isolation
   (user A never sees B), deletion, `/validate` across sessions.
4. **First partner session (Windows 11, Store Hermes):** the only test of Store install, real HERMES_HOME,
   the MSIX execution alias and the deep link. It reuses the guided-session format and adds:
   - a memory check across two sessions;
   - a sign-out check that the brain stops within 30 seconds;
   - a check that tools stay disabled after the user toggles them.
5. **Not covered until then:** MSIX-specific behaviour, Windows 11 only quirks, and Store update timing.
