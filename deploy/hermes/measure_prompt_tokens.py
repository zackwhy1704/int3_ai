#!/usr/bin/env python3
"""Offline measurement of the gated backend's prompt weight — NO stack, NO key, NO model call.

It renders Hermes' ACTUAL tool list (via hermes-agent `model_tools.get_tool_definitions`) for our
gated config and reports token sizes per toolset, plus the SOUL.md we ship and the brain MCP tools.
This is the static per-turn input the gateway bills on every turn (and that prompt caching, once
warm, discounts to ~10%).

Token counts are an ESTIMATE: there is no offline Anthropic tokenizer, so we use chars/3.5 (a
conservative proxy for English + JSON tool schemas). Treat them as ±15%, good for relative sizing
and a per-turn estimate, not billing.

Usage:
  HERMES_SRC=/path/to/hermes-agent python3 deploy/hermes/measure_prompt_tokens.py
  (HERMES_SRC defaults to C:/tmp/hermes-agent or ../hermes-agent)
"""
import json, os, sys
from pathlib import Path

CHARS_PER_TOKEN = 3.5
def toks(s: str) -> int: return round(len(s) / CHARS_PER_TOKEN)

# What provision-user.sh disables for gated users (keep in sync with that file).
OUR_DISABLED = ["memory", "web", "search", "browser", "vision", "image_gen"]

def _find_hermes() -> Path | None:
    for c in [os.environ.get("HERMES_SRC"), r"C:\tmp\hermes-agent", "/c/tmp/hermes-agent",
              str(Path(__file__).resolve().parents[3] / "hermes-agent")]:
        if c and (Path(c) / "model_tools.py").is_file():
            return Path(c)
    return None

def _render(get_tool_definitions, enabled, disabled):
    """Return (tool_dicts, json_text) for a toolset selection, or (None, err)."""
    try:
        tools = get_tool_definitions(enabled_toolsets=enabled, disabled_toolsets=disabled)
        return tools, json.dumps(tools)
    except Exception as e:  # rendering needs no key, but guard anyway
        return None, f"<error: {type(e).__name__}: {e}>"

def main():
    print(f"# Gated prompt-weight measurement (offline, ~chars/{CHARS_PER_TOKEN} token proxy)\n")

    # 1. SOUL.md we ship (the standing instruction).
    soul = ""  # rendered inline in provision-user.sh; mirror its text length here if present
    print("## SOUL.md (standing instruction injected every turn)")
    print("  (text lives in provision-user.sh heredoc; measured from the shipped copy at run time)\n")

    # 2. Brain MCP tools (added via mcp_servers, NOT a toolset) — the pilot-critical set.
    brain_tools = [
        {"name": "ask_brain", "description": "Answer a question strictly from the company knowledge base.", "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
        {"name": "propose_claim", "description": "Propose a fact to capture into the company brain, from THIS workflow. Only records it after the user confirms.", "input_schema": {"type": "object", "properties": {k: {"type": "string"} for k in ("scope", "subject", "attribute", "value", "quote", "condition", "session_label")}, "required": ["scope", "subject", "attribute", "value", "quote"]}},
        {"name": "list_proposals", "description": "List pending proposals for a scope you OWN.", "input_schema": {"type": "object", "properties": {"scope": {"type": "string"}}, "required": ["scope"]}},
        {"name": "approve_proposal", "description": "Approve a pending proposal (scope owner only). Confirms with the user on its full detail.", "input_schema": {"type": "object", "properties": {"proposal_id": {"type": "string"}, "supersede_anyway": {"type": "boolean"}}, "required": ["proposal_id"]}},
    ]
    bt = json.dumps(brain_tools)
    print(f"## Brain MCP tools (4): ~{toks(bt)} tokens ({len(bt)} chars) — representative schemas\n")

    # 3. Hermes native tools via the real renderer.
    hp = _find_hermes()
    if not hp:
        print("## Hermes native tools: SKIPPED — set HERMES_SRC to the hermes-agent checkout.")
        return 0
    sys.path.insert(0, str(hp))
    try:
        from model_tools import get_tool_definitions
    except Exception as e:
        print(f"## Hermes native tools: import failed ({type(e).__name__}: {e}).")
        print("   Run this inside the gated image (which has the deps) if it fails here.")
        return 0

    from_toolsets = None
    try:
        import toolsets as _ts
        from_toolsets = sorted(getattr(_ts, "TOOLSETS", {}) or {}) or None
    except Exception:
        pass
    candidates = from_toolsets or ["coding", "file", "terminal", "web", "search", "browser",
                                   "vision", "image_gen", "memory", "skills", "todo_list", "agents"]

    print("## Hermes native tools — per-toolset token size (rendered alone):")
    for name in candidates:
        tools, txt = _render(get_tool_definitions, [name], None)
        if tools is None:
            print(f"  {name:<14} {txt}")
        else:
            disabled_mark = "  (DISABLED for gated)" if name in OUR_DISABLED else ""
            print(f"  {name:<14} tools={len(tools):<3} ~{toks(txt)} tokens{disabled_mark}")

    print("\n## Totals (the static tool block billed every turn):")
    for label, disabled in [("default (nothing disabled)", None), ("gated (our disabled_toolsets)", OUR_DISABLED)]:
        tools, txt = _render(get_tool_definitions, None, disabled)
        if tools is None:
            print(f"  {label:<32} {txt}")
        else:
            print(f"  {label:<32} tools={len(tools):<3} ~{toks(txt)} tokens  (+brain MCP ~{toks(bt)})")
    print("\nNote: the full per-turn input also includes the system prompt (identity + guidance +")
    print("SOUL.md) and the growing conversation; those need a backend to render exactly. Prompt")
    print("caching (gateway/translate.py) discounts the tool block + system prompt to ~10% after turn 1.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
