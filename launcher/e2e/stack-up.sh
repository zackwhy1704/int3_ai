#!/usr/bin/env bash
# Launcher e2e stack: a FRESH isolated project (default hermes-launch) with priya + ada provisioned
# in INGRESS_MODE=launcher (no connect files, no tokens on disk; user_backends rows instead), the
# launcher edge on 127.0.0.1:8100, and one single-use invite link per user written to
# $INVITE_DIR/<user>.invite (mode 600; they are credentials until redeemed, never printed).
# Run from anywhere; uses deploy/hermes/.env like make-demo.sh. Tear down with:
#   DEPLOY_PROJECT=hermes-launch bash deploy/hermes/e2e/teardown.sh
set -euo pipefail
export MSYS_NO_PATHCONV=1
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"; cd "$ROOT"
export DEPLOY_PROJECT="${DEPLOY_PROJECT:-hermes-launch}"
[ "$DEPLOY_PROJECT" != "hermes-deploy" ] || { echo "refusing the default project"; exit 2; }
INVITE_DIR="${INVITE_DIR:?set INVITE_DIR (where the invite links are written)}"
CAP="${DEMO_CAP_USD:-3}"
USERS=("priya:9432" "ada:9433")
ENVF="deploy/hermes/.env"
[ -f "$ENVF" ] || { echo "$ENVF missing: run make demo once (it generates the non-model secrets)"; exit 2; }
set -a; . "$ENVF"; set +a

echo "== launcher e2e: shared images =="
. deploy/hermes/images.sh
ensure_images

echo "== launcher e2e: fresh stack ($DEPLOY_PROJECT) =="
bash deploy/hermes/e2e/teardown.sh || true
docker compose -f deploy/hermes/docker-compose.yml up -d --build
for _ in $(seq 1 90); do
  [ "$(docker inspect -f '{{.State.Status}}' "${DEPLOY_PROJECT}-init-1" 2>/dev/null || echo none)" = exited ] && break; sleep 2
done

echo "== launcher e2e: provision ${USERS[*]} (INGRESS_MODE=launcher, \$${CAP} cap) =="
for up in "${USERS[@]}"; do
  INGRESS_MODE=launcher bash deploy/hermes/provision-user.sh brindlewood "${up%%:*}" "${up##*:}" --key-owner ours --cap-usd "$CAP"
done

echo "== launcher e2e: invites =="
mkdir -p "$INVITE_DIR"
for up in "${USERS[@]}"; do
  u="${up%%:*}"
  docker exec -e ADMIN_DB_USER="${ADMIN_DB_USER:-brain}" -e ADMIN_DB_PASSWORD="$ADMIN_DB_PASSWORD" \
    "${DEPLOY_PROJECT}-brain-1" python -m app.launcher.admin invite --tenant brindlewood --user "$u" --ttl-hours 4 \
    | grep -o 'http[^ ]*/invite#cb_inv_[A-Za-z0-9_-]*' > "$INVITE_DIR/$u.invite"
  chmod 600 "$INVITE_DIR/$u.invite"
  echo "  $u: invite written ($(wc -c < "$INVITE_DIR/$u.invite") bytes)"
done
echo "== launcher edge: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8100/launcher/me) on /launcher/me without a token (401 expected);" \
     "$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8100/launcher/introspect) on /launcher/introspect (403 expected: not on the edge)"
