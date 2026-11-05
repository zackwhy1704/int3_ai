# Sourced by the VM scripts. Loads the hosted settings, the secrets, and the routes table.
# Never echo anything from /run/int3/stack.env.
GCP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$GCP_ROOT/../.." && pwd)"
. "$GCP_ROOT/config.env"
export DEPLOY_PROJECT
SECRETS=/run/int3/stack.env
ROUTES=/var/lib/int3/routes.tsv     # <random-id> <local-port> <kind> <label>; not a secret, not public

load_secrets() { [ -r "$SECRETS" ] || { echo "$SECRETS missing: is int3-stack.service up?" >&2; return 1; }; set -a; . "$SECRETS"; set +a; }

new_id() { openssl rand -hex 8; }   # the hostname says nothing about the partner or the person

# route_id <kind> <port> <label>: the id for a label, created once and then stable.
route_id() {
  local kind="$1" port="$2" label="$3" id
  touch "$ROUTES"; chmod 600 "$ROUTES"
  id="$(awk -F'\t' -v l="$label" '$4==l{print $1; exit}' "$ROUTES")"
  if [ -z "$id" ]; then
    id="$(new_id)"
    printf '%s\t%s\t%s\t%s\n' "$id" "$port" "$kind" "$label" >> "$ROUTES"
  fi
  printf '%s' "$id"
}
public_url() { printf 'https://%s.%s' "$1" "$INT3_BASE_DOMAIN"; }

next_port() {   # per-user ingress ports, loopback only; Caddy is the only thing in front of them
  local max
  max="$(awk -F'\t' '$3=="user"{print $2}' "$ROUTES" 2>/dev/null | sort -n | tail -1)"
  echo $(( ${max:-9499} + 1 ))
}

compose() {
  (cd "$REPO_ROOT" && docker compose --profile key-intake \
    -f deploy/hermes/docker-compose.yml -f deploy/gcp/compose.hosted.yml "$@")
}

render_caddy() {
  mkdir -p /var/lib/int3/caddy
  python3 "$GCP_ROOT/caddy/render_caddy.py" --routes "$ROUTES" --base "$INT3_BASE_DOMAIN" \
    --issuer "${1:-production}" --out /var/lib/int3/caddy/Caddyfile
}
