"""Per-user rate limit + per-tenant daily spend cap (item e), fail-fast 429.

Rate limit: an in-process sliding window per caller (tenant/user) — abuse /
runaway protection for one gateway process, not a distributed quota (with N
replicas each enforces its own share). Config RATE_LIMIT_PER_MIN (default 60);
0 disables.

Daily cap: a DURABLE per-tenant counter in the control DB (tenant_usage),
checked before each call and incremented after. Durable on purpose — an
in-memory counter resets on restart and lets spend run past the cap, which
matters most when key_owner='ours' (we pay). Only tenants with a daily_cap_usd
are capped; routing already refuses key_owner='ours' without one.

Cost is an ESTIMATE from an approximate per-model price table (USD per 1M
tokens), overridable via GATEWAY_PRICES (JSON). The cap is a guardrail and the
usage view is client-facing, NOT an invoice; cache tokens are folded into input
at the input rate as a rough proxy.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException

log = logging.getLogger("limits")

RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))
_WINDOW_S = 60.0
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()

# Approximate list prices, USD per 1M tokens: (input, output). GUARDRAIL
# estimates only — not billing. Override per deployment with GATEWAY_PRICES,
# e.g. GATEWAY_PRICES='{"claude-sonnet-5-5":[3,15]}'.
_DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (3.0, 15.0),
}
_FALLBACK_PRICE = (3.0, 15.0)


def _prices() -> dict:
    raw = os.getenv("GATEWAY_PRICES")
    if not raw:
        return _DEFAULT_PRICES
    try:
        return {k: tuple(v) for k, v in json.loads(raw).items()}
    except (ValueError, TypeError):
        log.warning("GATEWAY_PRICES is not valid JSON; using default price table")
        return _DEFAULT_PRICES


def check_rate_limit(caller: str, *, now: float | None = None) -> None:
    """Raise 429 if `caller` exceeded RATE_LIMIT_PER_MIN requests in the last
    60s. RATE_LIMIT_PER_MIN<=0 disables the limit. `now` is injectable for tests."""
    if RATE_LIMIT_PER_MIN <= 0:
        return
    t = time.monotonic() if now is None else now
    with _lock:
        dq = _hits[caller]
        cutoff = t - _WINDOW_S
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_PER_MIN:
            retry = max(1, int(_WINDOW_S - (t - dq[0])) + 1)
            raise HTTPException(
                status_code=429,
                detail=f"rate limit of {RATE_LIMIT_PER_MIN} requests/min exceeded for {caller}",
                headers={"Retry-After": str(retry)},
            )
        dq.append(t)


def estimate_cost_usd(model: str, usage: dict) -> float:
    """Rough USD for one call from normalised usage
    ({input,output,cache_read,cache_write}). Cache tokens fold into input at the
    input rate — a proxy, not an invoice."""
    pin, pout = _prices().get(model, _FALLBACK_PRICE)
    usage = usage or {}
    inp = (usage.get("input") or 0) + (usage.get("cache_read") or 0) + (usage.get("cache_write") or 0)
    out = usage.get("output") or 0
    return inp / 1_000_000 * pin + out / 1_000_000 * pout


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def _read_spend(tenant_id: str) -> float:
    """Today's recorded spend for a tenant (0.0 if none). Reads as gateway_auth.
    Deliberately NOT wrapped in try/except: if we cannot read the counter we
    must not blindly allow, so the request errors rather than bypassing the cap."""
    import psycopg

    from service_auth import _gateway_dsn

    with psycopg.connect(**_gateway_dsn(), autocommit=True) as conn:
        row = conn.execute(
            "SELECT spend_usd FROM tenant_usage WHERE tenant_id = %s AND day = %s",
            (tenant_id, _today()),
        ).fetchone()
    return float(row[0]) if row else 0.0


def check_daily_cap(tenant_id: str, cap_usd) -> None:
    """Raise 429 if the tenant has already reached its daily cap. No cap -> allow
    (and no DB read, so dev/none callers never touch the DB)."""
    if cap_usd is None:
        return
    spent = _read_spend(tenant_id)
    if spent >= float(cap_usd):
        raise HTTPException(
            status_code=429,
            detail=f"daily spend cap of ${cap_usd} reached for tenant {tenant_id} (recorded ~${spent:.4f})",
            headers={"Retry-After": "3600"},
        )


def record_spend(caller: str, model: str, usage: dict) -> None:
    """Add one call's estimated cost to the tenant's daily counter. Best-effort:
    a write failure is logged and swallowed so it never breaks the LLM response
    (the cap then under-counts until the next successful write). No-ops for dev
    callers (no tenant) and zero-cost calls."""
    if "/" not in caller:
        return
    tenant_id = caller.split("/", 1)[0]
    cost = estimate_cost_usd(model, usage)
    if cost <= 0:
        return
    try:
        import psycopg

        from service_auth import _gateway_dsn

        with psycopg.connect(**_gateway_dsn(), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO tenant_usage (tenant_id, day, spend_usd, request_count)"
                " VALUES (%s, %s, %s, 1)"
                " ON CONFLICT (tenant_id, day) DO UPDATE SET"
                "   spend_usd = tenant_usage.spend_usd + EXCLUDED.spend_usd,"
                "   request_count = tenant_usage.request_count + 1,"
                "   updated_at = now()",
                (tenant_id, _today(), cost),
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; never break the response
        log.warning("usage recording failed for tenant %s: %s", tenant_id, exc)
