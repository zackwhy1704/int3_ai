"""Per-request routing for the BYOK custodian.

Resolves the caller + requested model to a concrete upstream Route (provider,
base_url, tenant key, allowlist), and normalises each provider's usage shape.

Two caller shapes:
  * "tenant/user"  — GATEWAY_AUTH=service: the tenant's configured providers
                     decide routing; the requested model must be on a provider's
                     allowlist or the request is rejected (no silent substitute).
  * "dev"          — GATEWAY_AUTH=none (local only): a single env-keyed Anthropic
                     route, no allowlist enforcement. Never reached in production
                     (the ENV=production guard rejects none).
"""

import os
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

import provider_config

ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com/v1"


@dataclass
class Route:
    provider: str
    base_url: str
    api_key: Optional[str]
    model: str
    key_owner: str
    daily_cap_usd: Optional[float]


def resolve(caller: str, model: Optional[str]) -> Route:
    # dev / none mode: single env-keyed Anthropic route, no allowlist.
    if "/" not in caller:
        from translate import pinned_model

        return Route(
            provider="anthropic",
            base_url=os.getenv("ANTHROPIC_BASE", ANTHROPIC_DEFAULT_BASE),
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            model=model or pinned_model(),
            key_owner="ours",
            daily_cap_usd=None,
        )

    tenant_id = caller.split("/", 1)[0]
    if not model:
        raise HTTPException(status_code=400, detail="`model` is required")

    # Find the tenant provider whose allowlist contains the requested model.
    # No match -> 400 (never silently substitute a different model).
    for provider in ("anthropic", "openai"):
        cfg = provider_config.load(tenant_id, provider)
        if cfg and model in cfg.model_allowlist:
            if cfg.key_owner == "ours" and cfg.daily_cap_usd is None:
                # Mandatory cap when we pay; refuse rather than serve uncapped.
                raise HTTPException(
                    status_code=503,
                    detail=f"provider {provider} for tenant {tenant_id} has key_owner=ours but no daily cap",
                )
            return Route(
                provider=provider,
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                model=model,
                key_owner=cfg.key_owner,
                daily_cap_usd=cfg.daily_cap_usd,
            )

    raise HTTPException(
        status_code=400,
        detail=f"model {model!r} is not on an allowlist for tenant {tenant_id}",
    )


def normalise_usage(provider: str, usage: dict) -> dict:
    """Collapse each provider's usage shape to {input, output, cache_read,
    cache_write}. OpenAI reports cache reads at
    usage.prompt_tokens_details.cached_tokens and has no cache-write count
    (caching is automatic); Anthropic splits cache_creation (write) and
    cache_read explicitly."""
    usage = usage or {}
    if provider == "anthropic":
        return {
            "input": usage.get("input_tokens") or 0,
            "output": usage.get("output_tokens") or 0,
            "cache_write": usage.get("cache_creation_input_tokens") or 0,
            "cache_read": usage.get("cache_read_input_tokens") or 0,
        }
    # openai-compatible
    details = usage.get("prompt_tokens_details") or {}
    return {
        "input": usage.get("prompt_tokens") or 0,
        "output": usage.get("completion_tokens") or 0,
        "cache_write": 0,
        "cache_read": details.get("cached_tokens") or 0,
    }
