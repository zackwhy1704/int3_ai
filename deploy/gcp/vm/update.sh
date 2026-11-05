#!/usr/bin/env bash
# On request only (the VM never auto-updates): move to the latest commit of the configured ref, then
# rebuild and restart the shared stack. Per-user containers keep running. Prints the before and
# after commits. Updating a per-user gated image needs that user re-provisioned (see RUNBOOK.md).
set -euo pipefail
. "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"
before="$(git rev-parse --short HEAD)"
git fetch -q origin "$INT3_REF"
git checkout -q --detach FETCH_HEAD
install -m 0644 deploy/gcp/systemd/*.service deploy/gcp/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now int3-backup.timer int3-restore-test.timer int3-backup-metrics.timer
echo "[update] $before -> $(git rev-parse --short HEAD)"
systemctl restart int3-stack.service
