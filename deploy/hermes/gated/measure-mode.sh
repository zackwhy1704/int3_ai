#!/usr/bin/env bash
# measure-mode.sh on|off <project>
#
# Swap every per-user route proxy of an ISOLATED project between MEASURE (allow all, log every
# "METHOD /path", gated/route_proxy.py:6-8) and ENFORCE (default-deny allowlist). Used to
# re-measure the routes a real session needs (e.g. after a Hermes re-pin); `off` restores
# enforcement. The startup gate (startup-gate.sh) only proves enforcement at boot, so a proxy
# left in measure mode is a hole: this script self-checks before returning and exits non-zero
# if any proxy is not in the requested mode.
#
#   on   kill the enforcing proxy; start route_proxy.py with no ALLOWLIST_FILE, routes to
#        /tmp/routes-measure.log (truncated first). Check: log says MEASURING and a tokenless
#        POST /api/fs/write-text is FORWARDED (Hermes answers 401; no token, so nothing written).
#   off  kill the measuring proxy; start it with ALLOWLIST_FILE=/opt/proxy/allowlist.txt.
#        Check: log says ENFORCING and the same tokenless write-text gets the proxy's 403.
#
# Refuses the default project (hermes-deploy). Same port (9122), same in-container command the
# gate uses (startup-gate.sh:73); the gate's own `wait` is on the backend, so the restart does
# not take the container down.
set -u
export MSYS_NO_PATHCONV=1
mode="${1:-}"; project="${2:-}"
case "$mode" in on|off) ;; *) echo "usage: $0 on|off <project>" >&2; exit 2 ;; esac
[ -n "$project" ] || { echo "usage: $0 on|off <project>" >&2; exit 2; }
[ "$project" != "hermes-deploy" ] || { echo "REFUSING: never switch the default project (hermes-deploy)" >&2; exit 2; }

# Per-user backends are named hermes-<project>-<tenant>-<user> (provision-user.sh: hermes_name).
mapfile -t ctrs < <(docker ps --format '{{.Names}}' --filter "name=^hermes-$project-" | tr -d '\r' | sort)
[ "${#ctrs[@]}" -gt 0 ] || { echo "no running per-user backends for project $project" >&2; exit 1; }

PROBE='{"path":"/opt/data/measure-mode-probe","content":"x"}'
if [ "$mode" = on ]; then want=MEASURING; want_code=401; else want=ENFORCING; want_code=403; fi
fail=0
for c in "${ctrs[@]}"; do
  docker exec "$c" test -f /opt/proxy/route_proxy.py || { echo "[measure-mode] $c: not a gated backend, skipped"; continue; }
  # Anchored: matches only the proxy process itself (its argv starts with python3), never a shell
  # whose command line merely mentions the script.
  docker exec "$c" pkill -f '^python3 /opt/proxy/route_proxy.py' || true
  for _ in $(seq 1 20); do docker exec "$c" pgrep -f '^python3 /opt/proxy/route_proxy.py' >/dev/null || break; sleep 0.5; done
  before="$(docker exec "$c" sh -c 'grep -c "^route_proxy " /tmp/route_proxy.log 2>/dev/null || echo 0' | tr -d '\r')"
  if [ "$mode" = on ]; then
    docker exec "$c" sh -c ': > /tmp/routes-measure.log'
    docker exec -d -e PROXY_LOG=/tmp/routes-measure.log "$c" \
      sh -c 'exec python3 /opt/proxy/route_proxy.py >>/tmp/route_proxy.log 2>&1'
  else
    docker exec -d -e ALLOWLIST_FILE=/opt/proxy/allowlist.txt "$c" \
      sh -c 'exec python3 /opt/proxy/route_proxy.py >>/tmp/route_proxy.log 2>&1'
  fi
  line=""; st=""
  for _ in $(seq 1 30); do
    line="$(docker exec "$c" sh -c "grep '^route_proxy ' /tmp/route_proxy.log | tail -n +$((before + 1)) | tail -1" | tr -d '\r')"
    st="$(docker exec "$c" curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:9122/api/status)"
    [ -n "$line" ] && [ "$st" = 200 ] && break; sleep 1
  done
  wt="$(docker exec "$c" curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST \
        -H 'content-type: application/json' -d "$PROBE" http://127.0.0.1:9122/api/fs/write-text)"
  if [[ "$line" == *"$want"* ]] && [ "$st" = 200 ] && [ "$wt" = "$want_code" ]; then
    echo "[measure-mode] ok   $c: $line | /api/status=$st tokenless write-text=$wt"
  else
    echo "[measure-mode] FAIL $c: want $want + write-text $want_code; got '${line:-no new proxy line}' status=$st write-text=$wt" >&2
    fail=1
  fi
done
exit "$fail"
