#!/usr/bin/env bash
# Daily backup (int3-backup.timer) to gs://$INT3_BUCKET/<date>/: all Postgres databases and roles,
# a counts manifest for restore-test.sh, and a tar of each user's Hermes volume. The bucket deletes
# objects after INT3_BACKUP_DAYS. The keyvault key and DB passwords are in Secret Manager, not here.
set -euo pipefail
umask 077
. "$(dirname "$0")/lib.sh"
load_secrets
DB="$DEPLOY_PROJECT-db-1"
stamp="$(date -u +%Y-%m-%dT%H%MZ)"
tmp="$(mktemp -d -p /var/lib/int3)"
trap 'rm -rf "$tmp"' EXIT

docker exec -e PGPASSWORD="$ADMIN_DB_PASSWORD" "$DB" pg_dumpall -U "$ADMIN_DB_USER" | gzip > "$tmp/postgres.sql.gz"
bash "$GCP_ROOT/vm/db-counts.sh" "$DB" "$ADMIN_DB_USER" "$ADMIN_DB_PASSWORD" > "$tmp/counts.txt"
for vol in $(docker volume ls -q --filter "name=hermes-home-$DEPLOY_PROJECT-"); do
  docker run --rm -v "$vol:/v:ro" busybox tar -C /v -czf - . > "$tmp/$vol.tar.gz"
done
gcloud --project "$GCP_PROJECT" --quiet storage cp "$tmp"/* "gs://$INT3_BUCKET/$stamp/" >/dev/null
echo "[backup] gs://$INT3_BUCKET/$stamp/: $(ls "$tmp" | wc -l) files, $(du -sh "$tmp" | cut -f1)"
