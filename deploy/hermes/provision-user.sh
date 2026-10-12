#!/usr/bin/env bash
# Provision ONE user's thin-client backend: an MCP sidecar (that user's brain
# token) + a gated Hermes backend (that user's gateway token). Run after the
# base stack (docker-compose.yml) is up.
#
#   ./provision-user.sh <tenant> <user> [ingress-port]
#   ./provision-user.sh brindlewood marcus 9131
#
# Identity model (the whole point):
#   * BRAIN token  -> only the user's MCP sidecar. The brain scopes every answer
#     to this user's memberships. This token is NEVER put in the Hermes backend
#     container (asserted below) — so a compromised Hermes backend cannot read
#     the brain as the user.
#   * GATEWAY token -> the user's Hermes config (model.api_key). Distinct value
#     from the brain token. It can only spend this user's budget through the
#     gateway, which pins the model and injects the tenant key.
#
# Secrets: tokens are minted here and written only to the user's sidecar env,
# the user's Hermes config, and the connect file. Nothing is committed.
set -euo pipefail

TENANT="${1:?usage: provision-user.sh <tenant> <user> [ingress-port]}"
USER_ID="${2:?user id required}"
INGRESS_PORT="${3:-9131}"

TENANT_NET=hermes-deploy-tenant
EXT_NET=hermes-deploy-ext
DB=hermes-deploy-db-1
GATEWAY_IMAGE="${GATEWAY_IMAGE:-hermes-deploy-gateway}"
MCP_IMAGE="${MCP_IMAGE:-hermes-deploy-brain-mcp}"
GATED_IMAGE="${GATED_IMAGE:-hermes-gated}"

HERE="$(cd "$(dirname "$0")" && pwd)"
CONNECT_DIR="${CONNECT_DIR:-$HERE/connect}"
mkdir -p "$CONNECT_DIR"

: "${ADMIN_DB_PASSWORD:?set ADMIN_DB_PASSWORD (same as .env)}"
: "${TENANT_KEY_KEK:?set TENANT_KEY_KEK (same as .env)}"
PROVIDER_KEY="${PROVIDER_KEY:-${ANTHROPIC_API_KEY:?set PROVIDER_KEY or ANTHROPIC_API_KEY}}"
ADMIN_DB_USER="${ADMIN_DB_USER:-brain}"
MODEL="${ANTHROPIC_MODEL:-claude-sonnet-5-5}"

mcp_name="mcp-$TENANT-$USER_ID"       # also its network alias the Hermes config points at
hermes_name="hermes-$TENANT-$USER_ID"
vol="hermes-home-$TENANT-$USER_ID"

say() { echo "[provision] $*" >&2; }

# --- 0. tenant provider config (idempotent): allowlist = sonnet ONLY ----------
# key_owner=ours so the gateway injects this key; a daily cap is mandatory then.
say "ensuring tenant provider for $TENANT (allowlist: $MODEL only)"
docker run --rm --network "$EXT_NET" \
  -e ADMIN_DB_USER="$ADMIN_DB_USER" -e ADMIN_DB_PASSWORD="$ADMIN_DB_PASSWORD" \
  -e CONTROL_DB=control -e DB_HOST=db -e TENANT_KEY_KEK="$TENANT_KEY_KEK" \
  "$GATEWAY_IMAGE" python admin_keys.py set-provider \
    --tenant "$TENANT" --provider anthropic \
    --base-url https://api.anthropic.com/v1 --key-owner ours \
    --models "$MODEL" --cap-usd "${DAILY_CAP_USD:-5}" >/dev/null
docker run --rm --network "$EXT_NET" \
  -e ADMIN_DB_USER="$ADMIN_DB_USER" -e ADMIN_DB_PASSWORD="$ADMIN_DB_PASSWORD" \
  -e CONTROL_DB=control -e DB_HOST=db -e TENANT_KEY_KEK="$TENANT_KEY_KEK" \
  -e KEY="$PROVIDER_KEY" \
  "$GATEWAY_IMAGE" python admin_keys.py set-key \
    --tenant "$TENANT" --provider anthropic >/dev/null

# --- 1. mint two DISTINCT service tokens (brain + gateway), store only hashes --
mint() {  # echoes the plaintext token; stores its sha256 in control.service_tokens
  local label="$1" tok sha
  tok="cb_svc_$(openssl rand -hex 24)"
  sha="$(printf '%s' "$tok" | sha256sum | cut -d' ' -f1)"
  docker exec -e PGPASSWORD="$ADMIN_DB_PASSWORD" "$DB" \
    psql -U "$ADMIN_DB_USER" -d control -qtAc \
    "INSERT INTO service_tokens(token_hash,tenant_id,user_id,label)
     VALUES ('$sha','$TENANT','$USER_ID','$label')
     ON CONFLICT DO NOTHING;" >/dev/null
  printf '%s' "$tok"
}
say "minting brain + gateway tokens for $TENANT/$USER_ID"
BRAIN_TOKEN="$(mint "mcp:$hermes_name")"
GATEWAY_TOKEN="$(mint "gw:$hermes_name")"

# --- 2. the user's MCP sidecar: holds the BRAIN token, internal network only ---
say "starting MCP sidecar $mcp_name"
docker rm -f "$mcp_name" >/dev/null 2>&1 || true
docker run -d --name "$mcp_name" --network "$TENANT_NET" --network-alias "$mcp_name" \
  -e BRAIN_URL=http://brain:8000 -e BRAIN_SERVICE_TOKEN="$BRAIN_TOKEN" \
  "$MCP_IMAGE" >/dev/null

# --- 3. the user's gated Hermes backend: holds the GATEWAY token, NOT the brain one
docker volume inspect "$vol" >/dev/null 2>&1 || docker volume create "$vol" >/dev/null
# Write the Hermes config into the volume (model -> gateway, mcp -> this sidecar).
cfg="$(mktemp)"
cat > "$cfg" <<YAML
model:
  default: "$MODEL"
  provider: "custom"
  base_url: "http://brain-gateway:8001/v1"
  api_key: "$GATEWAY_TOKEN"
mcp_servers:
  brain:
    url: "http://$mcp_name:9000/mcp"
YAML
# Write it via stdin (portable: a single-file bind mount mangles on some hosts).
docker run --rm -i -v "$vol:/opt/data" busybox \
  sh -c 'cat > /opt/data/config.yaml; chown 1000:1000 /opt/data/config.yaml 2>/dev/null || true' < "$cfg"
rm -f "$cfg"

INGRESS_TOKEN="$(openssl rand -hex 24)"   # the desktop's session token (not a brain/gateway token)
say "starting gated Hermes backend $hermes_name (ingress 127.0.0.1:$INGRESS_PORT)"
docker rm -f "$hermes_name" >/dev/null 2>&1 || true
docker run -d --name "$hermes_name" --init --network "$TENANT_NET" \
  --entrypoint /opt/proxy/startup-gate.sh \
  -p "127.0.0.1:$INGRESS_PORT:9122" \
  -e HERMES_DASHBOARD_SESSION_TOKEN="$INGRESS_TOKEN" \
  -e HERMES_HOME=/opt/data \
  -e HERMES_DASHBOARD_FILES_ROOT=/opt/data \
  -e HERMES_WRITE_SAFE_ROOT=/opt/data \
  -e HTTPS_PROXY=http://edge:3128 -e https_proxy=http://edge:3128 \
  -e HTTP_PROXY=http://edge:3128  -e http_proxy=http://edge:3128 \
  -e NO_PROXY="localhost,127.0.0.1,brain-gateway,brain,$mcp_name" \
  -e no_proxy="localhost,127.0.0.1,brain-gateway,brain,$mcp_name" \
  -v "$vol:/opt/data" \
  "$GATED_IMAGE" serve --host 127.0.0.1 --port 9119 >/dev/null

# --- 4. ASSERT the brain token is not reachable from the Hermes container ------
say "asserting the brain token is absent from $hermes_name (env + config + home)"
if docker exec "$hermes_name" sh -c "cat /proc/1/environ 2>/dev/null | tr '\0' '\n'; cat /opt/data/config.yaml 2>/dev/null" \
     | grep -qF "$BRAIN_TOKEN"; then
  echo "[provision] FATAL: brain token found inside the Hermes container" >&2
  exit 1
fi
say "ok: brain token is only in the sidecar $mcp_name"

# --- 5. connect file for stock Hermes Desktop ---------------------------------
connect="$CONNECT_DIR/$TENANT-$USER_ID.connect.json"
cat > "$connect" <<JSON
{
  "name": "Company Brain — $USER_ID@$TENANT",
  "url": "http://127.0.0.1:$INGRESS_PORT",
  "session_token": "$INGRESS_TOKEN",
  "note": "Synthetic pilot. Open stock Hermes Desktop, add this remote backend, paste the session token."
}
JSON
say "connect file written: $connect"
echo "$connect"
