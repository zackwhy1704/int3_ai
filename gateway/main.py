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

import limits
import routing
from auth import GATEWAY_AUTH, verify_token
from translate import sanitise_request
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
    # Fail closed if the gateway's DB role can mutate service_tokens (an env
    # pointing at an over-privileged role must not silently enable token minting).
    if GATEWAY_AUTH == "service":
        import service_auth

        service_auth.assert_least_privilege()
    yield


app = FastAPI(title="Company Brain Gateway", version="0.3.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    h = {"status": "ok", "version": "0.3.0"}
    spend = limits.spend_health()
    if spend is not None:
        # A tenant we pay for is running with an under-counted daily cap.
        h["status"] = "degraded"
        h["spend_recording"] = spend
    return h


@app.get("/v1/models")
async def list_models(_caller: str = Depends(verify_token)):
    """OpenAI-compatible model list. Returns exactly the caller's tenant
    allowlist so a client's model picker can only offer allowlisted models.
    This is the cooperative hint; routing.resolve() is the hard 400-on-miss
    gate that a hand-crafted request can't get around."""
    models = routing.list_models(_caller)
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "company-brain"} for m in models
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    _caller: str = Depends(verify_token),
):
    body = await request.json()
    sanitised = sanitise_request(body)              # field allowlist; drops store/metadata/user/...
    route = routing.resolve(_caller, sanitised.get("model"))  # 400 if model not on the tenant's allowlist
    limits.check_rate_limit(_caller)                # 429 per-user, fail fast before any upstream call
    limits.check_daily_cap(_caller.split("/", 1)[0], route.daily_cap_usd)  # 429 per-tenant daily cap
    if body.get("stream"):
        return StreamingResponse(stream(sanitised, route, _caller), media_type="text/event-stream")
    return await complete(sanitised, route, _caller)
