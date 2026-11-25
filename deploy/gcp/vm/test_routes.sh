#!/usr/bin/env bash
# Tests for the routes table as stack-up.sh uses it (deploy plan step 1): adding local mode's two hosts
# must leave every existing hostname exactly as it was. Runs the REAL lib.sh route_id / next_port and
# render_caddy.py against a COPY of a routes.tsv shaped like the pilot's (synthetic ids; nothing outside
# a temp dir). Run from anywhere:  bash deploy/gcp/vm/test_routes.sh
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; GCP_SRC="$(cd "$HERE/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail=0; pass=0
ok()  { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }
PY=""  # the first python that really runs (Windows has a python3 Store alias that only prints a hint)
for c in python3 python py; do
  command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0)' >/dev/null 2>&1 && { PY="$c"; break; }
done

. "$HERE/lib.sh"            # route_id, next_port, public_url, INT3_BASE_DOMAIN
ROUTES="$T/routes.tsv"     # a copy, never /var/lib/int3/routes.tsv
printf '%s\n' \
  $'0a0a0a0a0a0a0a0a\t8100\tlauncher\tlauncher-edge' \
  $'0b0b0b0b0b0b0b0b\t8099\tkeys\tkey-intake' \
  $'1111111111111111\t9500\tuser\tzack/priya' \
  $'2222222222222222\t9501\tuser\tzack/marcus' \
  $'3333333333333333\t9502\tuser\tmarcuspilot/ada' > "$ROUTES"
before="$(cat "$ROUTES")"

launcher="$(route_id launcher 8100 launcher-edge)"; keys="$(route_id keys 8099 key-intake)"
[ "$launcher" = 0a0a0a0a0a0a0a0a ] && [ "$keys" = 0b0b0b0b0b0b0b0b ] && ok "existing launcher and keys ids returned unchanged" || bad "existing ids changed ($launcher $keys)"

local_id="$(route_id local 8101 launcher-edge-local)"; mcp_id="$(route_id mcp 8200 brain-mcp-local)"
[[ "$local_id" =~ ^[0-9a-f]{16}$ && "$mcp_id" =~ ^[0-9a-f]{16}$ && "$local_id" != "$mcp_id" ]] && ok "local and mcp get new random 16-hex ids" || bad "new ids malformed ($local_id $mcp_id)"
[ "$(head -n 5 "$ROUTES")" = "$before" ] && [ "$(wc -l < "$ROUTES" | tr -d ' ')" = 7 ] && ok "existing rows untouched; exactly two rows appended" || bad "rows changed: $(cat "$ROUTES")"
grep -qP "^$local_id\t8101\tlocal\tlauncher-edge-local$" "$ROUTES" && grep -qP "^$mcp_id\t8200\tmcp\tbrain-mcp-local$" "$ROUTES" && ok "new rows carry port, kind and label" || bad "new rows wrong"

# A second boot (stack-up.sh runs on every boot): nothing changes.
again="$(cat "$ROUTES")"
[ "$(route_id launcher 8100 launcher-edge)" = 0a0a0a0a0a0a0a0a ] && [ "$(route_id keys 8099 key-intake)" = 0b0b0b0b0b0b0b0b ] \
  && [ "$(route_id local 8101 launcher-edge-local)" = "$local_id" ] && [ "$(route_id mcp 8200 brain-mcp-local)" = "$mcp_id" ] \
  && [ "$(cat "$ROUTES")" = "$again" ] && ok "second run: all four ids stable, file unchanged" || bad "not stable on rerun"

[ "$(next_port)" = 9503 ] && ok "next per-user port unaffected by the new kinds (9503)" || bad "next_port $(next_port)"
[ "$(public_url "$mcp_id")/mcp" = "https://$mcp_id.$INT3_BASE_DOMAIN/mcp" ] && ok "LOCAL_MCP_URL shape: https://<mcp-id>.$INT3_BASE_DOMAIN/mcp" || bad "url shape"

out="$("$PY" "$GCP_SRC/caddy/render_caddy.py" --routes "$(cygpath -w "$ROUTES" 2>/dev/null || echo "$ROUTES")" --base "$INT3_BASE_DOMAIN" \
       --issuer internal --out "$(cygpath -w "$T/Caddyfile" 2>/dev/null || echo "$T/Caddyfile")" 2>&1)"
[ -z "$out" ] && grep -q "https://$local_id.$INT3_BASE_DOMAIN {" "$T/Caddyfile" && grep -q "reverse_proxy 127.0.0.1:8200 {" "$T/Caddyfile" \
  && [ "$(grep -c '^https://' "$T/Caddyfile")" = 7 ] && ok "render_caddy accepts the table: 7 sites incl. local (8101) and mcp (8200)" || bad "render_caddy: $out"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
