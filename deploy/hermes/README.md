# Company Brain — thin-client Hermes pilot

Stock Hermes Desktop as the client; everything sensitive stays server-side, one
isolated backend per user. **All data in this pilot is synthetic** — Brindlewood
Supply Co. and every person, document, and number in it are invented.

```
  stock Hermes Desktop ──▶ per-user gated backend ──▶ brain over MCP  (that user's brain token)
      (on the laptop)        (container, locked)   └─▶ LLM over our BYOK gateway (that user's gateway token)
```

What each piece guarantees:
- **Per-user brain token.** Each user has their own MCP sidecar holding their own
  brain token; the brain scopes every answer to that user's memberships. The
  brain token is **never** placed in the Hermes backend container (provisioning
  asserts this), so a compromised backend cannot read the brain as the user.
- **BYOK gateway.** The model provider key lives only in the gateway, which
  injects it per tenant, enforces the per-tenant model allowlist, and caps spend.
  The Hermes container never holds a provider key.
- **Default-deny egress.** User backends sit on an internal network; the only way
  out is the edge proxy, which (empty allowlist) refuses every connection. The
  backend reaches only the brain and the gateway, directly, and nothing else.
- **Startup gate.** The backend refuses to serve unless the read boundary, the
  route allowlist, and the egress lock are all provably in force.

## Prerequisites

- Docker + Docker Compose.
- The stock Hermes backend base image, built **reproducibly from upstream at a
  pinned commit** — no hand-built local image:

  ```
  ./build-base.sh        # NousResearch/hermes-agent @ 73162b00eefde3794bed0afb53d84a19c0eed230
  ```

  It builds `hermes-base-pinned` from a git build context, so the server-side
  Hermes version is fixed and cannot drift. Bump the commit only with
  justification (`HERMES_COMMIT=... ./build-base.sh`) and update this SHA. The
  gated image layers the proxy + startup gate on top (`gated/Dockerfile.gated`,
  `BASE=hermes-base-pinned`).

## Bring up the shared base (one command)

```
cd deploy/hermes
cp .env.example .env         # then fill in — each line says how to generate it
docker compose up -d --build # control DB, init+seed (brindlewood), brain, gateway (service), edge
```

`init` creates the control schema and least-privilege roles and provisions the
synthetic **brindlewood** tenant (3 users: marcus=finance, priya=operations,
ada=leadership).

## Provision a user

Build the gated image once, then provision each user:

```
docker build -t hermes-gated --build-arg BASE=hermes-base-pinned \
  -f gated/Dockerfile.gated gated
./provision-user.sh brindlewood marcus 9131 --key-owner theirs
```

Run `./verify-isolation.sh` after provisioning two users to assert a user cannot
reach another user's brain (network + auth).

This mints two distinct tokens (brain token → the user's MCP sidecar; gateway
token → the user's Hermes config), starts both containers, asserts the brain
token is absent from the Hermes container, and writes a connect file to
`connect/brindlewood-marcus.connect.json`.

---

## For the partner (plain language)

> **Where this runs today.** The connect file points at `http://127.0.0.1:<port>`
> and the backend binds to loopback on the Docker host. It therefore works ONLY
> on the machine running Docker — you cannot yet point your own laptop's Hermes
> Desktop at it over the network, and there is no TLS. Hosted ingress you can
> reach from your laptop (a public HTTPS endpoint, per user) is **Phase 3**
> (GCP + Caddy + TLS) and does not exist yet. For now, test on the Docker host
> itself (or via an SSH tunnel you set up).

You will use **stock Hermes Desktop** — nothing custom to install beyond Hermes
itself.

1. **Install Hermes Desktop** the normal way.
2. **Add your backend.** We will send you a small connect file (for example
   `brindlewood-marcus.connect.json`). It contains a URL like
   `http://127.0.0.1:9131` and a session token. In Hermes, add a remote backend
   with that URL and paste the session token. (Today this must be done on the
   Docker host — see the note above.)
3. **Ask the company brain.** Everything you ask is answered only from the
   company knowledge base, with citations. If the knowledge base doesn't cover
   something, Hermes will tell you it isn't there rather than guess.

Five questions to try (the first three are answered with citations; the last two
should be politely refused — that is the brain declining to make things up):

1. How many days of annual leave do staff get?
2. What is the hotel cap for London?
3. How long do new starters have an onboarding buddy?
4. What is the WiFi password for the guest network?   *(expect a refusal)*
5. Who won the company chess tournament in 2019?        *(expect a refusal)*

Each signed-in user sees only what their role is allowed to see: the finance
user can ask about payment terms, the operations user about supplier incidents,
and neither can read the other's restricted material — the brain refuses, with no
hint of what it is hiding.

*Reminder: this pilot runs on invented data. Do not treat any answer as real.*
