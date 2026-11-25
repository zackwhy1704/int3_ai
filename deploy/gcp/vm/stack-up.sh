#!/usr/bin/env bash
# int3-stack.service: bring the shared stack up on the VM. Safe on every boot. Does not touch
# Caddy: certificates are requested only by enable-tls.sh, after the owner confirms DNS.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
bash "$GCP_ROOT/vm/fetch-secrets.sh"
load_secrets

# The fixed services get random hostnames as well, created once and stable after that (route_id reuses
# the row with the same label; test_routes.sh): the hosted launcher edge, key intake, and local mode's
# own launcher edge and brain MCP endpoint (docs/LOCAL_MODE_DEPLOY_PLAN.md step 1).
LAUNCHER_URL="$(public_url "$(route_id launcher 8100 launcher-edge)")"
KEYS_URL="$(public_url "$(route_id keys 8099 key-intake)")"
LOCAL_URL="$(public_url "$(route_id local 8101 launcher-edge-local)")"
MCP_URL="$(public_url "$(route_id mcp 8200 brain-mcp-local)")"
export LAUNCHER_PUBLIC_URL="$LAUNCHER_URL" KEY_INTAKE_BASE_URL="$KEYS_URL" APP_ORIGIN="$LAUNCHER_URL"
# What the brain hands local-mode launchers: the MCP endpoint and the memory API (on the local edge).
export LOCAL_MCP_URL="$MCP_URL/mcp" LOCAL_MEMORY_URL="$LOCAL_URL"

cd "$REPO_ROOT"
. deploy/hermes/images.sh
ensure_images
compose up -d --build db brain gateway edge launcher-edge key-intake launcher-edge-local brain-mcp-local
healthz() {  # healthz <port> <name>: wait for the edge on that loopback port to answer 200
  local code=""
  for _ in $(seq 1 90); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$1/launcher/healthz" || true)"
    [ "$code" = 200 ] && return 0
    sleep 5
  done
  echo "[stack] FAILED: $2 healthz answered ${code:-nothing}" >&2; return 1
}
healthz 8100 "launcher edge" || exit 1
healthz 8101 "local launcher edge" || exit 1

# A recreated gateway or edge loses its per-user network attachments; put them back.
for net in $(docker network ls --format '{{.Name}}' | grep "^u-$DEPLOY_PROJECT-" || true); do
  docker network connect --alias brain-gateway "$net" "$DEPLOY_PROJECT-gateway-1" 2>/dev/null || true
  docker network connect --alias edge "$net" "$DEPLOY_PROJECT-edge-1" 2>/dev/null || true
done
echo "[stack] up; launcher edge and local launcher edge healthz 200"
