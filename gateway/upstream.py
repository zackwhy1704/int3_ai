"""
Upstream Anthropic API calls.

All httpx I/O lives here. translate.py handles the pure dict conversion;
this module handles connection, error propagation, and streaming.
"""

from __future__ import annotations

import os
from typing import AsyncGenerator

import httpx
from fastapi import HTTPException

from translate import (
    anthropic_response_to_openai,
    error_chunk,
    finish_chunk,
    openai_request_to_anthropic,
    text_delta_chunk,
    tool_call_delta_chunk,
    tool_call_start_chunk,
)

ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# Spike instrumentation: append per-call token usage so a harness can attribute
# tokens to each question (OpenAI-shaped usage is not emitted on the stream
# path, and Hermes does not record usage for custom providers). Off unless
# GATEWAY_USAGE_LOG is set, so production behaviour is unchanged.
_USAGE_LOG = os.environ.get("GATEWAY_USAGE_LOG", "")


def _log_usage(usage: dict, caller: str = "-") -> None:
    """Append one per-call usage line: plain + cache tokens + the resolved
    caller (tenant/user from GATEWAY_AUTH=service). Cache tokens are logged
    because prompt caching makes plain input_tokens undercount real cost — a
    cache-read turn bills cache_read at a fraction, and the first turn bills
    cache_creation at a premium; item-5 numbers are meaningless without them."""
    if not _USAGE_LOG:
        return
    try:
        with open(_USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(
                f"USAGE caller={caller} "
                f"in={usage.get('input_tokens') or 0} "
                f"out={usage.get('output_tokens') or 0} "
                f"cache_write={usage.get('cache_creation_input_tokens') or 0} "
                f"cache_read={usage.get('cache_read_input_tokens') or 0}\n"
            )
    except OSError:
        pass


def _headers() -> dict[str, str]:
    return {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


async def complete(openai_body: dict, caller: str = "-") -> dict:
    """Non-streaming: send request, return OpenAI-format dict."""
    anthropic_body = openai_request_to_anthropic(openai_body)
    model: str = anthropic_body["model"]  # server-pinned; client model ignored

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{ANTHROPIC_BASE}/messages",
            headers=_headers(),
            json=anthropic_body,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    body = resp.json()
    _log_usage(body.get("usage", {}) or {}, caller)
    return anthropic_response_to_openai(body, model)


async def stream(openai_body: dict, caller: str = "-") -> AsyncGenerator[bytes, None]:
    """
    Streaming: yields OpenAI-format SSE bytes.

    Anthropic streaming event flow:
      message_start
      content_block_start  (type: text | tool_use)
      content_block_delta  (type: text_delta | input_json_delta)
      content_block_stop
      message_delta        (carries stop_reason)
      message_stop
    """
    anthropic_body = openai_request_to_anthropic({**openai_body, "stream": True})

    active_tool_index: int | None = None
    tool_counter = 0
    usage_in = 0
    usage_out = 0
    cache_write = 0
    cache_read = 0

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST",
            f"{ANTHROPIC_BASE}/messages",
            headers=_headers(),
            json=anthropic_body,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield error_chunk(resp.status_code, body.decode())
                return

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    yield b"data: [DONE]\n\n"
                    return

                import json as _json

                try:
                    event = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue

                etype = event.get("type")

                if etype == "message_start":
                    mu = event.get("message", {}).get("usage", {}) or {}
                    usage_in = mu.get("input_tokens", 0) or 0
                    cache_write = mu.get("cache_creation_input_tokens", 0) or 0
                    cache_read = mu.get("cache_read_input_tokens", 0) or 0
                elif etype == "message_delta":
                    usage_out = (event.get("usage", {}) or {}).get(
                        "output_tokens", usage_out
                    ) or usage_out

                if etype == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        active_tool_index = tool_counter
                        tool_counter += 1
                        yield tool_call_start_chunk(
                            active_tool_index, block["id"], block["name"]
                        )

                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield text_delta_chunk(delta.get("text", ""))
                    elif (
                        delta.get("type") == "input_json_delta"
                        and active_tool_index is not None
                    ):
                        yield tool_call_delta_chunk(
                            active_tool_index, delta.get("partial_json", "")
                        )

                elif etype == "content_block_stop":
                    active_tool_index = None

                elif etype == "message_delta":
                    stop_reason = event.get("delta", {}).get("stop_reason")
                    if stop_reason:
                        yield finish_chunk(stop_reason)

                elif etype == "message_stop":
                    _log_usage(
                        {
                            "input_tokens": usage_in,
                            "output_tokens": usage_out,
                            "cache_creation_input_tokens": cache_write,
                            "cache_read_input_tokens": cache_read,
                        },
                        caller,
                    )
                    yield b"data: [DONE]\n\n"
                    return
