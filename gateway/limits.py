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

# USD per 1M tokens: (input, output) — or (input, output, cache_read, cache_write) to pin cache
# rates. GUARDRAIL estimates for the daily cap, not billing. Every allowlisted model MUST have an
# entry with a price source — a URL or "UNVERIFIED" when no confirmed public price is on hand.
# Override per deployment with GATEWAY_PRICES, e.g.
#   GATEWAY_PRICES='{"claude-sonnet-5-5":[3,15]}'  or  '{"m":[3,15,0.3,3.75]}'.
# When cache rates are omitted they are DERIVED from the input rate with the Anthropic prompt-cache
# multipliers: cache_read = 0.1x input, cache_write = 1.25x input. These multipliers are the
# published Anthropic ratios; the absolute input/output numbers remain UNVERIFIED placeholders.
_PRICE_SOURCES: dict[str, str] = {
    # MVP allowlist is claude-sonnet-5-5 only. UNVERIFIED: no confirmed public
    # price URL on hand; (3,15)/Mtok is a conservative placeholder. Set a real
    # source before anything an invoice depends on reads this.
    "claude-sonnet-5-5": "UNVERIFIED",
}
_DEFAULT_PRICES: dict[str, tuple[float, ...]] = {
    "claude-sonnet-5-5": (3.0, 15.0),  # UNVERIFIED — cache rates derived (0.1x / 1.25x)
}
_FALLBACK_PRICE = (3.0, 15.0)  # UNVERIFIED — used for any model absent above
_CACHE_READ_MULT = 0.1    # Anthropic prompt-cache read  = 0.1x input
_CACHE_WRITE_MULT = 1.25  # Anthropic prompt-cache write = 1.25x input

# Health: set when a key_owner='ours' spend write fails. A tenant WE pay for then
# runs with an under-counted cap, so GET /health reports it (degraded) rather
# than only logging. Cleared on the next successful write for that tenant.
_spend_health: dict | None = None


def spend_health() -> dict | None:
    """None when healthy, else {tenant, error, at} for the last ours-key spend
    write that failed. Surfaced by the gateway's GET /health."""
    return _spend_health


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


def _rates(model: str) -> tuple[float, float, float, float]:
    """(input, output, cache_read, cache_write) USD/Mtok for a model. A 2-tuple price derives the
    cache rates from the input rate (0.1x read, 1.25x write); a 4-tuple is used verbatim."""
    p = _prices().get(model, _FALLBACK_PRICE)
    pin, pout = p[0], p[1]
    pcr = p[2] if len(p) > 2 else pin * _CACHE_READ_MULT
    pcw = p[3] if len(p) > 3 else pin * _CACHE_WRITE_MULT
    return pin, pout, pcr, pcw


def estimate_cost_usd(model: str, usage: dict) -> float:
    """Rough USD for one call from normalised, DISJOINT usage
    ({input,output,cache_read,cache_write}). Cache reads bill at ~0.1x and writes at ~1.25x the
    input rate (Anthropic prompt caching) — NOT the full input rate (that over-billed cache-heavy
    runs ~5x). `normalise_usage` keeps `input` disjoint from the cache counts for both providers
    (routing.py), so nothing is double counted. A guardrail proxy, not an invoice."""
    pin, pout, pcr, pcw = _rates(model)
    u = usage or {}
    return (
        (u.get("input") or 0) / 1_000_000 * pin
        + (u.get("output") or 0) / 1_000_000 * pout
        + (u.get("cache_read") or 0) / 1_000_000 * pcr
        + (u.get("cache_write") or 0) / 1_000_000 * pcw
    )


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


# Check-then-record overshoot bound: check_daily_cap() reads the spend recorded
# BEFORE this request; record_spend() adds this request's cost AFTER it finishes.
# So the cap can be exceeded by at most the total cost of the requests in flight
# when the threshold is crossed — bounded per request by (input + max_tokens
# output) * price. With one request at a time per user, overshoot <= one
# request's cost. This is a guardrail, not a hard ceiling; a hard ceiling would
# need a reserve-before-call debit.
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


def record_spend(caller: str, model: str, usage: dict, key_owner: str = "theirs") -> None:
    """Add one call's estimated cost to the tenant's daily counter. Best-effort:
    a write failure never breaks the LLM response (the cap then under-counts
    until the next successful write). No-ops for dev callers (no tenant) and
    zero-cost calls.

    Failure severity depends on who pays: for key_owner='ours' a failed write
    means a tenant WE pay for is now running with an under-counted cap, so it is
    logged at ERROR and flips the spend health flag (GET /health -> degraded);
    for 'theirs' it is a WARNING (the client owns their own spend)."""
    global _spend_health
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
        if _spend_health and _spend_health.get("tenant") == tenant_id:
            _spend_health = None  # recovered
    except Exception as exc:  # noqa: BLE001 — best-effort; never break the response
        if key_owner == "ours":
            log.error("usage recording FAILED for OURS-key tenant %s: %s — daily cap is "
                      "now UNDER-COUNTING for a tenant we pay for", tenant_id, exc)
            _spend_health = {"tenant": tenant_id, "error": f"{type(exc).__name__}: {exc}",
                             "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        else:
            log.warning("usage recording failed for tenant %s: %s", tenant_id, exc)
