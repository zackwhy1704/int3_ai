"""
Pure OpenAI ↔ Anthropic message translation.

No I/O, no httpx, no FastAPI — only dict-in / dict-out logic.
This module has 100% unit-test coverage per the engineering standards.

Covered translations (table-driven tests in tests/gateway/test_translate.py):
  - system messages (string body, content-part array, multiple system msgs)
  - tool definitions (OpenAI function → Anthropic tool schema)
  - tool_choice (auto / none / required / specific function)
  - assistant messages with tool_calls → Anthropic tool_use content blocks
  - role:tool messages → Anthropic tool_result blocks, merged consecutively
  - plain text messages (user / assistant)
  - Anthropic text response → OpenAI message
  - Anthropic tool_use response → OpenAI tool_calls message
  - stop_reason mapping: end_turn→stop, max_tokens→length, tool_use→tool_calls
  - streaming: text_delta, input_json_delta, message_delta stop_reason
  - error passthrough (non-200 upstream)
"""

from __future__ import annotations

import json
import os

DEFAULT_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))


def pinned_model() -> str:
    """The model the gateway forces on every request, from config (LLM_MODEL,
    falling back to the backend's ANTHROPIC_MODEL). No hardcoded model id: an
    unset config fails closed rather than silently serving a stale default, and
    the client's requested ``model`` is ignored entirely (server pins it)."""
    model = os.getenv("LLM_MODEL") or os.getenv("ANTHROPIC_MODEL")
    if not model:
        raise RuntimeError(
            "LLM_MODEL (or ANTHROPIC_MODEL) must be set — the gateway pins the model server-side"
        )
    return model


def clamp_max_tokens(requested: object) -> int:
    """Client may ask for fewer tokens than the cap, never more."""
    cap = DEFAULT_MAX_TOKENS
    try:
        want = int(requested)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return cap
    return min(want, cap) if want > 0 else cap


# The ONLY client fields the gateway forwards upstream. Everything else is
# dropped — in particular anything that makes the provider persist the
# request/response (OpenAI `store`, `metadata`), attributes it to a caller-
# chosen identity (`user`), or steers provider-side routing/retention
# (`service_tier`, `prompt_cache_key`, `safety_identifier`, ...). Routing is a
# rebuild from this allowlist, never a verbatim forward of the client body.
_ALLOWED_FIELDS = (
    "messages", "tools", "tool_choice", "temperature", "max_tokens", "stream", "model",
)


def sanitise_request(openai_body: dict) -> dict:
    """Return a new body containing only the allowlisted fields, max_tokens
    clamped. `model` is kept as-is here; the allowlist check / pin happens in
    routing. Dropped fields never reach any provider."""
    out = {k: openai_body[k] for k in _ALLOWED_FIELDS if k in openai_body}
    out["max_tokens"] = clamp_max_tokens(openai_body.get("max_tokens"))
    return out

_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
}


# ---------------------------------------------------------------------------
# OpenAI → Anthropic
# ---------------------------------------------------------------------------


def openai_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    result = []
    for t in tools:
        fn = t.get("function", t)
        result.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        )
    return result


def openai_tool_choice_to_anthropic(tool_choice: object) -> dict | None:
    if tool_choice is None or tool_choice == "none":
        return None
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return {"type": "tool", "name": tool_choice["function"]["name"]}
    return {"type": "auto"}


def openai_messages_to_anthropic(messages: list[dict]) -> list[dict]:
    """
    Converts OpenAI message list to Anthropic format.

    Rules:
    - role:system is dropped (caller extracts it to top-level `system` field).
    - role:assistant with tool_calls → content: [tool_use, …] blocks.
    - role:tool → role:user with tool_result block; consecutive tool results
      are merged into one user message (Anthropic requirement).
    - Plain text messages pass through with role unchanged.
    """
    result: list[dict] = []

    for msg in messages:
        role = msg["role"]

        if role == "system":
            continue

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": msg.get("content") or "",
            }
            # Merge consecutive tool results into the last user message.
            if (
                result
                and result[-1]["role"] == "user"
                and isinstance(result[-1].get("content"), list)
            ):
                result[-1]["content"].append(block)
            else:
                result.append({"role": "user", "content": [block]})
            continue

        if role == "assistant" and msg.get("tool_calls"):
            content_blocks: list[dict] = []
            if msg.get("content"):
                content_blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                args = fn["arguments"]
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": fn["name"],
                        "input": json.loads(args) if isinstance(args, str) else args,
                    }
                )
            result.append({"role": "assistant", "content": content_blocks})
            continue

        result.append({"role": role, "content": msg.get("content", "")})

    return result


def openai_request_to_anthropic(openai_body: dict) -> dict:
    """Top-level converter: full OpenAI chat/completions body → Anthropic messages body."""
    messages: list[dict] = openai_body.get("messages", [])

    system = next((m["content"] for m in messages if m["role"] == "system"), None)

    # The model is whatever routing validated against the tenant's allowlist
    # (routing.resolve rejects anything not on it — no silent substitution);
    # pinned_model() is only a fallback for direct unit calls with no model.
    # max_tokens is already clamped by sanitise_request; clamp again defensively.
    body: dict = {
        "model": openai_body.get("model") or pinned_model(),
        "max_tokens": clamp_max_tokens(openai_body.get("max_tokens")),
        "messages": openai_messages_to_anthropic(messages),
    }

    if system:
        body["system"] = system
    if openai_body.get("stream"):
        body["stream"] = True

    tools = openai_body.get("tools")
    if tools:
        body["tools"] = openai_tools_to_anthropic(tools)
        tc = openai_tool_choice_to_anthropic(openai_body.get("tool_choice"))
        if tc:
            body["tool_choice"] = tc

    return _with_prompt_cache(body)


def _prompt_cache_enabled() -> bool:
    """ON by default; GATEWAY_PROMPT_CACHE=0 disables it. Read per-call so it is configurable
    at runtime and monkeypatch-able in tests."""
    return os.getenv("GATEWAY_PROMPT_CACHE", "1").lower() not in ("0", "false", "no", "")


def _with_prompt_cache(body: dict) -> dict:
    """Mark the STABLE PREFIX — the tool definitions and the system prompt — with Anthropic
    ``cache_control: {type: ephemeral}`` so every later turn in a session reuses it instead of
    re-billing it. The agent's system prompt + tool schemas are the same on every turn of a
    conversation and are the bulk of the input tokens; a cache read is ~10% of the input price
    (one cold write is +25%), so a multi-turn session is much cheaper.

    Breakpoints (Anthropic caches everything up to and including a marked block, in order
    tools -> system -> messages): one on the LAST tool (caches all tool defs) and one on the
    system block (caches tools + system). Messages are left uncached (they change each turn).

    KEY-OWNER IMPLICATION: the cache lives on the upstream provider side, keyed to the API key's
    org. For ``key_owner='theirs'`` (BYOK) the customer's own key caches the customer's own
    prefix — no cross-tenant sharing, since each tenant uses its own key. For ``key_owner='ours'``
    our key caches; still per our org, and the prefix carries no tenant secrets (system prompt +
    tool schemas only). Translation is pure, so this is covered by test_translate.py.
    """
    if not _prompt_cache_enabled():
        return body
    sys = body.get("system")
    if isinstance(sys, str) and sys:
        body["system"] = [{"type": "text", "text": sys, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(sys, list):
        for blk in reversed(sys):
            if isinstance(blk, dict):
                blk["cache_control"] = {"type": "ephemeral"}
                break
    tools = body.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return body


# ---------------------------------------------------------------------------
# Anthropic → OpenAI
# ---------------------------------------------------------------------------


def anthropic_response_to_openai(anthropic_response: dict, model: str) -> dict:
    content_blocks: list[dict] = anthropic_response.get("content", [])
    stop_reason: str = anthropic_response.get("stop_reason", "end_turn")
    finish_reason = _STOP_REASON_MAP.get(stop_reason, "stop")

    text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
    tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]

    message: dict = {"role": "assistant", "content": None}
    if text_parts:
        message["content"] = "\n".join(text_parts)
    if tool_use_blocks:
        message["tool_calls"] = [
            {
                "id": b["id"],
                "type": "function",
                "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
            }
            for b in tool_use_blocks
        ]

    return {
        "id": anthropic_response.get("id", ""),
        "object": "chat.completion",
        "model": anthropic_response.get("model") or model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": anthropic_response.get("usage", {}),
    }


# ---------------------------------------------------------------------------
# Streaming SSE chunk builders
# ---------------------------------------------------------------------------


def text_delta_chunk(text: str) -> bytes:
    chunk = {
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
    }
    return f"data: {json.dumps(chunk)}\n\n".encode()


def tool_call_start_chunk(index: int, call_id: str, name: str) -> bytes:
    chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    }
    return f"data: {json.dumps(chunk)}\n\n".encode()


def tool_call_delta_chunk(index: int, partial_json: str) -> bytes:
    chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {"index": index, "function": {"arguments": partial_json}}
                    ]
                },
                "finish_reason": None,
            }
        ]
    }
    return f"data: {json.dumps(chunk)}\n\n".encode()


def finish_chunk(stop_reason: str) -> bytes:
    finish_reason = _STOP_REASON_MAP.get(stop_reason, "stop")
    chunk = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
    return f"data: {json.dumps(chunk)}\n\n".encode()


def error_chunk(status_code: int, message: str) -> bytes:
    event = json.dumps({"error": {"code": status_code, "message": message}})
    return f"data: {event}\n\n".encode()
