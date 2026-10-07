"""
Company Brain Gateway — OpenAI-compatible LLM proxy.

POST /v1/chat/completions  — chat, streaming, tool calls
GET  /health               — liveness check

Auth
----
GATEWAY_AUTH=none  (local dev, explicit in docker-compose) — no token check.
GATEWAY_AUTH=oidc  (Gate 4+) — full RS256 OIDC verification.
Any other value   — 501 Not Implemented (fail safe, not fail open).

Running locally
---------------
  docker compose up gateway
  # or:
  ANTHROPIC_API_KEY=sk-... GATEWAY_AUTH=none uvicorn main:app --port 8001

Running in Cloud Run
---------------------
  All env vars come from Google Secret Manager.
  ANTHROPIC_API_KEY, GATEWAY_AUTH=oidc, OIDC_AUDIENCE=<client_id>, LLM_MODEL
"""

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse

from auth import verify_token
from upstream import complete, stream

app = FastAPI(title="Company Brain Gateway", version="0.2.0")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": "0.2.0"}


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    _caller: str = Depends(verify_token),
):
    body = await request.json()
    if body.get("stream"):
        return StreamingResponse(stream(body), media_type="text/event-stream")
    return await complete(body)
