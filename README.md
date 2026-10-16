# Company Brain

A multi-tenant **Company Brain**: scoped, cited question answering over a company's own
documents and workflow history, with a truth layer that tracks which claim is still
current — and that can **learn from real work**, capturing a fact only after a human with
authority approves it.

On this branch (`spike/remote-thin-client`) it reaches the user as a **thin client**:

```
stock Hermes Desktop  ──▶  per-user gated Hermes backend  ──▶  brain (MCP)  ──▶  BYOK LLM gateway
   (the official app)        (STOCK Hermes v0.21.7 in our         (truth layer)      (customer's own
                              gated container, one per user)                         model key, capped)
```

The user runs the **real, stock Hermes agent** — we neither fork nor modify Hermes. What is ours
is the **gating around stock Hermes** (the per-user container, default-deny proxies, startup gate),
the **brain**, and the **BYOK gateway**. Nothing is fine-tuned; permissions are enforced
server-side; the brain token lives only in each user's MCP sidecar.

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

### Test status (honest)

- **`make demo` with the STEERED prompts (`DEMO_SCAFFOLD=1`): PASSED 10/10** on a funded CI run
  (GitHub run 38838056914) — the full owner-gated capture workflow + default-deny egress, on the real
  agent over `/api/ws`. **`make e2e` (component tier): green.**
- **`make demo` DEFAULT is now the NATURAL sentences (no steering) — the pilot-capture bar — and is
  UNVALIDATED.** The capture fix is implemented (native Hermes memory disabled for gated users, a
  `SOUL.md` routing instruction, trimmed toolsets, gateway prompt caching — see
  **[deploy/hermes/CAPTURE_PILOT.md](deploy/hermes/CAPTURE_PILOT.md)**), but whether a plain sentence
  reliably reaches `propose_claim`/`approve_proposal`/`ask_brain` has **not** been run yet (held on
  cost grounds). `DEMO_SCAFFOLD=1` reverts to the proven steered path.
- **Open — owner-check RED→GREEN regression: UNVALIDATED.** `deploy/hermes/demo/ci_regression.sh`
  removes the owner check in both layers, asserts the capture regresses, then restores it. The
  baseline probe passes (GREEN) and the re-apply fix is committed, but a full GREEN→RED→GREEN has
  **not** been observed end-to-end. It is **manual-only** in CI (`workflow_dispatch` with
  `run_regression=true`) and is belt-and-suspenders — the demo's N1a/N1b already show the owner check
  blocking a non-owner on the real agent.

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
