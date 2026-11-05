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
case "${1:-}" in
  status) "${G[@]}" "${V[@]}" describe "$INT3_VM" --zone "$GCP_ZONE" --format='value(name,status,machineType.basename(),networkInterfaces[0].accessConfigs[0].natIP)' ;;
  stop)   "${G[@]}" "${V[@]}" stop  "$INT3_VM" --zone "$GCP_ZONE" ;;
  start)  "${G[@]}" "${V[@]}" start "$INT3_VM" --zone "$GCP_ZONE" ;;
  ssh)    shift; "${G[@]}" compute ssh "$INT3_VM" --zone "$GCP_ZONE" --tunnel-through-iap "$@" ;;
  ip)     "${G[@]}" compute addresses describe "$INT3_ADDRESS" --region "$GCP_REGION" --format='value(address)' ;;
  uptime)
    host="${2:?usage: ops.sh uptime <launcher-hostname>}"
    email="${INT3_ALERT_EMAIL:-$("${G[@]}" config get-value account 2>/dev/null)}"
    channel="$("${G[@]}" beta monitoring channels list --filter='displayName="int3 alerts"' --format='value(name)' | head -1)"
    [ -n "$channel" ] || channel="$("${G[@]}" beta monitoring channels create --display-name='int3 alerts' \
      --type=email --channel-labels="email_address=$email" --format='value(name)')"
    "${G[@]}" monitoring uptime list-configs --filter='displayName="int3 launcher healthz"' --format='value(name)' | grep -q . \
      || "${G[@]}" monitoring uptime create 'int3 launcher healthz' --resource-type=uptime-url \
           --resource-labels="host=$host,project_id=$GCP_PROJECT" --protocol=https --port=443 \
           --path=/launcher/healthz --period=5 --timeout=10 --status-classes=2xx --validate-ssl=true >/dev/null
    policy="$(mktemp)"
    cat > "$policy" <<JSON
{"displayName":"int3 launcher down","combiner":"OR","notificationChannels":["$channel"],
 "conditions":[{"displayName":"healthz failing","conditionThreshold":{
   "filter":"metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND resource.label.host=\"$host\"",
   "aggregations":[{"alignmentPeriod":"600s","perSeriesAligner":"ALIGN_NEXT_OLDER","crossSeriesReducer":"REDUCE_COUNT_FALSE","groupByFields":["resource.label.host"]}],
   "comparison":"COMPARISON_GT","thresholdValue":1,"duration":"0s","trigger":{"count":1}}}]}
JSON
    "${G[@]}" alpha monitoring policies list --filter='displayName="int3 launcher down"' --format='value(name)' | grep -q . \
      || "${G[@]}" alpha monitoring policies create --policy-from-file="$(cygpath -w "$policy" 2>/dev/null || echo "$policy")" >/dev/null
    rm -f "$policy"
    echo "uptime check on https://$host/launcher/healthz; alerts go to the notification channel 'int3 alerts'"
    ;;
  backup-alert)
    channel="$("${G[@]}" beta monitoring channels list --filter='displayName="int3 alerts"' --format='value(name)' | head -1)"
    [ -n "$channel" ] || { echo "run ops.sh uptime first: it creates the alert channel" >&2; exit 2; }
    cond() {  # <title> <metric> <hours>
      printf '{"displayName":"%s","conditionThreshold":{"filter":"metric.type=\\"custom.googleapis.com/int3/%s\\" AND resource.type=\\"global\\"","aggregations":[{"alignmentPeriod":"3600s","perSeriesAligner":"ALIGN_MAX"}],"comparison":"COMPARISON_GT","thresholdValue":%s,"duration":"0s","trigger":{"count":1}}}' "$1" "$2" "$3"
    }
    policy="$(mktemp)"
    printf '{"displayName":"int3 backups stale","combiner":"OR","notificationChannels":["%s"],"conditions":[%s,%s]}' "$channel" \
      "$(cond 'last good backup older than 26h' backup_age_hours 26)" \
      "$(cond 'last good restore test older than 8 days' restore_test_age_hours 192)" > "$policy"
    "${G[@]}" alpha monitoring policies list --filter='displayName="int3 backups stale"' --format='value(name)' | grep -q . \
      || "${G[@]}" alpha monitoring policies create --policy-from-file="$(cygpath -w "$policy" 2>/dev/null || echo "$policy")" >/dev/null
    rm -f "$policy"
    echo "alert policy 'int3 backups stale' is set"
    ;;
  *) sed -n 2,7p "$0"; exit 2 ;;
esac
