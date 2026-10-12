# Handoff — thin-client Hermes pilot (`deploy/hermes/`)

Synthetic-data pilot that runs stock Hermes Desktop against a per-user,
locked-down Company Brain backend. No cloud; everything is local Docker.

## What's here

| File | Role |
|------|------|
| `docker-compose.yml` | shared base: control DB, init+seed (brindlewood), brain API, BYOK gateway (service mode), default-deny egress proxy |
| `provision-user.sh` | `<tenant> <user> [port]` → that user's MCP sidecar (brain token) + gated Hermes backend (gateway token) + connect file |
| `brain-mcp/` | the per-user MCP sidecar image (`ask_brain`, identity carried by the brain token) |
| `gated/` | `Dockerfile.gated` (stock Hermes + proxy + gate), `startup-gate.sh`, `route_proxy.py`, `egress_proxy.py`, `allowlist.txt` |
| `.env.example` | every secret, with a generate command; copy to `.env` |
| `connect/` | per-user connect files for stock Hermes Desktop (git-ignored) |

## Run it

```
cd deploy/hermes
cp .env.example .env && $EDITOR .env
./build-base.sh                       # stock Hermes @ pinned commit -> hermes-base-pinned
docker compose up -d --build
docker build -t hermes-deploy-brain-mcp brain-mcp
docker build -t hermes-gated --build-arg BASE=hermes-base-pinned -f gated/Dockerfile.gated gated
./provision-user.sh brindlewood marcus 9131 --key-owner theirs
./verify-isolation.sh brindlewood marcus priya   # asserts cross-user isolation
```

## Security properties (all enforced by mechanism, not comments)

- **Per-user isolation (network + auth).** Each user's Hermes backend + sidecar
  live on a private internal network; no Hermes backend joins the brain network,
  so it cannot reach the brain directly, another user's sidecar, or another
  user's backend. The sidecar also requires a per-user X-MCP-Secret (401
  otherwise). `verify-isolation.sh` asserts both layers.
- **Per-user brain token, never in the Hermes container.** provision-user.sh
  mints two *distinct* tokens — the brain token goes only to the MCP sidecar, the
  gateway token only to the Hermes config — and asserts the brain token is absent
  from the Hermes container before finishing.
- **Scoping holds across users.** Verified: marcus (finance) gets a cited answer
  to "When are invoices payable?"; priya (operations) gets a refusal to the same
  question with zero leaked finance titles/canaries.
- **Startup gate.** Each backend refuses to serve unless the read boundary, the
  route allowlist, and the egress lock (provider unreachable, gateway reachable)
  are all live.
- **BYOK gateway.** Provider key only in the gateway; per-tenant allowlist
  (sonnet only) + daily cap enforced; usage attributed per user.

## Deferred (scope cuts for the MVP)

- LLM-judge refusal detector (the eval gate is a keyword PROXY — see `eval/README.md`).
- Negative-control fixtures; Haiku/OpenAI evaluation (sonnet-only allowlist).
- Distributed rate limiting (the limiter is per gateway process).
- OAuth / web admin.

## Known limitations

- The connect file works only on the Docker host (loopback, no TLS). Partner
  laptops need hosted TLS ingress — Phase 3.
- Token minting uses `openssl` + a direct `INSERT` (psql `-v` bindings, validated
  ids); a real deployment mints via a secrets manager.
- Single gateway process; multi-replica rate limiting not implemented.
- The base image is pinned to one upstream commit; bump deliberately.
- All data is synthetic (Brindlewood Supply Co.); nothing here is real.
