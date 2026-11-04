#!/usr/bin/env bash
# COMPONENT TIER (BYPASSES HERMES) runner. The real demo is `make demo` (deploy/hermes/demo/).
# `make e2e` body: ONE command, clean machine -> fresh ISOLATED stack -> provision 3
# users -> run the scenario -> teardown. Isolated by DEPLOY_PROJECT (default hermes-e2e)
# so it never touches a running hermes-deploy stack. Exits non-zero on any failure.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # harmless on Linux/CI; stops Git Bash mangling container paths
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"; cd "$ROOT"
export DEPLOY_PROJECT="${DEPLOY_PROJECT:-hermes-e2e}"
[ "$DEPLOY_PROJECT" != "hermes-deploy" ] || { echo "refusing to run e2e as the default project"; exit 2; }
[ -f deploy/hermes/.env ] && { set -a; . deploy/hermes/.env; set +a; }   # env vars still win in CI
CAP="${E2E_CAP_USD:-1}"                          # low per-run gateway daily spend cap
PORTS=("marcus:9231" "priya:9232" "ada:9233")    # off the deploy's 9131-9133 to avoid host-port clashes
COMPOSE=(docker compose -f deploy/hermes/docker-compose.yml)

# Teardown on exit, unless E2E_NO_TEARDOWN=1 (used by the regression red/green proof,
# which keeps the stack up to re-run the scenario against a neutralised brain).
teardown() { [ "${E2E_NO_TEARDOWN:-0}" = 1 ] && { echo "(E2E_NO_TEARDOWN=1: leaving $DEPLOY_PROJECT up)"; return 0; }
             DEPLOY_PROJECT="$DEPLOY_PROJECT" bash deploy/hermes/e2e/teardown.sh || true; }
trap teardown EXIT

echo "== e2e: shared images (version-safe: deploy/hermes/images.sh) =="
. deploy/hermes/images.sh
ensure_images || { echo "image build/verification failed"; exit 2; }

echo "== e2e: fresh isolated stack ($DEPLOY_PROJECT) =="
teardown
"${COMPOSE[@]}" up -d --build

echo "== e2e: wait for init to provision the tenant =="
for _ in $(seq 1 90); do
  [ "$(docker inspect -f '{{.State.Status}}' "${DEPLOY_PROJECT}-init-1" 2>/dev/null || echo none)" = exited ] && break
  sleep 2
done
docker logs "${DEPLOY_PROJECT}-init-1" 2>&1 | grep -iE "provisioned|already" | tail -1 || true

echo "== e2e: provision 3 users (key-owner ours, \$${CAP} cap) =="
for up in "${PORTS[@]}"; do
  bash deploy/hermes/provision-user.sh brindlewood "${up%%:*}" "${up##*:}" --key-owner ours --cap-usd "$CAP"
done

echo "== e2e: run scenario (fresh volume -> brain already clean = DEMO.md Reset) =="
bash deploy/hermes/e2e/run_e2e.sh
