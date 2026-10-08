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

DEFAULT_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
DEFAULT_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))

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

    body: dict = {
        "model": openai_body.get("model") or DEFAULT_MODEL,
        "max_tokens": openai_body.get("max_tokens") or DEFAULT_MAX_TOKENS,
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
