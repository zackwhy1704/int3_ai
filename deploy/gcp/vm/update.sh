#!/usr/bin/env bash
# On request only (the VM never auto-updates): move to ONE EXPLICIT, reviewed commit, then rebuild and
# restart the shared stack. Per-user containers keep running. Prints the before and after commits.
# Updating a per-user gated image needs that user re-provisioned (see RUNBOOK.md).
#
#   sudo bash /opt/int3/deploy/gcp/vm/update.sh <commit-sha>
#
# Pinned on purpose: INT3_REF (config.env) is only where commits are fetched FROM. Moving to "the
# latest commit of the branch" deployed whatever had been pushed last (docs/LOCAL_MODE_DEPLOY_PLAN.md,
# "Risk found while reading"). The commit must be a hex SHA (7-40 chars, no branch or tag names) and
# must be on INT3_REF, so a typo or a commit from elsewhere is refused before anything changes.
set -euo pipefail
usage() { echo "usage: update.sh <commit-sha>   (a commit on $INT3_REF; see RUNBOOK.md \"Update\")" >&2; exit 2; }
. "$(dirname "$0")/lib.sh"
[ "$#" -eq 1 ] || usage
want="$1"
[[ "$want" =~ ^[0-9a-f]{7,40}$ ]] || { echo "[update] refused: '$want' is not a commit SHA (7-40 lowercase hex)" >&2; usage; }
cd "$REPO_ROOT"
before="$(git rev-parse --short HEAD)"
git fetch -q origin "$INT3_REF"
sha="$(git rev-parse -q --verify "$want^{commit}" 2>/dev/null)" \
  || { echo "[update] refused: commit $want not found after fetching $INT3_REF" >&2; exit 1; }
git merge-base --is-ancestor "$sha" FETCH_HEAD \
  || { echo "[update] refused: $want is not on $INT3_REF" >&2; exit 1; }
git checkout -q --detach "$sha"
install -m 0644 deploy/gcp/systemd/*.service deploy/gcp/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now int3-backup.timer int3-restore-test.timer int3-backup-metrics.timer int3-purge-memories.timer
echo "[update] $before -> $(git rev-parse --short HEAD)"
systemctl restart int3-stack.service
