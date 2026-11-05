#!/usr/bin/env bash
# On the VM, by the operator, in an interactive SSH session:
#
#   sudo bash /opt/int3/deploy/gcp/provision-partner.sh <name> [--cap-usd N] [--ttl-hours N] [--no-invites] [--reprovision]
#
# Creates a synthetic tenant for one partner (the fictional Brindlewood seed, under the partner's
# name), three users with their own gated backends and random hostnames, and prints one single-use
# invite link per user TO THIS TERMINAL ONLY. It refuses to run if its output is piped, redirected
# or captured, so invites cannot land in a log. Re-running for an existing partner adds nothing and
# just prints fresh invites. --no-invites provisions without minting any, so it needs no terminal.
# --reprovision recreates each existing user's containers on the same hostname, port and data volume,
# which re-encrypts the current Anthropic key into the tenant and picks up new images. Signed-in
# devices stay valid, and open connections drop until they reconnect.
set -euo pipefail
. "$(dirname "$0")/vm/lib.sh"
NAME="${1:?usage: provision-partner.sh <name> [--cap-usd N] [--ttl-hours N]}"; shift
CAP=5 TTL=72 INVITES=1 REPROVISION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --cap-usd) CAP="$2"; shift 2 ;;
    --ttl-hours) TTL="$2"; shift 2 ;;
    --no-invites) INVITES=""; shift ;;
    --reprovision) REPROVISION=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$NAME" =~ ^[a-z][a-z0-9]{1,30}$ ]] || { echo "partner name must be lowercase letters and digits" >&2; exit 2; }
[ -z "$INVITES" ] || [ -t 1 ] || { echo "refusing: invites print to an interactive terminal only" >&2; exit 2; }
load_secrets
[ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "the Anthropic key is not in Secret Manager yet (see RUNBOOK.md)" >&2; exit 2; }
docker ps --format '{{.Names}}' | grep -qx int3-caddy \
  || { echo "Caddy is not running: TLS comes first (enable-tls.sh)" >&2; exit 2; }

BRAIN="$DEPLOY_PROJECT-brain-1"
LAUNCHER_ID="$(awk -F'\t' '$3=="launcher"{print $1; exit}' "$ROUTES")"
admin_env=(-e ADMIN_DB_USER="$ADMIN_DB_USER" -e ADMIN_DB_PASSWORD="$ADMIN_DB_PASSWORD")

if docker exec -e PGPASSWORD="$ADMIN_DB_PASSWORD" "$DEPLOY_PROJECT-db-1" psql -U "$ADMIN_DB_USER" -d control -tAc \
     "SELECT 1 FROM tenants WHERE id = '$NAME'" | grep -q 1; then
  echo "tenant $NAME exists"
else
  # identities.email is unique across tenants, so each partner's copy of the seed gets its own domain.
  seed="/tmp/seed-$NAME"
  docker exec "$BRAIN" sh -c "rm -rf $seed && cp -r /app/seed/brindlewood $seed && sed -i 's/@brindlewood\.example/@$NAME.example/' $seed/users.yaml"
  docker exec "${admin_env[@]}" "$BRAIN" python -m app.tenants.admin provision --id "$NAME" \
    --name "$NAME (synthetic demo data)" --seed "$seed" >/dev/null
  docker exec "$BRAIN" rm -rf "$seed"
  echo "tenant $NAME created from the synthetic seed"
fi

for user in priya marcus ada; do
  label="$NAME/$user"
  if grep -qP "\t\Q$label\E\$" "$ROUTES" && [ -z "$REPROVISION" ]; then
    echo "$label: backend exists"
    continue
  fi
  port="$(awk -F'\t' -v l="$label" '$4==l{print $2; exit}' "$ROUTES")"
  [ -n "$port" ] || port="$(next_port)"
  id="$(route_id user "$port" "$label")"
  BACKEND_PUBLIC_URL="$(public_url "$id")" CONTAINER_RESTART=unless-stopped INGRESS_MODE=launcher \
    PROVIDER_KEY="$ANTHROPIC_API_KEY" GATEWAY_IMAGE="$DEPLOY_PROJECT-gateway" \
    bash "$REPO_ROOT/deploy/hermes/provision-user.sh" "$NAME" "$user" "$port" \
      --key-owner ours --cap-usd "$CAP" 2>&1 | grep -v 'invite:' | tail -1
done

render_caddy production
docker exec int3-caddy caddy reload --config /etc/caddy/Caddyfile

[ -n "$INVITES" ] || { echo "provisioned $NAME; no invites minted"; exit 0; }
echo
echo "Invite links for $NAME: single use, ${TTL}h. Send each one privately to that person."
for user in priya marcus ada; do
  docker exec "${admin_env[@]}" "$BRAIN" python -m app.launcher.admin invite --tenant "$NAME" --user "$user" \
    --ttl-hours "$TTL" --server-url "$(public_url "$LAUNCHER_ID")" | tail -1 | sed "s/^ */$user:  /"
done
