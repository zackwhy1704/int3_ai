"""Runtime settings, read once from the environment.

The API process gets only what it needs at runtime: the control-plane role's
password and the key that derives each tenant role's password. The Postgres
superuser credentials are given only to the one-shot `init` and `tools`
containers (see app/tenants/admin.py); nothing in this module reads them.
"""

import os

ENV = os.environ.get("ENV", "development")
APP_ORIGIN = os.environ.get("APP_ORIGIN", "http://localhost:5173")

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))

# Phase 2 capture: per-user cap on propose_claim calls per hour (0 disables).
PROPOSAL_RATE_PER_HOUR = int(os.environ.get("PROPOSAL_RATE_PER_HOUR", "30"))
CONTROL_DB = "control"
CONTROL_ROLE = "app_control"
CONTROL_PASSWORD = os.environ.get("APP_CONTROL_PASSWORD", "")
TENANT_DB_KEY = os.environ.get("TENANT_DB_KEY", "")

# Least-privilege role for the GATEWAY's token verification: SELECT on
# service_tokens only (no mint, no session access). The gateway is the tier
# nearest the per-user container; with app_control it could mint a token for
# any user and read their brain. admin.init() creates this role and its grant.
GATEWAY_DB_ROLE = "gateway_auth"
GATEWAY_DB_PASSWORD = os.environ.get("GATEWAY_DB_PASSWORD", "")

SESSION_IDLE_MINUTES = 30
SESSION_ABSOLUTE_HOURS = 8
SESSION_COOKIE = "__Host-session"
LOGIN_COOKIE = "__Host-login"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")

# Local test identity provider (navikt/mock-oauth2-server). Development and tests only:
# startup refuses to run with it configured when ENV=production.
MOCK_OIDC_URL = os.environ.get("MOCK_OIDC_URL", "")  # as the backend reaches it
MOCK_OIDC_PUBLIC_URL = os.environ.get(
    "MOCK_OIDC_PUBLIC_URL", ""
)  # as the browser reaches it
