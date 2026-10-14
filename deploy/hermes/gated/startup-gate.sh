#!/usr/bin/env bash
# Per-user Hermes container startup gate.
#
# This is the container's command. It REFUSES TO STAY UP unless every security
# invariant the pilot depends on is satisfied — asserted by MECHANISM, not by
# reading an env var and trusting it. A failed assertion exits non-zero before
# (or instead of) the backend serving traffic, so Docker reports the container
# as failed. A comment cannot do that; this can.
#
# Checks, in order:
#   STATIC  1. HERMES_HOME, HERMES_DASHBOARD_FILES_ROOT, HERMES_WRITE_SAFE_ROOT
#              all set and byte-equal; WRITE_SAFE_ROOT a SINGLE os.pathsep entry.
#   STATIC  2. config.yaml terminal.backend absent or not "ssh".
#   LIVE    3. route proxy up AND enforcing (an excluded route 403s).
#   LIVE    4. read boundary real: in-boundary path 200, out-of-boundary 403.
#   LIVE    5. egress locked: an allowed host connects, a disallowed one does not.
#
# Usage (as the container command):  startup-gate.sh serve --host 127.0.0.1 --port 9119
set -u

HERMES_BIN=/opt/hermes/.venv/bin/hermes
ROUTE_PROXY=/opt/proxy/route_proxy.py
ALLOWLIST=/opt/proxy/allowlist.txt
PORT_BACKEND=9119
PORT_PROXY=9122
TOKEN="${HERMES_DASHBOARD_SESSION_TOKEN:-}"
# Egress posture (Task J): inference goes through OUR in-network gateway, which
# substitutes the tenant key. The model provider must therefore be UNREACHABLE
# directly from the container — a direct route is an exfil channel (a hook could
# call the provider with an attacker's key). The positive liveness check is the
# gateway, not the provider.
GATE_GATEWAY_HEALTH="${GATE_GATEWAY_HEALTH:-http://brain-gateway:8001/health}"
GATE_PROVIDER_HOST="${GATE_PROVIDER_HOST:-api.anthropic.com}"
GATE_BAD_HOST="${GATE_BAD_HOST:-github.com}"

fatal() { echo "[gate] FATAL: $*" >&2; exit 1; }
ok()    { echo "[gate] ok: $*" >&2; }

# --- STATIC 1: the three roots set, equal, single-entry -----------------------
[ -n "${HERMES_HOME:-}" ] || fatal "HERMES_HOME unset"
[ -n "${HERMES_DASHBOARD_FILES_ROOT:-}" ] || fatal "HERMES_DASHBOARD_FILES_ROOT unset (read boundary would vanish — see web_server_files.py:144-158)"
[ -n "${HERMES_WRITE_SAFE_ROOT:-}" ] || fatal "HERMES_WRITE_SAFE_ROOT unset"
[ "$HERMES_DASHBOARD_FILES_ROOT" = "$HERMES_HOME" ] || fatal "HERMES_DASHBOARD_FILES_ROOT ($HERMES_DASHBOARD_FILES_ROOT) != HERMES_HOME ($HERMES_HOME)"
[ "$HERMES_WRITE_SAFE_ROOT" = "$HERMES_HOME" ] || fatal "HERMES_WRITE_SAFE_ROOT ($HERMES_WRITE_SAFE_ROOT) != HERMES_HOME ($HERMES_HOME)"
case "$HERMES_WRITE_SAFE_ROOT" in
  *:*) fatal "HERMES_WRITE_SAFE_ROOT has multiple os.pathsep entries; must be exactly one" ;;
esac
[ -n "$TOKEN" ] || fatal "HERMES_DASHBOARD_SESSION_TOKEN unset (no non-interactive auth)"
ok "roots set, equal, single-entry: $HERMES_HOME"

# --- STATIC 2: terminal.backend not ssh --------------------------------------
# finding (iii): terminal.backend=ssh makes the fs API bypass the read guard.
CFG="$HERMES_HOME/config.yaml"
if [ -f "$CFG" ]; then
  if grep -Eq '^[[:space:]]*backend:[[:space:]]*"?ssh"?' "$CFG"; then
    fatal "config.yaml sets terminal.backend: ssh — removes the fs read/write boundary"
  fi
fi
ok "terminal.backend is not ssh"

[ -f "$ALLOWLIST" ] || fatal "route allowlist $ALLOWLIST missing"

# --- start route proxy (enforcing) and backend -------------------------------
ALLOWLIST_FILE="$ALLOWLIST" python3 "$ROUTE_PROXY" >/tmp/route_proxy.log 2>&1 &
PROXY_PID=$!
"$HERMES_BIN" "$@" >/tmp/backend.log 2>&1 &
BACKEND_PID=$!

cleanup() { kill "$PROXY_PID" "$BACKEND_PID" 2>/dev/null; }
trap cleanup EXIT

# wait for readiness (backend writes HERMES_BACKEND_READY to its log)
for _ in $(seq 1 90); do
  grep -q HERMES_BACKEND_READY /tmp/backend.log 2>/dev/null && break
  kill -0 "$BACKEND_PID" 2>/dev/null || fatal "backend exited during startup; see /tmp/backend.log"
  sleep 2
done
grep -q HERMES_BACKEND_READY /tmp/backend.log 2>/dev/null || fatal "backend not ready within timeout"

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$@"; }
# POSITIVE liveness only: retry until 200 or ~30s, since the route proxy and the
# shared gateway can bind a moment after the backend reports ready (a transient that
# used to FATAL the first user provisioned). The NEGATIVE security checks below
# (the 403s, and the unreachable-egress checks) are never retried — they must hold
# on the first probe.
wait_200() { for _ in $(seq 1 30); do [ "$(code "$@")" = 200 ] && return 0; sleep 1; done; return 1; }
AUTH=(-H "Authorization: Bearer $TOKEN")

# --- LIVE 3: route proxy up and enforcing ------------------------------------
wait_200 "http://127.0.0.1:$PORT_PROXY/api/status" || fatal "route proxy not serving /api/status"
EXCLUDED=$(code -X POST "${AUTH[@]}" -H 'content-type: application/json' -d '{"path":"'"$HERMES_HOME"'/gate-probe","content":"x"}' "http://127.0.0.1:$PORT_PROXY/api/fs/write-text")
[ "$EXCLUDED" = 403 ] || fatal "route allowlist not enforcing: POST /api/fs/write-text returned $EXCLUDED, expected 403"
ok "route proxy enforcing (write-text 403)"

# --- LIVE 4: read boundary real ----------------------------------------------
IN=$(code "${AUTH[@]}" "http://127.0.0.1:$PORT_PROXY/api/fs/list?path=$HERMES_HOME")
OUT=$(code "${AUTH[@]}" "http://127.0.0.1:$PORT_PROXY/api/fs/list?path=/etc")
[ "$IN" = 200 ] || fatal "in-boundary read of $HERMES_HOME returned $IN, expected 200"
[ "$OUT" = 403 ] || fatal "out-of-boundary read of /etc returned $OUT, expected 403 — boundary not active"
ok "read boundary live (in=200 /etc=403)"

# --- LIVE 5: egress locked, provider NOT direct, gateway up -------------------
# positive liveness: our in-network gateway must answer (inference path alive).
wait_200 --noproxy '*' "$GATE_GATEWAY_HEALTH" || fatal "model gateway $GATE_GATEWAY_HEALTH not reachable; inference path down"
# the provider must NOT be reachable directly — through the proxy OR raw (a hook
# that strips proxy env must still fail). curl exit 0 means it connected.
prov_proxy=$(curl -s -o /dev/null --max-time 12 "https://$GATE_PROVIDER_HOST/"; echo $?)
prov_raw=$(env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy \
           curl -s -o /dev/null --max-time 12 "https://$GATE_PROVIDER_HOST/"; echo $?)
[ "$prov_proxy" != 0 ] || fatal "model provider $GATE_PROVIDER_HOST reachable via proxy — exfil channel (attacker key -> provider)"
[ "$prov_raw" != 0 ]   || fatal "model provider $GATE_PROVIDER_HOST reachable raw — exfil channel (attacker key -> provider)"
# a generic disallowed host must also be unreachable raw.
bad_raw=$(env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy \
          curl -s -o /dev/null --max-time 12 "https://$GATE_BAD_HOST/"; echo $?)
[ "$bad_raw" != 0 ] || fatal "disallowed host $GATE_BAD_HOST reachable raw — egress not locked"
ok "egress locked (gateway up; provider + $GATE_BAD_HOST both unreachable)"

echo "[gate] ALL INVARIANTS SATISFIED — serving" >&2
trap - EXIT
wait "$BACKEND_PID"
