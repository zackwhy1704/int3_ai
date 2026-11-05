#!/usr/bin/env bash
# Prove the newest backup restores: load it into a THROWAWAY Postgres (no network, never the live
# database) and require its counts to equal the manifest written at backup time. Exit 1 on any
# difference. Weekly via int3-restore-test.timer.
#
#   restore-test.sh                                 # newest backup in the bucket
#   restore-test.sh <postgres.sql.gz> <counts.txt>  # local files (no gcloud needed)
set -euo pipefail
umask 077
HERE="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
name="int3-restore-test-$$"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT

if [ $# -eq 2 ]; then
  cp "$1" "$tmp/postgres.sql.gz"; cp "$2" "$tmp/counts.txt"
else
  . "$HERE/lib.sh"
  newest="$(gcloud --project "$GCP_PROJECT" --quiet storage ls "gs://$INT3_BUCKET/" | sort | tail -1)"
  [ -n "$newest" ] || { echo "[restore-test] FAILED: no backups" >&2; exit 1; }
  gcloud --project "$GCP_PROJECT" --quiet storage cp "${newest}postgres.sql.gz" "${newest}counts.txt" "$tmp/" >/dev/null
  echo "[restore-test] restoring $newest"
fi

pw="$(openssl rand -hex 16)"
docker run -d --name "$name" --network none -e POSTGRES_USER=brain -e POSTGRES_PASSWORD="$pw" \
  -e POSTGRES_DB=control pgvector/pgvector:pg16 >/dev/null
for _ in $(seq 1 60); do
  # pg_isready passes during the image's own init phase; a real query on the final server does not.
  docker exec -e PGPASSWORD="$pw" "$name" psql -h 127.0.0.1 -U brain -d control -tAc 'SELECT 1' >/dev/null 2>&1 && break
  sleep 2
done
# The dump recreates role brain with the live password hash, so restore over the local socket.
gunzip -c "$tmp/postgres.sql.gz" | docker exec -i "$name" psql -U brain -d postgres -q >"$tmp/restore.log" 2>&1 || true
# Expected noise only: the cluster already has role brain and database control.
unexpected="$({ grep -E 'ERROR:' "$tmp/restore.log" || true; } | { grep -vE 'role "brain" already exists|database "control" already exists|current user cannot be dropped' || true; } | wc -l)"

bash "$HERE/db-counts.sh" "$name" brain "$pw" > "$tmp/restored.txt"
if ! diff "$tmp/counts.txt" "$tmp/restored.txt" >"$tmp/diff.txt"; then
  echo "[restore-test] FAILED: restored counts differ from the backup manifest:" >&2
  cat "$tmp/diff.txt" >&2
  exit 1
fi
[ "$unexpected" = 0 ] || { echo "[restore-test] FAILED: $unexpected unexpected restore errors" >&2; grep -E 'ERROR:' "$tmp/restore.log" | head -5 >&2; exit 1; }
echo "[restore-test] OK: $(wc -l < "$tmp/restored.txt") counts match, 0 unexpected errors"
cat "$tmp/restored.txt"
