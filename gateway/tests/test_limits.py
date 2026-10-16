"""Item e: per-user rate limit + per-tenant daily spend cap (fail-fast 429).

Pure unit tests — the sliding window takes an injected clock and the daily-cap
threshold logic reads spend through a monkeypatchable seam, so no DB/network.
The DB round-trip (record -> read -> cap triggers) is proven live in the item-e
report against a service-mode gateway."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import limits


# --- per-user rate limit ----------------------------------------------------

def test_rate_limit_allows_up_to_limit_then_429(monkeypatch):
    monkeypatch.setattr(limits, "RATE_LIMIT_PER_MIN", 3)
    c = "brindlewood/priya-rl1"
    for i in range(3):
        limits.check_rate_limit(c, now=1000.0 + i)  # 3 within the window: allowed
    with pytest.raises(limits.HTTPException) as ei:
        limits.check_rate_limit(c, now=1002.5)       # 4th within 60s: blocked
    assert ei.value.status_code == 429
    assert ei.value.headers.get("Retry-After")       # client told when to retry


def test_rate_limit_window_slides(monkeypatch):
    monkeypatch.setattr(limits, "RATE_LIMIT_PER_MIN", 2)
    c = "brindlewood/priya-rl2"
    limits.check_rate_limit(c, now=0.0)
    limits.check_rate_limit(c, now=1.0)
    with pytest.raises(limits.HTTPException):
        limits.check_rate_limit(c, now=2.0)
    limits.check_rate_limit(c, now=70.0)             # old hits aged out -> allowed


def test_rate_limit_zero_disables(monkeypatch):
    monkeypatch.setattr(limits, "RATE_LIMIT_PER_MIN", 0)
    for i in range(50):
        limits.check_rate_limit("brindlewood/priya-rl3", now=float(i) * 0.001)  # never raises


# --- cost estimate ----------------------------------------------------------

def test_estimate_cost_prices_input_output_and_cache_at_distinct_rates():
    one_m = 1_000_000
    assert abs(limits.estimate_cost_usd("claude-sonnet-5-5",
              {"input": one_m, "output": 0, "cache_read": 0, "cache_write": 0}) - 3.0) < 1e-9
    assert abs(limits.estimate_cost_usd("claude-sonnet-5-5",
              {"input": 0, "output": one_m, "cache_read": 0, "cache_write": 0}) - 15.0) < 1e-9
    # cache read = 0.1x input (0.3), cache write = 1.25x input (3.75) — NOT the full input rate.
    assert abs(limits.estimate_cost_usd("claude-sonnet-5-5",
              {"input": 0, "output": 0, "cache_read": one_m, "cache_write": 0}) - 0.3) < 1e-9
    assert abs(limits.estimate_cost_usd("claude-sonnet-5-5",
              {"input": 0, "output": 0, "cache_read": 0, "cache_write": one_m}) - 3.75) < 1e-9
    assert abs(limits.estimate_cost_usd("claude-sonnet-5-5",
              {"input": 0, "output": 0, "cache_read": one_m, "cache_write": one_m}) - 4.05) < 1e-9


def test_prices_overridable_via_env_2tuple_derives_cache(monkeypatch):
    monkeypatch.setenv("GATEWAY_PRICES", '{"gpt-mid":[1,2]}')
    # input+output at 1,2; cache derived from input: read 0.1, write 1.25
    assert abs(limits.estimate_cost_usd("gpt-mid",
              {"input": 1_000_000, "output": 1_000_000}) - 3.0) < 1e-9
    assert abs(limits.estimate_cost_usd("gpt-mid",
              {"input": 0, "output": 0, "cache_read": 1_000_000, "cache_write": 1_000_000}) - 1.35) < 1e-9


def test_prices_overridable_via_env_4tuple_pins_cache(monkeypatch):
    monkeypatch.setenv("GATEWAY_PRICES", '{"m4":[3,15,0.3,3.75]}')
    assert abs(limits.estimate_cost_usd("m4",
              {"input": 0, "output": 0, "cache_read": 1_000_000, "cache_write": 1_000_000}) - 4.05) < 1e-9


def test_validation_run_totals_recompute_to_about_46_cents():
    """The 2026-10-16 validation run (GitHub 38846845310) summed across the 3 users. At the full
    input rate this read $2.459 (the bug); priced with cache read 0.1x / write 1.25x it is ~$0.46."""
    usage = {"input": 2934 + 6704 + 18952,          # 28,590
             "output": 368 + 925 + 3102,            # 4,395
             "cache_write": 21364,                   # cold write (marcus); ada/priya reused it
             "cache_read": 42728 + 149548 + 555464}  # 747,740
    cost = limits.estimate_cost_usd("claude-sonnet-5-5", usage)
    assert abs(cost - 0.456) < 0.01, cost
    # and the old full-input-rate miscount reproduces the recorded $2.459, proving the fix's scope
    buggy = sum(usage[k] for k in ("input", "cache_read", "cache_write")) / 1e6 * 3.0 + usage["output"] / 1e6 * 15.0
    assert abs(buggy - 2.459) < 0.01, buggy


# --- per-tenant daily cap ---------------------------------------------------

def test_daily_cap_blocks_at_or_over_and_allows_under(monkeypatch):
    monkeypatch.setattr(limits, "_read_spend", lambda t: 5.0)
    with pytest.raises(limits.HTTPException) as ei:
        limits.check_daily_cap("brindlewood", 5.0)   # spent == cap -> block
    assert ei.value.status_code == 429 and ei.value.headers.get("Retry-After")
    monkeypatch.setattr(limits, "_read_spend", lambda t: 4.99)
    limits.check_daily_cap("brindlewood", 5.0)        # under cap -> allowed


def test_daily_cap_none_never_reads_db(monkeypatch):
    def boom(_t):
        raise AssertionError("must not read the DB when the tenant has no cap")
    monkeypatch.setattr(limits, "_read_spend", boom)
    limits.check_daily_cap("brindlewood", None)       # returns immediately


def test_record_spend_noops_for_dev_caller(monkeypatch):
    # dev/none caller has no tenant: record_spend returns before touching the DB.
    import service_auth
    monkeypatch.setattr(service_auth, "_gateway_dsn",
                        lambda: (_ for _ in ()).throw(AssertionError("no DB for dev caller")))
    limits.record_spend("dev", "claude-sonnet-5-5",
                        {"input": 10, "output": 10, "cache_read": 0, "cache_write": 0})


def test_ours_key_spend_write_failure_sets_health_flag(monkeypatch):
    # A spend-write failure is swallowed (never breaks the response), but for a
    # tenant WE pay for (key_owner='ours') it flips the /health spend flag.
    import service_auth
    monkeypatch.setattr(service_auth, "_gateway_dsn",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    limits._spend_health = None
    usage = {"input": 1000, "output": 1000, "cache_read": 0, "cache_write": 0}

    limits.record_spend("acme/u", "claude-sonnet-5-5", usage, key_owner="theirs")
    assert limits.spend_health() is None          # client owns their spend -> warning only

    limits.record_spend("acme/u", "claude-sonnet-5-5", usage, key_owner="ours")
    h = limits.spend_health()
    assert h and h["tenant"] == "acme" and "db down" in h["error"]
    limits._spend_health = None                   # reset module state for other tests
