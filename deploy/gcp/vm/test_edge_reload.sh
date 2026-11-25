#!/usr/bin/env bash
# The launcher edges load their allowlist once, at start. This proves a changed allowlist reaches the running
# edge through lib.sh compose(): the label carrying the file's sha256 changes, so `compose up` recreates that
# edge (and only that one), and the new rule is live. Runs REAL containers (the two edges only, --no-deps,
# no brain) from a temp copy of the files, on ports 18100/18101, in a throwaway compose project.
# Needs Docker. Run from anywhere:  bash deploy/gcp/vm/test_edge_reload.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; SRC="$(cd "$HERE/../../.." && pwd)"
T="$(mktemp -d)"
fail=0; pass=0
ok()  { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }

. "$HERE/lib.sh"
REPO_ROOT="$T/repo"; URLS_ENV="$T/local-urls.env"
mkdir -p "$REPO_ROOT/deploy/hermes/gated" "$REPO_ROOT/deploy/gcp"
cp "$SRC/deploy/hermes/docker-compose.yml" "$REPO_ROOT/deploy/hermes/"
cp "$SRC"/deploy/hermes/gated/{route_proxy.py,launcher-edge.allowlist,launcher-edge-local.allowlist} "$REPO_ROOT/deploy/hermes/gated/"
cp "$SRC/deploy/gcp/compose.hosted.yml" "$REPO_ROOT/deploy/gcp/"
export DEPLOY_PROJECT=int3edgetest LAUNCHER_EDGE_PORT=18100 LAUNCHER_EDGE_LOCAL_PORT=18101
export ADMIN_DB_PASSWORD=t APP_CONTROL_PASSWORD=t TENANT_DB_KEY=t GATEWAY_DB_PASSWORD=t TENANT_KEY_KEK=t
export APP_ORIGIN=https://a.example KEY_INTAKE_BASE_URL=https://b.example LOCAL_MCP_URL=https://c.example/mcp LOCAL_MEMORY_URL=https://d.example
write_urls_env; unset "${URL_VARS[@]}"
cleanup() { compose down -v --remove-orphans >/dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT

up()   { compose up -d --no-deps launcher-edge launcher-edge-local >/dev/null 2>&1; }
cid()  { docker inspect -f '{{.Id}}' "$DEPLOY_PROJECT-$1-1" 2>/dev/null | cut -c1-12; }
code() {  # code <port> <path>: what the edge answers (no brain here: forwarded = 502, refused = 403)
  local c=""; for _ in $(seq 1 30); do
    c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$1$2")"; [ "$c" != 000 ] && break; sleep 1; done; echo "$c"; }

up
h1="$(cid launcher-edge)"; l1="$(cid launcher-edge-local)"
[ -n "$h1" ] && [ -n "$l1" ] && ok "both edges up from lib.sh compose()" || bad "edges did not start"
[ "$(code 18100 /launcher/local/policy)" = 403 ] && ok "hosted edge refuses /launcher/local/policy (403)" || bad "hosted edge before change"
sha="$(docker inspect -f '{{index .Config.Labels "int3.allowlist.sha256"}}' "$DEPLOY_PROJECT-launcher-edge-1")"
[ "$sha" = "$(sha256sum < "$REPO_ROOT/deploy/hermes/gated/launcher-edge.allowlist" | cut -c1-64)" ] && ok "label = sha256 of the hosted allowlist" || bad "label $sha"

up
[ "$(cid launcher-edge)" = "$h1" ] && [ "$(cid launcher-edge-local)" = "$l1" ] && ok "no change -> nothing recreated" || bad "recreated without a change"

echo "GET  /launcher/local/policy     # test: added to the COPY" >> "$REPO_ROOT/deploy/hermes/gated/launcher-edge.allowlist"
up
h2="$(cid launcher-edge)"
[ -n "$h2" ] && [ "$h2" != "$h1" ] && ok "hosted allowlist changed -> hosted edge recreated" || bad "hosted edge not recreated ($h1 -> $h2)"
[ "$(cid launcher-edge-local)" = "$l1" ] && ok "local edge (allowlist unchanged) not recreated" || bad "local edge recreated"
# Forwarded to the (absent) brain: the proxy answers with its upstream error once its connect attempt fails,
# which can take a while, hence the long timeout here. Refused would be an immediate 403.
after="$(curl -s -o /dev/null -w '%{http_code}' --max-time 90 http://127.0.0.1:18100/launcher/local/policy)"
[[ "$after" =~ ^5[0-9][0-9]$ ]] && ok "the new rule is live: forwarded ($after here, no brain) instead of refused (403)" || bad "new rule not live (got $after)"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
