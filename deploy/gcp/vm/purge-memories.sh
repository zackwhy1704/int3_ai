#!/usr/bin/env bash
# Daily memory retention (int3-purge-memories.timer): delete the assistant memories of every tenant
# cancelled more than MEMORY_RETENTION_DAYS (default 30) ago, audited in memory_audit with counts only
# (backend/app/launcher/memory.py::purge_cancelled). Prints counts, never content. Backups still hold a
# purged memory until the bucket deletes that backup (INT3_BACKUP_DAYS), so the worst case is
# MEMORY_RETENTION_DAYS + INT3_BACKUP_DAYS after cancellation.
# Runs as the brain's own app roles (control: SELECT tenants, INSERT memory_audit; each tenant: DELETE
# user_memories), so no admin secret is loaded.
set -euo pipefail
umask 077
. "$(dirname "$0")/lib.sh"
docker exec "$DEPLOY_PROJECT-brain-1" python -m app.launcher.admin purge-memories
touch /var/lib/int3/last-memory-purge.ok
