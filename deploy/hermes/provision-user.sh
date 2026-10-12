#!/usr/bin/env bash
# Provision ONE user's thin-client backend with per-user ISOLATION.
#
#   ./provision-user.sh <tenant> <user> [ingress-port] [--key-owner ours|theirs] [--cap-usd N]
#   ./provision-user.sh brindlewood marcus 9131 --key-owner theirs
#
# Isolation (both layers — the cross-user hole this closes):
#   NETWORK: a per-user internal network u-<tenant>-<user> holds ONLY this user's
#     Hermes backend + MCP sidecar. gateway and edge are connected to it under
#     fixed aliases. The sidecar also joins the brain-only network to reach the
#     brain; no Hermes container joins that network. So a Hermes container cannot
#     reach the brain directly, another user's sidecar, or another user's backend.
#   AUTH: the sidecar demands a per-user X-MCP-Secret (constant-time); it is
#     written only into THIS user's Hermes mcp_servers headers. Wrong/missing -> 401.
#
# Identity: BRAIN token -> only the sidecar (never the Hermes container, asserted);
# GATEWAY token (distinct) -> only the Hermes config. All data is synthetic.
set -euo pipefail

TENANT="${1:?usage: provision-user.sh <tenant> <user> [port] [--key-owner ours|theirs] [--cap-usd N]}"
USER_ID="${2:?user id required}"
shift 2
INGRESS_PORT="9131"; KEY_OWNER="theirs"; CAP_USD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --key-owner) KEY_OWNER="${2:?}"; shift 2 ;;
    --cap-usd)   CAP_USD="${2:?}"; shift 2 ;;
    [0-9]*)      INGRESS_PORT="$1"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Validate ids before they touch SQL, a container name or a network name.
ID_RE='^[a-z0-9_-]+$'
[[ "$TENANT" =~ $ID_RE ]] || { echo "invalid tenant id (want $ID_RE): $TENANT" >&2; exit 2; }
[[ "$USER_ID" =~ $ID_RE ]] || { echo "invalid user id (want $ID_RE): $USER_ID" >&2; exit 2; }
case "$KEY_OWNER" in ours|theirs) ;; *) echo "key-owner must be ours|theirs" >&2; exit 2 ;; esac
[ "$KEY_OWNER" = ours ] && [ -z "$CAP_USD" ] && CAP_USD="5"    # mandatory cap when we pay

EXT_NET=hermes-deploy-ext
BRAIN_NET=hermes-deploy-brain
UNET="u-$TENANT-$USER_ID"
DB=hermes-deploy-db-1
GATEWAY_CTR=hermes-deploy-gateway-1
EDGE_CTR=hermes-deploy-edge-1
GATEWAY_IMAGE="${GATEWAY_IMAGE:-hermes-deploy-gateway}"
MCP_IMAGE="${MCP_IMAGE:-hermes-deploy-brain-mcp}"
GATED_IMAGE="${GATED_IMAGE:-hermes-gated}"

HERE="$(cd "$(dirname "$0")" && pwd)"
CONNECT_DIR="${CONNECT_DIR:-$HERE/connect}"; mkdir -p "$CONNECT_DIR"

: "${ADMIN_DB_PASSWORD:?set ADMIN_DB_PASSWORD (same as .env)}"
: "${TENANT_KEY_KEK:?set TENANT_KEY_KEK (same as .env)}"
PROVIDER_KEY="${PROVIDER_KEY:-${ANTHROPIC_API_KEY:?set PROVIDER_KEY or ANTHROPIC_API_KEY}}"
ADMIN_DB_USER="${ADMIN_DB_USER:-brain}"
MODEL="${ANTHROPIC_MODEL:-claude-sonnet-5-5}"

mcp_name="mcp-$TENANT-$USER_ID"
hermes_name="hermes-$TENANT-$USER_ID"
vol="hermes-home-$TENANT-$USER_ID"
say() { echo "[provision] $*" >&2; }

# --- 0. tenant provider config (idempotent): allowlist = sonnet ONLY ----------
say "tenant provider for $TENANT (key_owner=$KEY_OWNER${CAP_USD:+, cap=\$$CAP_USD}, allowlist: $MODEL)"
CAP_ARG=(); [ -n "$CAP_USD" ] && CAP_ARG=(--cap-usd "$CAP_USD")
docker run --rm --network "$EXT_NET" \
  -e ADMIN_DB_USER="$ADMIN_DB_USER" -e ADMIN_DB_PASSWORD="$ADMIN_DB_PASSWORD" \
  -e CONTROL_DB=control -e DB_HOST=db -e TENANT_KEY_KEK="$TENANT_KEY_KEK" \
  "$GATEWAY_IMAGE" python admin_keys.py set-provider \
    --tenant "$TENANT" --provider anthropic \
    --base-url https://api.anthropic.com/v1 --key-owner "$KEY_OWNER" \
    --models "$MODEL" "${CAP_ARG[@]}" >/dev/null
docker run --rm --network "$EXT_NET" \
  -e ADMIN_DB_USER="$ADMIN_DB_USER" -e ADMIN_DB_PASSWORD="$ADMIN_DB_PASSWORD" \
  -e CONTROL_DB=control -e DB_HOST=db -e TENANT_KEY_KEK="$TENANT_KEY_KEK" \
  -e KEY="$PROVIDER_KEY" \
  "$GATEWAY_IMAGE" python admin_keys.py set-key \
    --tenant "$TENANT" --provider anthropic >/dev/null

# --- 1. mint two DISTINCT service tokens; store only hashes (psql vars, no interp)
mint() {
  local label="$1" tok sha
  tok="cb_svc_$(openssl rand -hex 24)"
  sha="$(printf '%s' "$tok" | sha256sum | cut -d' ' -f1)"
  # psql interpolates :'var' from stdin/-f (not from -c), so feed the SQL on
  # stdin with -v bindings — values are quoted by psql, never shell-interpolated.
  docker exec -i -e PGPASSWORD="$ADMIN_DB_PASSWORD" "$DB" \
    psql -U "$ADMIN_DB_USER" -d control -qtA \
      -v hash="$sha" -v tenant="$TENANT" -v uid="$USER_ID" -v label="$label" >/dev/null <<'SQL'
INSERT INTO service_tokens(token_hash,tenant_id,user_id,label)
VALUES (:'hash',:'tenant',:'uid',:'label') ON CONFLICT DO NOTHING;
SQL
  printf '%s' "$tok"
}
say "minting brain + gateway tokens for $TENANT/$USER_ID"
BRAIN_TOKEN="$(mint "mcp:$hermes_name")"
GATEWAY_TOKEN="$(mint "gw:$hermes_name")"
MCP_SECRET="$(openssl rand -hex 32)"

# --- 2. per-user network; attach gateway + edge under fixed aliases -----------
docker network inspect "$UNET" >/dev/null 2>&1 || docker network create --internal "$UNET" >/dev/null
connect_alias() {  # idempotent connect of a shared container to the user net
  local ctr="$1" alias="$2"
  docker network connect --alias "$alias" "$UNET" "$ctr" 2>/dev/null \
    || true   # already connected
}
connect_alias "$GATEWAY_CTR" brain-gateway
connect_alias "$EDGE_CTR" edge

# --- 3. the user's MCP sidecar: u-net (reach from Hermes) + brain-net (reach brain)
say "starting MCP sidecar $mcp_name (u-net + brain-net, X-MCP-Secret required)"
docker rm -f "$mcp_name" >/dev/null 2>&1 || true
docker run -d --name "$mcp_name" --network "$UNET" \
  -e BRAIN_URL=http://brain:8000 -e BRAIN_SERVICE_TOKEN="$BRAIN_TOKEN" -e MCP_SECRET="$MCP_SECRET" \
  "$MCP_IMAGE" >/dev/null
docker network connect "$BRAIN_NET" "$mcp_name"      # sidecar also reaches the brain

# --- 4. the user's gated Hermes backend: u-net ONLY; GATEWAY token + MCP secret
docker volume inspect "$vol" >/dev/null 2>&1 || docker volume create "$vol" >/dev/null
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
    headers:
      X-MCP-Secret: "$MCP_SECRET"
YAML
docker run --rm -i -v "$vol:/opt/data" busybox \
  sh -c 'cat > /opt/data/config.yaml; chown 1000:1000 /opt/data/config.yaml 2>/dev/null || true' < "$cfg"
rm -f "$cfg"

INGRESS_TOKEN="$(openssl rand -hex 24)"
say "starting gated Hermes backend $hermes_name (u-net only, ingress 127.0.0.1:$INGRESS_PORT)"
docker rm -f "$hermes_name" >/dev/null 2>&1 || true
docker run -d --name "$hermes_name" --init --network "$UNET" \
  --entrypoint /opt/proxy/startup-gate.sh \
  -p "127.0.0.1:$INGRESS_PORT:9122" \
  -e HERMES_DASHBOARD_SESSION_TOKEN="$INGRESS_TOKEN" \
  -e HERMES_HOME=/opt/data -e HERMES_DASHBOARD_FILES_ROOT=/opt/data -e HERMES_WRITE_SAFE_ROOT=/opt/data \
  -e HTTPS_PROXY=http://edge:3128 -e https_proxy=http://edge:3128 \
  -e HTTP_PROXY=http://edge:3128  -e http_proxy=http://edge:3128 \
  -e NO_PROXY="localhost,127.0.0.1,brain-gateway,$mcp_name" \
  -e no_proxy="localhost,127.0.0.1,brain-gateway,$mcp_name" \
  -v "$vol:/opt/data" \
  "$GATED_IMAGE" serve --host 127.0.0.1 --port 9119 >/dev/null

# --- 5. ASSERT the brain token is not reachable from the Hermes container ------
say "asserting the brain token is absent from $hermes_name"
if docker exec "$hermes_name" sh -c "cat /proc/1/environ 2>/dev/null | tr '\0' '\n'; cat /opt/data/config.yaml 2>/dev/null" \
     | grep -qF "$BRAIN_TOKEN"; then
  echo "[provision] FATAL: brain token found inside the Hermes container" >&2; exit 1
fi
say "ok: brain token only in the sidecar $mcp_name"

# --- 6. connect file for stock Hermes Desktop (host-only until Phase 3 TLS) ----
connect="$CONNECT_DIR/$TENANT-$USER_ID.connect.json"
cat > "$connect" <<JSON
{
  "name": "Company Brain — $USER_ID@$TENANT",
  "url": "http://127.0.0.1:$INGRESS_PORT",
  "session_token": "$INGRESS_TOKEN",
  "note": "Synthetic pilot. Works ONLY on this Docker host until hosted TLS ingress (Phase 3)."
}
JSON
say "connect file: $connect"
echo "$connect"
