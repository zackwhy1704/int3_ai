"""
Anthropic API proxy.

Translates OpenAI-format chat completion requests into Anthropic Messages API
calls, and translates the responses back.

Why translate instead of using the Anthropic SDK directly?
- Hermes Agent uses the OpenAI-compatible format for all LLM calls.
- Future providers (Vertex, OpenAI) can be added here without touching the gateway
  interface or any client code.

Streaming
---------
Anthropic uses server-sent events with its own event types. We translate them to
OpenAI SSE format so Hermes (and any OpenAI-compatible client) can consume them
without changes.
"""

import os
from typing import AsyncGenerator

import httpx

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096

_HEADERS = {
    "x-api-key": ANTHROPIC_API_KEY,
    "anthropic-version": ANTHROPIC_VERSION,
    "content-type": "application/json",
}


def _to_anthropic_body(openai_body: dict) -> dict:
    """Converts an OpenAI-format chat request to an Anthropic Messages request."""
    messages: list[dict] = openai_body.get("messages", [])

    # Anthropic puts the system prompt as a top-level field, not in messages.
    system = next(
        (m["content"] for m in messages if m["role"] == "system"), None
    )
    user_messages = [m for m in messages if m["role"] != "system"]

    body: dict = {
        "model": openai_body.get("model", DEFAULT_MODEL),
        "max_tokens": openai_body.get("max_tokens", DEFAULT_MAX_TOKENS),
        "messages": user_messages,
    }
    if system:
        body["system"] = system
    if openai_body.get("stream"):
        body["stream"] = True

    return body


def _to_openai_response(anthropic_response: dict, model: str) -> dict:
    """Converts an Anthropic Messages response to OpenAI chat completion format."""
    content_blocks = anthropic_response.get("content", [])
    text = " ".join(
        block["text"] for block in content_blocks if block.get("type") == "text"
    )
    return {
        "id": anthropic_response.get("id", ""),
        "object": "chat.completion",
        "model": anthropic_response.get("model", model),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": anthropic_response.get("stop_reason", "stop"),
            }
        ],
        "usage": anthropic_response.get("usage", {}),
    }


async def _stream_anthropic_to_openai(
    anthropic_body: dict,
) -> AsyncGenerator[bytes, None]:
    """
    Streams Anthropic SSE events and re-encodes them as OpenAI SSE format.

    Anthropic event types we handle:
      content_block_delta → delta.type="text_delta" → yield OpenAI chunk
      message_stop        → yield [DONE]
    """
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{ANTHROPIC_BASE}/messages",
            headers=_HEADERS,
            json=anthropic_body,
        ) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                yield f"data: {{\"error\": \"{resp.status_code}\", \"detail\": {error_body.decode()!r}}}\n\n".encode()
                return

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    yield b"data: [DONE]\n\n"
                    return
                try:
                    import json
                    event = json.loads(raw)
                except Exception:
                    continue

                event_type = event.get("type")

                if event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        chunk = {
                            "choices": [
                                {"index": 0, "delta": {"content": text}, "finish_reason": None}
                            ]
                        }
                        import json
                        yield f"data: {json.dumps(chunk)}\n\n".encode()

                elif event_type == "message_stop":
                    yield b"data: [DONE]\n\n"
                    return


async def proxy_to_anthropic(openai_body: dict, stream: bool):
    """
    Entry point called by main.py.

    Returns:
      - stream=False: dict (JSON-serialisable OpenAI response)
      - stream=True:  async generator of SSE bytes
    """
    anthropic_body = _to_anthropic_body(openai_body)
    model = openai_body.get("model", DEFAULT_MODEL)

    if stream:
        return _stream_anthropic_to_openai(anthropic_body)

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{ANTHROPIC_BASE}/messages",
            headers=_HEADERS,
            json=anthropic_body,
        )
        if resp.status_code != 200:
            from fastapi import HTTPException
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return _to_openai_response(resp.json(), model)
