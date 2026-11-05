# int3 hosted pilot — runbook

One VM in `asia-southeast1` running the same stack as a local launcher-mode stack, with Caddy as the
only public listener. Everything is in this folder. Settings: `config.env`. The base domain
(`INT3_BASE_DOMAIN`) is one setting; every public hostname is `<random-id>.<base>`, so certificate
transparency logs reveal no partner or person.

```
Hermes Desktop / int3 launcher
        │ https only (443)
      Caddy ─ per-host certificate, Host rewritten to loopback, X-Forwarded-* removed
        │ 127.0.0.1 only
  launcher edge · key intake · one ingress auth proxy per user
        │
  brain · gateway · db · one gated Hermes backend + MCP sidecar per user (internal networks)
```

**https only.** `.app` is HSTS preloaded. Port 80 is closed and Caddy has no http listener or redirect;
certificates use TLS-ALPN-01 on 443. `provision-user.sh` refuses a non-https public URL, `admin invite`
refuses a non-https server, and the launcher refuses any non-https, non-loopback backend or link and
follows no redirects.

## Network and access

| | |
|---|---|
| VPC | `int3-net` (dedicated: the project's `default` network has SSH and RDP open to the internet) |
| Inbound | tcp/443 from anywhere; tcp/22 from `35.235.240.0/20` (IAP) only |
| SSH | `bash deploy/gcp/ops.sh ssh` (IAP + OS Login; the VM has no project SSH keys) |

## Sizing and cost

Measured on the local stack (`memory.peak`, one chat turn): a user's backend + sidecar + ingress peaked
at 420 MiB; shared services at about 0.85 GiB. 3 partners × 3 users ≈ 4.6 GiB, about 5.6 GiB with the OS,
budgeted ×1.5 for tool-heavy turns (not measured) ≈ 8.5 GiB, which rules out 8 GB machines.

`e2-highmem-2` (2 vCPU, 16 GB) ≈ $81/month, 50 GB balanced disk $5.50, static IP about $3.65, the rest
under $2: **about $92/month** (third-party price listing; confirm in Google's calculator). CPU under
concurrent use is not measured. To upgrade: stop, `gcloud compute instances set-machine-type`, start.
A stopped VM still bills disk and IP, about $9/month.

## First-time setup, in order

**1. Infrastructure** (your PC). Idempotent; requests no certificates; prints the static IP.
```bash
bash deploy/gcp/infra.sh
```
The VM then installs Docker, clones this repo, generates its non-model secrets into Secret Manager
(`int3-stack-env`), builds the pinned Hermes image, and starts the stack. Follow it with
`bash deploy/gcp/ops.sh ssh -- sudo journalctl -u int3-stack -f`.

**2. Anthropic key.** You store it; nobody else handles it. In Cloud Shell, so it stays out of local history:
```bash
read -rsp "Anthropic key: " K; echo
printf '%s' "$K" | gcloud secrets create int3-anthropic-api-key --project=int3-513810 \
  --replication-policy=user-managed --locations=asia-southeast1 --data-file=-
unset K
```
Then pick it up: `sudo systemctl restart int3-stack`. To rotate: `gcloud secrets versions add
int3-anthropic-api-key --data-file=-`, restart, and re-provision (each tenant holds an encrypted copy).

**3. DNS** (you, at Porkbun, for `int3labs.app`): `A  *.app  →  <static IP>`. Check:
`nslookup anything.app.int3labs.app`. Confirm to the operator. **No certificate is requested before this.**

**4. TLS** (on the VM). Staging first, so a mistake cannot hit production rate limits. The script also
checks public DNS itself and refuses if the wildcard does not point at this VM.
```bash
sudo bash /opt/int3/deploy/gcp/vm/enable-tls.sh --owner-confirmed-dns --staging
sudo bash /opt/int3/deploy/gcp/vm/enable-tls.sh --owner-confirmed-dns --production
```

**5. Uptime check** (your PC), with the launcher hostname from `sudo cat /var/lib/int3/routes.tsv`:
```bash
bash deploy/gcp/ops.sh uptime <launcher-id>.app.int3labs.app
```
Alerts go to the gcloud account's address unless `INT3_ALERT_EMAIL` is set in your environment.

## A new partner

```bash
bash deploy/gcp/ops.sh ssh
sudo bash /opt/int3/deploy/gcp/provision-partner.sh <name> [--cap-usd 5] [--ttl-hours 72]
```
Creates a synthetic tenant (the fictional Brindlewood seed), users priya, marcus and ada, each with a
gated backend and a random hostname, and prints three single-use invite links **to your terminal only**:
it refuses if its output is piped or redirected. Each backend takes about 420 MiB. Run it again for fresh
invites. The cap is per tenant, per day, and we pay it.

## Installer

The repo is public, so a GitHub Release is the natural home, but a release needs a git tag. Otherwise use
a public object in a separate bucket (never the backup bucket). Build: `launcher/README.md`.

## Day to day

| | |
|---|---|
| State | `bash deploy/gcp/ops.sh status` |
| Stop / start | `bash deploy/gcp/ops.sh stop` / `start`. All containers return on start |
| Stack log | `sudo journalctl -u int3-stack` |
| Caddy log | `sudo docker logs int3-caddy` (no access log, by design) |
| Update | `sudo bash /opt/int3/deploy/gcp/vm/update.sh` moves to the latest commit of `INT3_REF` and restarts the shared stack. The VM never updates itself. A change to the gated image reaches a user only when that user is re-provisioned. |
| Revoke a person | `sudo docker exec … python -m app.launcher.admin revoke-user --tenant T --user U`; their tokens stop within about 30 s |
| Record a Hermes build | `app.launcher.admin record-build --platform windows --package-version V --release v0.22.6` |

## Backups

`int3-backup.timer`, daily at 02:00 Singapore time, to `gs://int3-513810-int3-backups/<timestamp>/`:
`pg_dumpall`, a counts manifest, and a tar per user's Hermes volume. Objects are deleted after 30 days.
The bucket is private and the VM can write and read but not delete.

`int3-restore-test.timer`, weekly: the newest dump goes into a throwaway Postgres with no network, and
its counts must equal the manifest. Run by hand: `sudo systemctl start int3-restore-test; sudo journalctl
-u int3-restore-test`. A failed timer is visible only in the journal: nothing alerts on it yet.

**Real restore:** stop the stack, remove the `int3_pgdata` volume, `compose up -d db`, then
`gunzip -c postgres.sql.gz | docker exec -i int3-db-1 psql -U brain -d postgres`, then start the stack.
Secrets come from Secret Manager, and **without `int3-stack-env` the encrypted provider keys cannot be
decrypted**: never delete that secret. Hermes volumes: untar into `hermes-home-int3-<tenant>-<user>`.

## What lives where

| | |
|---|---|
| Anthropic key, DB passwords, keyvault key | Secret Manager; on the VM in `/run/int3/stack.env` (tmpfs) and, because Docker keeps container env, in `/var/lib/docker` (root only, encrypted disk) |
| Hostname table | `/var/lib/int3/routes.tsv` (not secret; labels never reach the Caddyfile or DNS). **Not backed up beyond the VM disk**: losing it needs re-provisioning |
| Certificates | Docker volume `int3-caddy_caddy_data` |
| Invites | Printed once; the database keeps only a hash |
