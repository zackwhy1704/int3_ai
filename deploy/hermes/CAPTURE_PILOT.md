# Pilot-ready capture — gap, diagnosis, recommendation, test plan

**Status: design only. No code change and no CI run is implied by this document.** It is the plan to
get from "the demo passes with steering" to "a real user's natural sentence is captured correctly."

## What commit `b87ecb3` actually changed (honest)

To make the Hermes-tier demo reach the brain's capture tools, `b87ecb3` rewrote the demo/regression
prompts from natural language into **structured, steered prompts** that name the tool and its
arguments, e.g.:

> "Use the company brain tools (propose_claim / list_proposals / approve_proposal), not your own
> notes or memory. Propose a new fact for the owner to approve. scope: operations; subject: loading
> bay …; attribute: opening time; value: 6am on weekdays; quote: '…'."

That is a **test scaffold**, not pilot behaviour. It passes because it tells the agent exactly which
tool to call and what to pass. A pilot user will type a plain sentence ("Remember for operations that
the loading bay now opens at 6am on weekdays."), and — as CI run 38759011921 showed — the stock
agent then reaches for its **own native memory tool** ("Saved to memory …"), never the brain. So the
green demo proves the *backend + owner gate + /api/ws path* work; it does **not** yet prove
pilot-ready capture.

## Diagnosis (hermes-agent v0.21.7 source; Hermes is NOT modified)

**(a) Which native tool catches "remember …", where is it registered, can it be disabled per backend?**
- The tool is the single **`memory`** tool — `tools/memory_tool.py:214` (`def memory_tool(...)`), whose
  description is "Save durable facts to persistent memory that survive across sessions"
  (`tools/memory_tool.py:349`). It writes `MEMORY.md` (agent notes) / `USER.md` (user memory) under
  `HERMES_HOME` (`tools/memory_tool.py:2-5`).
- It is part of the **`memory` toolset**, gated by `enabled_toolsets` / `disabled_toolsets`
  (`agent/agent_init.py:1093,1113-1117,1304-1312`).
- **Yes, it is config-disableable per backend** (both are `config.yaml` keys our provisioning already
  writes to `/opt/data/config.yaml`):
  - `memory.memory_enabled: false` — `hermes_cli/config_defaults.py:1311-1317` ("To disable memory:
    memory_enabled").
  - and/or `disabled_toolsets: ["memory"]` — `hermes_cli/config_defaults.py:279`.

**(b) Can operator config steer "remember for <scope> …" → propose_claim, company questions → ask_brain?**
- Yes. The backend auto-loads a standing-instructions context file under `HERMES_HOME` —
  `SOUL.md` / `AGENTS.md` / `CLAUDE.md` / `.hermes.md` (`hermes_cli/config_defaults.py:197,519`;
  char-capped at `context_file_char_limit`). There are also `agent.coding_instructions`
  (`:206-209`), `persona_prompt_file` (`:1116`), and custom personalities' `system_prompt`
  (`:1751`). Any of these can carry: *"To remember or capture a fact for a scope (operations,
  finance, …), call the company brain's `propose_claim` so the scope owner can approve it — never
  your own memory. To answer a question about the company, call `ask_brain` and cite it."* Our
  provisioning writes `/opt/data` (= `HERMES_HOME`), so dropping a `SOUL.md` there is a provisioning
  change, not a Hermes change.

**(c) Can per-user native memory be turned off entirely for gated users?**
- Yes — `memory.memory_enabled: false` **plus** `memory.user_profile_enabled: false`
  (`hermes_cli/config_defaults.py:1311-1312`) disables both the `memory` tool and the injection of
  `MEMORY.md` / `USER.md` into the system prompt. This also satisfies the posture rule **"Hermes
  per-user memory must not be read or synced"**: with it off there is no per-user memory store in play.

## Recommendation

**Primary: Option C — disable native memory for gated users, reinforced by a one-line standing
instruction (Option B).**

- At provision, write to `/opt/data/config.yaml`: `memory.memory_enabled: false` and
  `memory.user_profile_enabled: false`; optionally `disabled_toolsets: ["memory"]` as belt-and-braces.
- Also write a short `/opt/data/SOUL.md` (or `.hermes.md`) with the routing instruction in (b).

Why this one:

| Option | What it does | Trade-off |
|---|---|---|
| A — leave as is (steered prompts) | demo passes | not pilot behaviour; a real sentence still hits native memory |
| B — SOUL.md steering only | nudges routing | the `memory` tool still exists; the model *may* still pick it |
| **C (+B) — disable memory + steer** | removes the competing tool entirely, so capture *must* go to `propose_claim`; also enforces "no per-user memory" | gated users lose Hermes' native cross-session memory — which for this product is **desired**: the brain *is* the shared, owner-gated memory |

C is both the most reliable capture fix and the one that matches the security posture. It is a
**provisioning/config** change only (no Hermes edit, no new tool).

**STATUS: IMPLEMENTED (config/provisioning only, unvalidated pending one approved run).**
`deploy/hermes/provision-user.sh` now writes `memory.memory_enabled: false` +
`user_profile_enabled: false`, `agent.disabled_toolsets: [memory, web, search, browser, vision,
image_gen]`, and a `/opt/data/SOUL.md` carrying the routing instruction above. The startup gate
(`gated/startup-gate.sh`) refuses to boot unless `memory_enabled: false` is set. The demo now sends
the **natural** sentences by default (below); `DEMO_SCAFFOLD=1` restores the old steered prompts.

## Test plan — the bar for "pilot-ready capture" (UNEXECUTED; run only on explicit approval)

Re-run the Hermes-tier scenario with the **natural DEMO.md sentences and NO steering prefix / no
tool names / no structured args**, against backends provisioned per Option C:

1. priya: **"Remember for operations that the loading bay now opens at 6am on weekdays."**
   → must create a pending **propose_claim** proposal (not a native-memory write); approval prompt
   shows the exact statement.
2. priya: **"Show me pending operations proposals and approve the loading bay one."**
   → must be **blocked** (priya is not the owner) — no record, proposal still pending.
3. ada: **"Show me pending operations proposals and approve the loading bay one."**
   → approves; a workflow-authority record is written, citing priya as proposer.
4. priya: **"What time does the loading bay open?"** → answered, cited to the operations workflow record.

Pass bar = the same server-side assertions the current demo uses (DB rows + the sidecar `CB_ASK`
signal + owner gate), achieved **without** any steering. Until this passes on a real run, capture is
implemented but not yet *proven* pilot-ready.

**This is now the default `make demo` path** (`run_demo.sh`: natural sentences via `cap <natural>
<scaffold>`; `DEMO_SCAFFOLD=1` reverts to steered). It is **not yet executed** — running it spends
real model budget and needs explicit approval of one run.
