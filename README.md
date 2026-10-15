# Company Brain

A multi-tenant **Company Brain**: scoped, cited question answering over a company's own
documents and workflow history, with a truth layer that tracks which claim is still
current — and that can **learn from real work**, capturing a fact only after a human with
authority approves it.

On this branch (`spike/remote-thin-client`) it reaches the user as a **thin client**:

```
stock Hermes Desktop  ──▶  per-user gated Hermes backend  ──▶  brain (MCP)  ──▶  BYOK LLM gateway
   (the official app)        (our hardened fork, one               (truth layer)      (customer's own
                              isolated container per user)                             model key, capped)
```

The user runs the **real, stock Hermes agent** (we ship no custom desktop). What is ours is
the per-user **gated backend**, the **brain**, and the **BYOK gateway**. Nothing is
fine-tuned; permissions are enforced server-side; the brain token lives only in each user's
MCP sidecar.

## Start here

- **[QUICKSTART.md](QUICKSTART.md)** — one command (`make demo`) brings up an isolated
  stack and drives the real Hermes agent through the demo, then prints a ✅/❌ table.
- **[CLAUDE.md](CLAUDE.md)** — the architecture, the invariants, and how we work.
- **[deploy/hermes/README.md](deploy/hermes/README.md)** and
  **[deploy/hermes/DEMO.md](deploy/hermes/DEMO.md)** — the live system on this branch: the
  gated backend, the proxies and startup gate, provisioning, and the desktop connect flow.

## The two test tiers

| command | tier | what it drives |
|---|---|---|
| `make demo` | **Hermes tier** (the real thing) | the stock Hermes agent over `/api/ws`, the desktop's own path |
| `make e2e` | **component tier (bypasses Hermes)** | the gateway model + brain MCP directly — a faster lower-tier check |

Both run in **isolated** compose projects (`hermes-demo` / `hermes-e2e`) and never touch a
running `hermes-deploy`. `make demo-down` / `make e2e-down` remove them.

## The one secret you provide

The LLM key, in `deploy/hermes/.env` (`ANTHROPIC_API_KEY=…`), which **you edit yourself** —
never paste a key into chat. Every other secret for the throwaway stack is generated for
you. All seed and demo data is **synthetic**.

## Repo layout

- `backend/app/` — the FastAPI brain (truth layer, capture-with-approval, retrieval, auth).
- `deploy/hermes/` — the per-user gated backend, proxies, startup gate, provisioning, the
  `demo/` (Hermes tier) and `e2e/` (component tier).
- `gateway/` — the BYOK custodian gateway (OpenAI-compatible `/v1/chat/completions`).
- `seed/` — synthetic tenants (`brindlewood`: marcus/priya/ada) and test fixtures.
