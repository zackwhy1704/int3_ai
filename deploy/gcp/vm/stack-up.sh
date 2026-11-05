#!/usr/bin/env bash
# int3-stack.service: bring the shared stack up on the VM. Safe on every boot. Does not touch
# Caddy: certificates are requested only by enable-tls.sh, after the owner confirms DNS.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
bash "$GCP_ROOT/vm/fetch-secrets.sh"
load_secrets

# The two fixed services get random hostnames as well, created once.
LAUNCHER_URL="$(public_url "$(route_id launcher 8100 launcher-edge)")"
KEYS_URL="$(public_url "$(route_id keys 8099 key-intake)")"
export LAUNCHER_PUBLIC_URL="$LAUNCHER_URL" KEY_INTAKE_BASE_URL="$KEYS_URL" APP_ORIGIN="$LAUNCHER_URL"

cd "$REPO_ROOT"
. deploy/hermes/images.sh
ensure_images
compose up -d --build db brain gateway edge launcher-edge key-intake
code=""
for _ in $(seq 1 90); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8100/launcher/healthz || true)"
  [ "$code" = 200 ] && break
  sleep 5
done
[ "$code" = 200 ] || { echo "[stack] FAILED: launcher edge healthz answered ${code:-nothing}" >&2; exit 1; }

# A recreated gateway or edge loses its per-user network attachments; put them back.
for net in $(docker network ls --format '{{.Name}}' | grep "^u-$DEPLOY_PROJECT-" || true); do
  docker network connect --alias brain-gateway "$net" "$DEPLOY_PROJECT-gateway-1" 2>/dev/null || true
  docker network connect --alias edge "$net" "$DEPLOY_PROJECT-edge-1" 2>/dev/null || true
done
echo "[stack] up; launcher edge healthz 200"
