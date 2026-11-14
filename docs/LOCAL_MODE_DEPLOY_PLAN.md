# Local mode on int3-pilot: deployment plan (NOT applied)

Status: design for owner approval, 2026-11-14. Nothing here has been run on the VM. Read-only
facts were gathered on 2026-11-14 with `ops.sh ssh` (commands quoted below).

## In plain language

We add local mode to the existing pilot server rather than renting a second one. Local-mode
companies get two new web addresses: one for the int3 app, one for the brain connection. Hosted
companies (marcuspilot, zack) keep the addresses they have today. Nothing about how they sign in or
chat changes.

The one shared piece is the brain service. It has to be updated to the local-mode code, which needs a
short restart (about a minute). We do that in a quiet window, take a backup and a disk snapshot first,
and check a hosted chat straight afterwards. If anything is wrong, we put the old version back in
about five minutes. The database changes only add things, so the old version keeps working with them.

## 1. Today (measured, read-only)

`ops.sh ssh --command "git -C /opt/int3 rev-parse --short HEAD; nproc; free -m; docker stats --no-stream"`:

| Fact | Value |
|---|---|
| Deployed commit | `2bc47f8` (local mode is `3de67d1`, M2 is `cafbf46` and later) |
| Machine | e2-highmem-2: 2 vCPU, 16,002 MiB |
| Memory used / available | 3,812 MiB used, 12,189 MiB available |
| Load average | 0.04 / 0.14 / 0.16 |
| Disk | 20 of 49 GB used |
| Hosted users | 6 (zack: priya, marcus, ada; marcuspilot: the same three names) |
| Per hosted user | Hermes 212–378 MiB + forwarder ~20 MiB + MCP sidecar ~41–45 MiB |
| Shared | brain 510 MiB, db 93, gateway 55, caddy 53, key-intake 42, launcher-edge 19, edge 11 |

**Risk found while reading:** `deploy/gcp/config.env` has `INT3_REF=spike/remote-thin-client`.
`vm/update.sh` checks out the **latest** commit of that branch. Anyone running update.sh today would
deploy local mode (M1 + M2) to the pilot by accident. Step 0 below fixes that.

## 2. What gets added

| Piece | How | New public host | Port (loopback) |
|---|---|---|---|
| Local launcher edge: a second instance of the launcher-edge proxy with its own allowlist (sign-in, me, compat, devices, `/launcher/local/*`, memory). It has no `/launcher/launch` and no BYOK. | compose service `launcher-edge-local` | `route_id local 8101 launcher-edge-local` | 8101 |
| Brain MCP endpoint (multi-user, no own credential) | existing `brain-mcp-local` service, added to the hosted overlay | `route_id mcp 8200 brain-mcp-local` | 8200 |
| Memory API | already part of the brain; reached through the local edge | (the `local` host) | — |
| Memory retention | `int3-purge-memories.timer` (in this commit) | — | — |

Both hosts are new random 16-hex IDs from `route_id`, exactly as the launcher and key-intake hosts were
made, and get their own TLS certificates through the existing Caddy (TLS-ALPN, 443 only). Brain
environment: `LOCAL_MCP_URL=https://<mcp>.app.int3labs.app/mcp` and
`LOCAL_MEMORY_URL=https://<local>.app.int3labs.app`, both required (`:?`) in the hosted overlay.

**Separation from hosted:**
- The existing hosted launcher edge's allowlist **drops** `/launcher/local/*` and the memory routes, so
  hosted users' address never serves local mode.
- The local edge does not serve `/launcher/launch` or BYOK.
- Every local route also returns 404 to a hosted tenant (`_require_local`).
- A brain token works only for a `launch_mode='local'`, non-cancelled tenant (`tokens.resolve_brain`).

## 3. Headroom

- **Memory.** Local-mode users have no per-user container on the VM: their Hermes runs on their PC.
  - New fixed cost: local edge ~20 MiB + brain MCP endpoint ~50–100 MiB (estimate, sized like the MCP
    sidecars; not measured on the VM).
  - Brain growth: small. The memory reranker is the model the brain already loads.
  - After the change: about 12 GB still available.
- **CPU** is the limit for local mode, because every local-mode question and memory recall runs in the
  shared brain.
  - Measured on the dev PC: one memory recall (embed + rerank 10) costs 73 ms. Assume about 150–250 ms on
    the VM's 2 vCPU (not measured).
  - The per-user limits cap a busy user at 30 brain + 60 memory calls a minute.
  - A pilot of 2–3 companies with up to about 30 active users fits.
  - Upgrade trigger: brain p95 latency over 2 s, or sustained load average over 1.5. Next size up is
    e2-highmem-4 (a stop/start, about 3 min).
- **Disk:** brain image rebuild ~1 GB; memories are KB per user.

## 4. Steps (owner runs, or approves me running each)

0. **Pin the VM** (code change, reviewed separately):
   - `vm/update.sh` takes an explicit commit (`update.sh <sha>`) and refuses to run without one;
   - `config.env` keeps the branch only as the fetch source.
1. **Build before touching the VM:**
   - admin commands `set-launch-mode --tenant T --mode local|hosted` and
     `set-credential-sources --tenant T a,b,c` (today only tests can set them);
   - the split allowlists (`launcher-edge.allowlist` hosted-only, `launcher-edge-local.allowlist`);
   - `launcher-edge-local` + `brain-mcp-local` in `compose.hosted.yml`;
   - `route_id local|mcp` in `stack-up.sh`.

   Full server suite plus a local-stack E2E. Push, scan, and record the SHA.
2. **Quiet window** (Singapore evening). Announce to marcuspilot only if they are active: hosted
   sessions drop for about one minute.
3. **Safety copies:**
   - `sudo systemctl start int3-backup` and confirm the new `gs://…/<stamp>/`;
   - a disk snapshot: `gcloud compute disks snapshot int3-pilot --snapshot-names pre-local-<date>`.
4. **Update to the pinned SHA:** `update.sh <sha>`. That runs control `init` (CONTROL_VERSION 3) and
   restarts the shared stack. Per-user Hermes containers keep running.
5. **Migrate tenants:** `app.tenants.admin migrate` (zack and marcuspilot v3 → v5). Additive:
   `user_memories` and `memory_audit` are new tables, `tenants.cancelled_at` and `allowed_credential_sources`
   are new columns with defaults.
6. **TLS for the two new hosts:** add the routes and re-render Caddy with the existing `enable-tls.sh`
   flow (production issuer).
7. **Hosted checks**, from outside (`smoke-external.py chat`), on zack and marcuspilot as one user each:
   - a sourced answer;
   - the launcher host unchanged;
   - `/launcher/local/policy` is 404 on the hosted host.
8. **Local checks**, on a new synthetic local-mode tenant (section 6), with a real launcher + Desktop:
   - question → `search_brain` → `[claim:N]` → `validate_citations`;
   - a propose card;
   - `int3_remember` → Memory tab;
   - the next-turn recall.

## 5. Rollback (about 5 minutes)

- **Trigger:** any hosted check in step 7 fails, or brain errors appear in `docker logs int3-brain-1`.
- **Code:** run `update.sh 2bc47f8`. It rebuilds the brain at the old code and restarts the shared stack.
  The old code ignores the added tables and columns: migrations only add, and old code does not check
  the schema version (`backend/app/migrations.py`).
- **Hosts:** remove the two new routes and re-render Caddy. Local-mode launchers then fail closed:
  no brain token, so no brain.
- **Data:** none to undo. The new tables can be dropped later.
- **Worst case** (a database problem): restore the step 3 dump per RUNBOOK "Backups › Real
  restore", or recreate the VM disk from the snapshot. Either loses at most the minutes since step 3.

## 6. Provisioning a partner tenant in local mode

1. Provision as today, without per-user backends:
   - `app.tenants.admin provision --id <t> --name … --seed …` (synthetic data), or an empty tenant
     plus users;
   - no `provision-user.sh`: local-mode users have no VM container.
2. `app.launcher.admin set-launch-mode --tenant <t> --mode local` (new, step 1).
3. Optionally `set-credential-sources --tenant <t> api_key,local_endpoint,hermes_oauth`, which is the
   default. Add `personal_login` only if the company allows personal Claude Code / ChatGPT logins.
4. Invites:
   - `app.launcher.admin invite --tenant <t> --user <u> --server-url https://<local>.app.int3labs.app`,
     minted in an interactive SSH shell and sent out of band (as for marcuspilot);
   - the launcher takes its server from the link, so local-mode users never see the hosted host.
5. Give the partner `docs/PARTNER_GUIDE_LOCAL.md`.
6. Offboarding:
   - one user: `revoke-user`;
   - the whole company: `cancel-tenant`, which stops access at once; memories are purged 30 days later
     by the timer and are gone from backups 30 days after that.

## 7. Not covered

- Hermes Desktop from the Microsoft Store (MSIX) has still not been run end to end. The first partner
  session is that test (Windows 11 22H2+ needed; the dev PC is Windows 10).
- The VM's CPU cost per recall is an estimate (section 3).
- The backup alert still depends on `roles/monitoring.metricWriter` (unchanged).
