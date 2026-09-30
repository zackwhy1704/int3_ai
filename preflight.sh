#!/usr/bin/env bash
# Run ~10 minutes before a demo: ./preflight.sh
# Prints PASS or FAIL per check and exits non-zero if anything failed.
cd "$(dirname "$0")"
fail=0

check() {
  local name=$1; shift
  if "$@" >/dev/null 2>&1; then echo "PASS  $name"; else echo "FAIL  $name"; fail=1; fi
}

# Give a just-started stack up to 30s to finish starting before judging it.
for _ in $(seq 1 30); do
  curl -sf -o /dev/null http://localhost:5173/api/users && break
  sleep 1
done

for svc in db backend web; do
  check "container '$svc' running" test -n "$(docker compose ps --status running -q "$svc" 2>/dev/null)"
done
check "web UI reachable at http://localhost:5173" curl -sf -o /dev/null http://localhost:5173/
check "API reachable through the web proxy" curl -sf -o /dev/null http://localhost:5173/api/users

docker compose exec -T backend python -m app.preflight
status=$?
if [ "$status" -ne 0 ]; then
  fail=1
  # 1 = a check above failed; anything else means the checks couldn't run at all.
  [ "$status" -ne 1 ] && echo "FAIL  backend checks could not run (exit $status)"
fi

echo
if [ "$fail" = 0 ]; then
  echo "PREFLIGHT: ALL PASS"
else
  echo "PREFLIGHT: FAILED - see 'If it breaks' in README.md"
fi
exit "$fail"
