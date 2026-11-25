#!/usr/bin/env bash
# Tests for lib.sh compose() + write_urls_env (compose.hosted.yml requires the public URLs with :?).
# Runs the REAL lib.sh and `docker compose config` (no containers) on a temp copy of the two compose files,
# so no stray .env is read. The DB secrets load_secrets would provide are dummies here.
# Run from anywhere:  bash deploy/gcp/vm/test_compose_env.sh
set -uo pipefail
unset MSYS_NO_PATHCONV
HERE="$(cd "$(dirname "$0")" && pwd)"; SRC="$(cd "$HERE/../../.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail=0; pass=0
ok()  { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }

. "$HERE/lib.sh"
REPO_ROOT="$T/repo"; URLS_ENV="$T/local-urls.env"           # the copy, never /var/lib/int3
mkdir -p "$REPO_ROOT/deploy/hermes/gated" "$REPO_ROOT/deploy/gcp"
cp "$SRC/deploy/hermes/docker-compose.yml" "$REPO_ROOT/deploy/hermes/"
cp "$SRC"/deploy/hermes/gated/*.allowlist "$REPO_ROOT/deploy/hermes/gated/"   # compose() hashes them
cp "$SRC/deploy/gcp/compose.hosted.yml" "$REPO_ROOT/deploy/gcp/"
# What load_secrets puts in the environment on the VM (dummies; compose config only interpolates them).
export ADMIN_DB_PASSWORD=t APP_CONTROL_PASSWORD=t TENANT_DB_KEY=t GATEWAY_DB_PASSWORD=t TENANT_KEY_KEK=t
unset "${URL_VARS[@]}"
brain_env() { compose config 2>/dev/null | awk '/^  brain:/{b=1} b&&/^  [a-z]/&&!/^  brain:/{b=0} b' | grep -E "^ +$1: " | sed 's/.*: //'; }

# 1. no file, nothing exported: refused, and the message says where the values come from
out="$(compose config -q 2>&1)"; c=$?
[ "$c" != 0 ] && grep -q "required variable" <<<"$out" && grep -q "local-urls.env" <<<"$out" \
  && ok "no file, no exports -> compose refuses, naming local-urls.env" || bad "no file (exit $c): $out"

# 2. write_urls_env refuses an empty URL and writes nothing
export APP_ORIGIN=https://aaaa1111.app.example KEY_INTAKE_BASE_URL=https://bbbb2222.app.example LOCAL_MCP_URL=
write_urls_env 2>/dev/null; c=$?
[ "$c" != 0 ] && [ ! -e "$URLS_ENV" ] && ok "an empty URL -> write_urls_env refuses, no file" || bad "empty URL written (exit $c)"

# 3. stack-up.sh's values -> the file: four lines, 0600, atomic (no temp left behind)
export LOCAL_MCP_URL=https://cccc3333.app.example/mcp LOCAL_MEMORY_URL=https://dddd4444.app.example
write_urls_env && [ "$(wc -l < "$URLS_ENV" | tr -d ' ')" = 4 ] && [ -z "$(ls "$T" | grep 'local-urls.env\.')" ] \
  && ok "write_urls_env writes the four URLs" || bad "write_urls_env"
if [ "$(uname -s | cut -c1-5)" = MINGW ]; then ok "mode 0600 (not checkable on Windows; chmod 600 in write_urls_env)"
else [ "$(stat -c %a "$URLS_ENV")" = 600 ] && ok "file mode 0600" || bad "mode $(stat -c %a "$URLS_ENV")"; fi

# 4. an empty environment plus the file: compose works (restore `compose up -d db`, `compose ps`, logs)
unset "${URL_VARS[@]}"
compose config -q 2>/dev/null && ok "empty environment + file -> compose config accepted" || bad "file not used: $(compose config -q 2>&1 | head -2)"
[ "$(brain_env LOCAL_MCP_URL)" = https://cccc3333.app.example/mcp ] && [ "$(brain_env LOCAL_MEMORY_URL)" = https://dddd4444.app.example ] \
  && ok "brain gets the URLs from the file" || bad "brain env: $(brain_env LOCAL_MCP_URL)"
compose config --services 2>/dev/null | grep -qx db && ok "restore path: compose sees the db service" || bad "no db service"

# 5. stack-up.sh's fresh exports win over a stale file
export LOCAL_MCP_URL=https://eeee5555.app.example/mcp
[ "$(brain_env LOCAL_MCP_URL)" = https://eeee5555.app.example/mcp ] && ok "an exported URL overrides the file" || bad "file overrode the export"
unset LOCAL_MCP_URL

# 6. the file goes away again: refused again (first boot cannot start on missing values)
rm -f "$URLS_ENV"
compose config -q >/dev/null 2>&1 && bad "accepted without file" || ok "file removed -> refused again"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
