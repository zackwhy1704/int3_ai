#!/usr/bin/env bash
# db-counts.sh <db-container> <user> <password>: stable row counts of the tables that matter, one
# per line, numbers and names only. Backup writes them; a restore must reproduce them exactly.
set -euo pipefail
DB="$1" U="$2" P="$3"
q() { docker exec -e PGPASSWORD="$P" "$DB" psql -U "$U" -d "$1" -tAc "$2"; }
for t in tenants launcher_invites user_backends launcher_devices service_tokens hermes_compat; do
  n="$(q control "SELECT count(*) FROM $t" 2>/dev/null || echo absent)"
  echo "control.$t $n"
done
for d in $(q control "SELECT datname FROM pg_database WHERE datname LIKE 't\_%' ORDER BY 1"); do
  for t in claims documents; do
    n="$(q "$d" "SELECT count(*) FROM $t" 2>/dev/null || echo absent)"
    echo "$d.$t $n"
  done
done
