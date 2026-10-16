#!/usr/bin/env bash
# `make demo`: anyone with repo access runs ONE command and watches the REAL stock
# Hermes agent run the Company Brain demo over /api/ws. Isolated DEPLOY_PROJECT=hermes-demo
# (never touches hermes-deploy). Leaves the stack UP so a tester can open the desktop app;
# `make demo-down` removes it. Exits non-zero on any failure.
set -euo pipefail
export MSYS_NO_PATHCONV=1
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"; cd "$ROOT"
export DEPLOY_PROJECT="${DEPLOY_PROJECT:-hermes-demo}"
[ "$DEPLOY_PROJECT" != "hermes-deploy" ] || { echo "refusing to run demo as the default project"; exit 2; }
ENVF="deploy/hermes/.env"
CONNECT_DIR="$ROOT/deploy/hermes/connect/$DEPLOY_PROJECT"; export CONNECT_DIR
# The gateway daily cap is PER-TENANT (gateway/admin_keys.py: daily_cap_usd keyed on tenant), shared
# by all 3 users and the gateway DB is fresh per run. Default $3: a deliberately TIGHT budget so a
# run cannot quietly cost a lot. CI lowers per-turn cost further with HERMES_REASONING_EFFORT. If a
# legitimate full run needs more, RAISE this deliberately (owner's call) — do not bump it silently.
CAP="${DEMO_CAP_USD:-3}"
PORTS=("marcus:9331" "priya:9332" "ada:9333")   # off deploy(9131)/e2e(9231)
COMPOSE=(docker compose -f deploy/hermes/docker-compose.yml)

die() { echo ""; echo "PREREQUISITE FAILED: $1"; echo "  fix: $2"; exit 2; }

echo "== demo: checking prerequisites =="
docker info >/dev/null 2>&1 || die "Docker is not running." "Start Docker Desktop (Win/Mac) or 'sudo systemctl start docker' (Linux)."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 not found." "Install Compose v2 (Docker Desktop ships it; Linux: 'sudo apt-get install docker-compose-plugin')."
# >= 8 GB free memory (best-effort; Linux/CI).
if command -v free >/dev/null 2>&1; then
  freemb="$(free -m | awk '/^Mem:/{print $7?$7:$4}')"
  [ "${freemb:-0}" -ge 8000 ] || die "Less than ~8 GB memory available (${freemb} MB)." "Close other apps or use a machine/runner with >= 8 GB free; the stack runs a local embedding model + Postgres."
fi
[ -f "$ENVF" ] || die "$ENVF is missing." "cp deploy/hermes/.env.example $ENVF and set ANTHROPIC_API_KEY=sk-ant-... (you edit the file; never paste the key into chat)."
grep -qE '^ANTHROPIC_API_KEY=.+' "$ENVF" || die "ANTHROPIC_API_KEY is not set in $ENVF." "Edit $ENVF and set ANTHROPIC_API_KEY=sk-ant-... yourself."

echo "== demo: generating any missing non-model secrets (only the model key is yours) =="
gen() { # <KEY> <generator>
  grep -qE "^$1=.+" "$ENVF" || { echo "$1=$(eval "$2")" >> "$ENVF"; echo "  generated $1"; }
}
grep -qE '^ADMIN_DB_USER=' "$ENVF" || echo "ADMIN_DB_USER=brain" >> "$ENVF"
gen ADMIN_DB_PASSWORD   "openssl rand -hex 32"
gen APP_CONTROL_PASSWORD "openssl rand -hex 32"
gen TENANT_DB_KEY       "openssl rand -hex 32"
gen GATEWAY_DB_PASSWORD "openssl rand -hex 32"
gen TENANT_KEY_KEK      "openssl rand -base64 32"
grep -qE '^ANTHROPIC_MODEL=' "$ENVF" || echo "ANTHROPIC_MODEL=claude-sonnet-5-5" >> "$ENVF"
grep -qE '^ENV=' "$ENVF" || echo "ENV=development" >> "$ENVF"
grep -qE '^EGRESS_ALLOW=' "$ENVF" || echo "EGRESS_ALLOW=" >> "$ENVF"
set -a; . "$ENVF"; set +a

teardown() { DEPLOY_PROJECT="$DEPLOY_PROJECT" bash deploy/hermes/e2e/teardown.sh || true; }

echo "== demo: build shared images (if missing) =="
docker image inspect hermes-base-pinned      >/dev/null 2>&1 || bash deploy/hermes/build-base.sh
docker image inspect hermes-gated            >/dev/null 2>&1 || docker build -t hermes-gated --build-arg BASE=hermes-base-pinned -f deploy/hermes/gated/Dockerfile.gated deploy/hermes/gated
docker image inspect hermes-deploy-brain-mcp >/dev/null 2>&1 || docker build -t hermes-deploy-brain-mcp deploy/hermes/brain-mcp

echo "== demo: fresh isolated stack ($DEPLOY_PROJECT) =="
teardown
mkdir -p "$CONNECT_DIR"
"${COMPOSE[@]}" up -d --build
for _ in $(seq 1 90); do
  [ "$(docker inspect -f '{{.State.Status}}' "${DEPLOY_PROJECT}-init-1" 2>/dev/null || echo none)" = exited ] && break; sleep 2
done
docker logs "${DEPLOY_PROJECT}-init-1" 2>&1 | grep -iE "provisioned|already" | tail -1 || true

echo "== demo: provision marcus/priya/ada (key-owner ours, \$${CAP} cap) =="
for up in "${PORTS[@]}"; do
  bash deploy/hermes/provision-user.sh brindlewood "${up%%:*}" "${up##*:}" --key-owner ours --cap-usd "$CAP"
done

echo "== demo: run the scenario through the REAL Hermes agent (/api/ws) =="
set +e
DEPLOY_PROJECT="$DEPLOY_PROJECT" CONNECT_DIR="$CONNECT_DIR" bash deploy/hermes/demo/run_demo.sh
rc=$?
set -e
echo ""
echo "The stack is LEFT UP so you can open the desktop app (connect-desktop.ps1/.sh)."
echo "Remove it with:  make demo-down"
exit $rc
