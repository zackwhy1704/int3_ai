"""authcore — shared RS256 JWT verifier for Company Brain services.

Exports:
  verify_sync(token, audience, jwks_url, allowed_issuers) -> dict
  verify_async(token, audience, jwks_url, allowed_issuers) -> dict
  AuthCoreError — raised on any verification failure

JWT library: authlib only. python-jose is not used (D3).
"""
from .verifier import AuthCoreError, verify_async, verify_sync

__all__ = ["verify_sync", "verify_async", "AuthCoreError"]
