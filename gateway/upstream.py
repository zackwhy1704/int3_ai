"""
Upstream provider calls (Anthropic + OpenAI-compatible).

Routing is a rebuild from the sanitised request (translate.sanitise_request),
never a verbatim forward of the client body. The tenant's key is injected here;
no client-supplied key is ever used. Usage is normalised per provider
(routing.normalise_usage) and logged with the resolved caller.
"""

from __future__ import annotations

import json as _json
import os
from typing import AsyncGenerator

import httpx
from fastapi import HTTPException

import limits
import routing
from translate import (
    anthropic_response_to_openai,
    error_chunk,
    finish_chunk,
    openai_request_to_anthropic,
    text_delta_chunk,
    tool_call_delta_chunk,
    tool_call_start_chunk,
)

ANTHROPIC_VERSION = "2023-06-01"
_USAGE_LOG = os.environ.get("GATEWAY_USAGE_LOG", "")


def _log_usage(norm: dict, caller: str, provider: str, model: str) -> None:
    """One normalised usage line per call (input/output/cache_write/cache_read)
    plus provider, model and the resolved caller. Cache tokens are recorded
    because caching makes plain input undercount real cost. Feeds the
    client-facing usage view, not an invoice."""
    if not _USAGE_LOG:
        return
    try:
        with open(_USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(
                f"USAGE caller={caller} provider={provider} model={model} "
                f"in={norm['input']} out={norm['output']} "
                f"cache_write={norm['cache_write']} cache_read={norm['cache_read']}\n"
            )
    except OSError:
        pass


def _account(norm: dict, caller: str, provider: str, model: str, key_owner: str = "theirs") -> None:
    """One place that handles a completed call's usage: the client-facing usage
    log line AND the durable per-tenant daily spend counter. Both are
    best-effort and must never raise into the response path. key_owner sets how
    loudly a failed spend write is reported (ERROR + health flag for 'ours')."""
    _log_usage(norm, caller, provider, model)
    limits.record_spend(caller, model, norm, key_owner)


def _anthropic_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}


def _openai_headers(api_key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {api_key}", "content-type": "application/json"}


async def complete(sanitised: dict, route: routing.Route, caller: str = "-") -> dict:
    """Non-streaming. Anthropic: translate then POST /messages. OpenAI-compatible:
    POST the sanitised body to /chat/completions. Tenant key injected here."""
    if route.api_key is None:
        raise HTTPException(status_code=502, detail=f"no key configured for provider {route.provider}")

    if route.provider == "anthropic":
        body = openai_request_to_anthropic(sanitised)
        url, headers, payload = f"{route.base_url}/messages", _anthropic_headers(route.api_key), body
    else:
        url = f"{route.base_url}/chat/completions"
        headers, payload = _openai_headers(route.api_key), {**sanitised, "stream": False}

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    data = resp.json()
    _account(routing.normalise_usage(route.provider, data.get("usage", {})), caller, route.provider, route.model, route.key_owner)
    if route.provider == "anthropic":
        return anthropic_response_to_openai(data, route.model)
    return data  # OpenAI-compatible response passes straight back


async def stream(sanitised: dict, route: routing.Route, caller: str = "-") -> AsyncGenerator[bytes, None]:
    if route.api_key is None:
        yield error_chunk(502, f"no key configured for provider {route.provider}")
        return
    if route.provider == "anthropic":
        async for chunk in _stream_anthropic(sanitised, route, caller):
            yield chunk
    else:
        async for chunk in _stream_openai(sanitised, route, caller):
            yield chunk


async def _stream_anthropic(sanitised, route, caller):
    anthropic_body = openai_request_to_anthropic({**sanitised, "stream": True})
    active_tool_index: int | None = None
    tool_counter = 0
    norm = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST", f"{route.base_url}/messages", headers=_anthropic_headers(route.api_key), json=anthropic_body
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
                try:
                    event = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "message_start":
                    norm = routing.normalise_usage("anthropic", event.get("message", {}).get("usage", {}))
                elif etype == "message_delta":
                    out = (event.get("usage", {}) or {}).get("output_tokens")
                    if out:
                        norm["output"] = out
                if etype == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        active_tool_index = tool_counter
                        tool_counter += 1
                        yield tool_call_start_chunk(active_tool_index, block["id"], block["name"])
                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield text_delta_chunk(delta.get("text", ""))
                    elif delta.get("type") == "input_json_delta" and active_tool_index is not None:
                        yield tool_call_delta_chunk(active_tool_index, delta.get("partial_json", ""))
                elif etype == "content_block_stop":
                    active_tool_index = None
                elif etype == "message_delta":
                    stop_reason = event.get("delta", {}).get("stop_reason")
                    if stop_reason:
                        yield finish_chunk(stop_reason)
                elif etype == "message_stop":
                    _account(norm, caller, "anthropic", route.model, route.key_owner)
                    yield b"data: [DONE]\n\n"
                    return


async def _stream_openai(sanitised, route, caller):
    """OpenAI-compatible pass-through stream: relay SSE bytes unchanged, and if
    the provider emits a usage chunk, normalise + log it. (Built, not called
    live — exercised by mocked tests.)"""
    payload = {**sanitised, "stream": True}
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST", f"{route.base_url}/chat/completions", headers=_openai_headers(route.api_key), json=payload
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield error_chunk(resp.status_code, body.decode())
                return
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    raw = line[6:]
                    if raw != "[DONE]":
                        try:
                            evt = _json.loads(raw)
                            if evt.get("usage"):
                                _account(routing.normalise_usage("openai", evt["usage"]), caller, "openai", route.model, route.key_owner)
                        except _json.JSONDecodeError:
                            pass
                    yield (line + "\n\n").encode()
