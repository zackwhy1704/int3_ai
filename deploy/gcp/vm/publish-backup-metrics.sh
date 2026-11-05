#!/usr/bin/env bash
# Hourly (int3-backup-metrics.timer): publish how long ago the last GOOD backup and restore test were,
# as Cloud Monitoring gauges. The alert fires on age, so every failure mode is covered the same way:
# a failing script, a timer that never ran, a full disk. (A stopped VM publishes nothing; the uptime
# check covers that.) No marker yet counts as very old on purpose. Numbers only; no secrets.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
age() { [ -f "$1" ] && echo $(( ( $(date +%s) - $(stat -c %Y "$1") ) / 3600 )) || echo 9999; }
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
series() {  # <metric> <value>
  printf '{"metric":{"type":"custom.googleapis.com/int3/%s"},"resource":{"type":"global","labels":{"project_id":"%s"}},"points":[{"interval":{"endTime":"%s"},"value":{"int64Value":"%s"}}]}' \
    "$1" "$GCP_PROJECT" "$now" "$2"
}
body="{\"timeSeries\":[$(series backup_age_hours "$(age /var/lib/int3/last-backup.ok)"),$(series restore_test_age_hours "$(age /var/lib/int3/last-restore-test.ok)")]}"
curl -fsS -X POST "https://monitoring.googleapis.com/v3/projects/$GCP_PROJECT/timeSeries" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" -H 'content-type: application/json' -d "$body" >/dev/null
echo "[metrics] backup_age_hours=$(age /var/lib/int3/last-backup.ok) restore_test_age_hours=$(age /var/lib/int3/last-restore-test.ok)"
