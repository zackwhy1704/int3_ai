#!/usr/bin/env bash
# Operator commands, from your PC:
#   bash deploy/gcp/ops.sh status | stop | start | ssh | ip
#   bash deploy/gcp/ops.sh uptime <launcher-hostname>   # uptime check + email alert (once, after TLS)
#   bash deploy/gcp/ops.sh backup-alert                 # alert when backups or restore tests go stale
#
# A stopped VM bills no compute; the disk and static IP keep billing. Everything returns on start:
# the stack via int3-stack.service, the per-user containers via their restart policy.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL="--path="
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/config.env"
G=(gcloud --project "$GCP_PROJECT")
V=(compute instances)

# Monitoring REST API: gcloud's beta/alpha groups prompt to install components, which hangs unattended.
mon() {  # <METHOD> <path> [json-body]
  local url="https://monitoring.googleapis.com/v3/projects/$GCP_PROJECT/$2"
  if [ -n "${3:-}" ]; then
    curl -fsS -X "$1" "$url" -H "Authorization: Bearer $("${G[@]}" auth print-access-token 2>/dev/null)" -H 'content-type: application/json' -d "$3"
  else
    curl -fsS -X "$1" "$url" -H "Authorization: Bearer $("${G[@]}" auth print-access-token 2>/dev/null)"
  fi
}
field_name() { python -c 'import json,sys; d=json.load(sys.stdin); print(next((x["name"] for k in ("notificationChannels","alertPolicies") for x in d.get(k,[]) if x.get("displayName")==sys.argv[1]), d.get("name","") if "displayName" in d else ""))' "$1"; }
alert_channel() {
  local email found
  found="$(mon GET notificationChannels | field_name 'int3 alerts')"
  if [ -n "$found" ]; then echo "$found"; return; fi
  email="${INT3_ALERT_EMAIL:-$("${G[@]}" config get-value account 2>/dev/null)}"
  mon POST notificationChannels "{\"type\":\"email\",\"displayName\":\"int3 alerts\",\"labels\":{\"email_address\":\"$email\"}}" | field_name 'int3 alerts'
}
ensure_policy() {  # <displayName> <json>
  [ -n "$(mon GET alertPolicies | field_name "$1")" ] || mon POST alertPolicies "$2" >/dev/null
}
case "${1:-}" in
  status) "${G[@]}" "${V[@]}" describe "$INT3_VM" --zone "$GCP_ZONE" --format='value(name,status,machineType.basename(),networkInterfaces[0].accessConfigs[0].natIP)' ;;
  stop)   "${G[@]}" "${V[@]}" stop  "$INT3_VM" --zone "$GCP_ZONE" ;;
  start)  "${G[@]}" "${V[@]}" start "$INT3_VM" --zone "$GCP_ZONE" ;;
  ssh)    shift; "${G[@]}" compute ssh "$INT3_VM" --zone "$GCP_ZONE" --tunnel-through-iap "$@" ;;
  ip)     "${G[@]}" compute addresses describe "$INT3_ADDRESS" --region "$GCP_REGION" --format='value(address)' ;;
  uptime)
    host="${2:?usage: ops.sh uptime <launcher-hostname>}"
    "${G[@]}" monitoring uptime list-configs --filter='displayName="int3 launcher healthz"' --format='value(name)' | grep -q . \
      || "${G[@]}" monitoring uptime create 'int3 launcher healthz' --resource-type=uptime-url \
           --resource-labels="host=$host,project_id=$GCP_PROJECT" --protocol=https --port=443 \
           --path=/launcher/healthz --period=5 --timeout=10 --status-classes=2xx --validate-ssl=true >/dev/null
    channel="$(alert_channel)"
    ensure_policy 'int3 launcher down' "{\"displayName\":\"int3 launcher down\",\"combiner\":\"OR\",\"notificationChannels\":[\"$channel\"],\"conditions\":[{\"displayName\":\"healthz failing\",\"conditionThreshold\":{\"filter\":\"metric.type=\\\"monitoring.googleapis.com/uptime_check/check_passed\\\" AND resource.type=\\\"uptime_url\\\" AND resource.label.host=\\\"$host\\\"\",\"aggregations\":[{\"alignmentPeriod\":\"600s\",\"perSeriesAligner\":\"ALIGN_NEXT_OLDER\",\"crossSeriesReducer\":\"REDUCE_COUNT_FALSE\",\"groupByFields\":[\"resource.label.host\"]}],\"comparison\":\"COMPARISON_GT\",\"thresholdValue\":1,\"duration\":\"0s\",\"trigger\":{\"count\":1}}}]}"
    echo "uptime check on https://$host/launcher/healthz; alerts go to the channel 'int3 alerts'"
    ;;
  backup-alert)
    channel="$(alert_channel)"
    # A policy cannot name a metric that does not exist yet, so declare both gauges first (idempotent).
    for m in backup_age_hours restore_test_age_hours; do
      mon POST metricDescriptors "{\"type\":\"custom.googleapis.com/int3/$m\",\"metricKind\":\"GAUGE\",\"valueType\":\"INT64\",\"unit\":\"h\",\"description\":\"int3: hours since the last good run\"}" >/dev/null
    done
    cond() {  # <title> <metric> <hours>
      printf '{"displayName":"%s","conditionThreshold":{"filter":"metric.type=\\"custom.googleapis.com/int3/%s\\" AND resource.type=\\"global\\"","aggregations":[{"alignmentPeriod":"3600s","perSeriesAligner":"ALIGN_MAX"}],"comparison":"COMPARISON_GT","thresholdValue":%s,"duration":"0s","trigger":{"count":1}}}' "$1" "$2" "$3"
    }
    ensure_policy 'int3 backups stale' "$(printf '{"displayName":"int3 backups stale","combiner":"OR","notificationChannels":["%s"],"conditions":[%s,%s]}' "$channel" \
      "$(cond 'last good backup older than 26h' backup_age_hours 26)" \
      "$(cond 'last good restore test older than 8 days' restore_test_age_hours 192)")"
    echo "alert policy 'int3 backups stale' is set"
    ;;
  *) sed -n 2,7p "$0"; exit 2 ;;
esac
