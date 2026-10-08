"""
Gateway auth tests — production guard and GATEWAY_AUTH=none behavior.

Security invariants tested:
  test_gateway_auth_none_rejected_in_production — GATEWAY_AUTH=none must cause startup
      failure when ENV=production. This is a hard invariant: the gateway must never
      pass bearer tokens unverified in production.

FAIL-WITHOUT-FIX evidence (S2):
  If the guard in main.py lifespan is removed (the block starting with
  `if ENV == "production" and GATEWAY_AUTH == "none": raise RuntimeError(...)`),
  this test fails with: did not raise RuntimeError.
  The production guard is at gateway/main.py lines 40-44.
  fail-without-fix evidence requires no Docker — the lifespan is called directly.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest


def test_gateway_auth_none_rejected_in_production():
    """GATEWAY_AUTH=none must cause startup failure when ENV=production.

    Invariant: gateway never passes bearer tokens unverified in production.
    The guard is in gateway/main.py lifespan (S2).

    FAIL-WITHOUT-FIX: without the guard at main.py:40-44, this test raises
    AssertionError('did not raise') — proving the guard is load-bearing.
    """
    # Import main inside the test so the env patch is active at import time
    # (main.py reads ENV at module level, so we must reload or call lifespan directly).
    # We call the lifespan directly to avoid full app startup.
    import sys

    # Remove cached module so env changes take effect.
    for mod in list(sys.modules.keys()):
        if mod in ("main", "gateway.main"):
            del sys.modules[mod]

    with patch.dict(os.environ, {"GATEWAY_AUTH": "none", "ENV": "production"}):
        import importlib
        import sys as _sys

        # Ensure gateway directory is on the path so `import main` works.
        gateway_dir = os.path.join(os.path.dirname(__file__), "..")
        gateway_dir = os.path.abspath(gateway_dir)
        if gateway_dir not in _sys.path:
            _sys.path.insert(0, gateway_dir)

        # Clear any cached main module.
        for mod in list(_sys.modules.keys()):
            if mod == "main":
                del _sys.modules[mod]

        # Re-import main with patched env so GATEWAY_AUTH and ENV are set at module level.
        import main as gw_main
        importlib.reload(gw_main)

        # The lifespan is an async context manager. We trigger it with a fake app.
        import asyncio

        async def _trigger():
            # Re-read the guard values after reload.
            async with gw_main.lifespan(gw_main.app):
                pass

        with pytest.raises(RuntimeError, match="not allowed"):
            asyncio.run(_trigger())
