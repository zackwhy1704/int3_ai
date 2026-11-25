# Sourced by the VM scripts. Loads the hosted settings, the secrets, and the routes table.
# Never echo anything from /run/int3/stack.env.
GCP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$GCP_ROOT/../.." && pwd)"
. "$GCP_ROOT/config.env"
export DEPLOY_PROJECT
SECRETS=/run/int3/stack.env
ROUTES=/var/lib/int3/routes.tsv     # <random-id> <local-port> <kind> <label>; not a secret, not public
# The public URLs compose.hosted.yml requires (:?), written by stack-up.sh on every start so compose() works
# outside it too (RUNBOOK restore, `compose ps`, `compose logs`). Not secret; 0600 root anyway.
URLS_ENV=/var/lib/int3/local-urls.env
URL_VARS=(APP_ORIGIN KEY_INTAKE_BASE_URL LOCAL_MCP_URL LOCAL_MEMORY_URL)

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

# write_urls_env [file]: the URL_VARS from the environment (stack-up.sh just derived them) into $URLS_ENV,
# atomically and 0600. Refuses an empty one, so a broken start never records a blank URL.
write_urls_env() {
  local f="${1:-$URLS_ENV}" v tmp
  for v in "${URL_VARS[@]}"; do
    [ -n "${!v:-}" ] || { echo "write_urls_env: $v is empty; not writing $f" >&2; return 1; }
  done
  tmp="$(mktemp "$f.XXXXXX")" && chmod 600 "$tmp" || return 1
  for v in "${URL_VARS[@]}"; do printf '%s=%s\n' "$v" "${!v}"; done > "$tmp"
  mv -f "$tmp" "$f"
}

# Every compose call on the VM. key-intake and local-mode are profiles in the base file; the hosted stack
# runs both. $URLS_ENV, when present, supplies the public URLs; anything exported in the shell (stack-up.sh)
# wins over it. Without the file and without exports, compose fails on the first missing URL with the
# hint from compose.hosted.yml, so a first boot still cannot start with missing values.
#
# The two launcher edges read their allowlist once, at start, from a bind-mounted file, and compose does not
# notice a changed file. So every call passes each allowlist's sha256 as a label (compose.hosted.yml): a
# changed allowlist changes the label, and `compose up` recreates exactly that edge.
compose() {
  local envf=() g="$REPO_ROOT/deploy/hermes/gated"
  [ -r "$URLS_ENV" ] && envf=(--env-file "$URLS_ENV")
  (cd "$REPO_ROOT" &&
    LAUNCHER_EDGE_ALLOWLIST_SHA256="$(sha256sum < "$g/launcher-edge.allowlist" | cut -c1-64)" \
    LAUNCHER_EDGE_LOCAL_ALLOWLIST_SHA256="$(sha256sum < "$g/launcher-edge-local.allowlist" | cut -c1-64)" \
    docker compose "${envf[@]}" --profile key-intake --profile local-mode \
      -f deploy/hermes/docker-compose.yml -f deploy/gcp/compose.hosted.yml "$@")
}

render_caddy() {
  mkdir -p /var/lib/int3/caddy
  python3 "$GCP_ROOT/caddy/render_caddy.py" --routes "$ROUTES" --base "$INT3_BASE_DOMAIN" \
    --issuer "${1:-production}" --out /var/lib/int3/caddy/Caddyfile
}
