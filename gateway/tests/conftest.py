"""Gateway test fixtures.

The gateway now pins the model from config (LLM_MODEL / ANTHROPIC_MODEL) and
fails closed when neither is set — there is deliberately no hardcoded model
default in production code. The unit tests therefore need a model in the env;
set a deterministic one here (without clobbering a real value if present) so
translate tests stay hermetic.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _pinned_model_env():
    created = "LLM_MODEL" not in os.environ and "ANTHROPIC_MODEL" not in os.environ
    if created:
        os.environ["LLM_MODEL"] = "claude-test-pinned"
    try:
        yield os.environ.get("LLM_MODEL") or os.environ.get("ANTHROPIC_MODEL")
    finally:
        if created:
            os.environ.pop("LLM_MODEL", None)
