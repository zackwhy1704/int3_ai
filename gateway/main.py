"""
Company Brain Gateway — OpenAI-compatible LLM proxy.

Exposes POST /v1/chat/completions (and other standard endpoints).
Holds the Anthropic API key server-side; no client machine ever sees it.

Auth
----
Gate 2 (dev): GATEWAY_AUTH=none — all requests pass through, no token check.
Gate 4+:      GATEWAY_AUTH=oidc — validates the caller's Google OIDC Bearer token.
              See auth.py for verification logic.

LLM routing
-----------
Translates OpenAI-format requests to the Anthropic Messages API and back.
To add another provider (e.g. OpenAI, Vertex), extend proxy.py.

Running locally
---------------
  docker compose up gateway
  # or:
  ANTHROPIC_API_KEY=sk-... GATEWAY_AUTH=none uvicorn gateway.main:app --port 8001

Running in Cloud Run (production)
----------------------------------
  All env vars injected from Google Secret Manager.
  ANTHROPIC_API_KEY, GATEWAY_AUTH=oidc, OIDC_AUDIENCE=<client_id>
"""

import os

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse

from .auth import verify_token
from .proxy import proxy_to_anthropic

app = FastAPI(title="Company Brain Gateway", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    _caller: str = Depends(verify_token),
):
    """
    OpenAI-compatible chat completions endpoint.

    Accepts: { messages, model, stream, max_tokens, ... }
    Returns: OpenAI-format response (or SSE stream if stream=true)

    The caller's identity (_caller) is available for metering in a future gate.
    """
    body = await request.json()
    stream: bool = body.get("stream", False)
    result = await proxy_to_anthropic(body, stream=stream)

    if stream:
        return StreamingResponse(result, media_type="text/event-stream")
    return result
