"""
Company Brain Gateway — OpenAI-compatible LLM proxy.

POST /v1/chat/completions  — chat, streaming, tool calls
GET  /health               — liveness check

Auth
----
GATEWAY_AUTH=none  (local dev, explicit in docker-compose dev profile) — no token check.
                   Refused at startup when ENV=production.
GATEWAY_AUTH=oidc  (Phase B) — full RS256 OIDC verification via authcore.
Any other value   — 501 Not Implemented (fail safe, not fail open).

Running locally
---------------
  docker compose --profile dev up gateway
  # or:
  ANTHROPIC_API_KEY=sk-... GATEWAY_AUTH=none uvicorn main:app --port 8001

Running in Cloud Run
---------------------
  All env vars come from Google Secret Manager.
  ANTHROPIC_API_KEY, GATEWAY_AUTH=oidc, OIDC_AUDIENCE=<client_id>, LLM_MODEL
"""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse

from auth import GATEWAY_AUTH, verify_token
from upstream import complete, stream

ENV = os.getenv("ENV", "development")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to start in production with auth disabled (D3 / security guard).
    if ENV == "production" and GATEWAY_AUTH == "none":
        raise RuntimeError(
            "GATEWAY_AUTH=none is not allowed when ENV=production. "
            "Set GATEWAY_AUTH=oidc and configure authcore for production."
        )
    yield


app = FastAPI(title="Company Brain Gateway", version="0.3.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.3.0"}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    _caller: str = Depends(verify_token),
):
    body = await request.json()
    if body.get("stream"):
        return StreamingResponse(stream(body), media_type="text/event-stream")
    return await complete(body)
