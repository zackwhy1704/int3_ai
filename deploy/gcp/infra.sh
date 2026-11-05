#!/usr/bin/env bash
# Operator (your PC, gcloud signed in). Creates everything GCP-side, idempotently, and prints the
# static IP. Requests NO certificates: TLS is a separate step after the DNS record is confirmed.
#
#   bash deploy/gcp/infra.sh
#
# A dedicated VPC, because the project's `default` network has SSH and RDP open to 0.0.0.0/0. On
# int3-net the only inbound rules are 443 from anywhere and 22 from Google's IAP range.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/config.env"
G=(gcloud --project "$GCP_PROJECT" --quiet)
exists() { "$@" >/dev/null 2>&1; }
SA_EMAIL="$INT3_SA@$GCP_PROJECT.iam.gserviceaccount.com"
PROJECT_NUMBER="$("${G[@]}" projects describe "$GCP_PROJECT" --format='value(projectNumber)')"

echo "== network: $INT3_NETWORK (443 from anywhere, 22 from IAP only) =="
exists "${G[@]}" compute networks describe "$INT3_NETWORK" \
  || "${G[@]}" compute networks create "$INT3_NETWORK" --subnet-mode=custom
exists "${G[@]}" compute networks subnets describe "$INT3_NETWORK-$GCP_REGION" --region "$GCP_REGION" \
  || "${G[@]}" compute networks subnets create "$INT3_NETWORK-$GCP_REGION" --network "$INT3_NETWORK" \
       --region "$GCP_REGION" --range "$INT3_SUBNET_RANGE"
exists "${G[@]}" compute firewall-rules describe int3-allow-https \
  || "${G[@]}" compute firewall-rules create int3-allow-https --network "$INT3_NETWORK" \
       --direction INGRESS --allow tcp:443 --source-ranges 0.0.0.0/0
exists "${G[@]}" compute firewall-rules describe int3-allow-iap-ssh \
  || "${G[@]}" compute firewall-rules create int3-allow-iap-ssh --network "$INT3_NETWORK" \
       --direction INGRESS --allow tcp:22 --source-ranges 35.235.240.0/20

echo "== static IP: $INT3_ADDRESS =="
exists "${G[@]}" compute addresses describe "$INT3_ADDRESS" --region "$GCP_REGION" \
  || "${G[@]}" compute addresses create "$INT3_ADDRESS" --region "$GCP_REGION"
IP="$("${G[@]}" compute addresses describe "$INT3_ADDRESS" --region "$GCP_REGION" --format='value(address)')"

echo "== service account: $INT3_SA (only int3-* secrets, only the backup bucket) =="
exists "${G[@]}" iam service-accounts describe "$SA_EMAIL" \
  || "${G[@]}" iam service-accounts create "$INT3_SA" --display-name "int3 pilot VM"
# A condition on the name means the Anthropic secret, which the owner creates, is covered too.
"${G[@]}" projects add-iam-policy-binding "$GCP_PROJECT" --member "serviceAccount:$SA_EMAIL" \
  --role roles/secretmanager.secretAccessor \
  --condition "title=int3-secrets-only,expression=resource.name.startsWith(\"projects/$PROJECT_NUMBER/secrets/int3-\")" >/dev/null
# Viewer: list versions (metadata only), so the VM can tell "none yet" from "could not read".
"${G[@]}" projects add-iam-policy-binding "$GCP_PROJECT" --member "serviceAccount:$SA_EMAIL" \
  --role roles/secretmanager.viewer \
  --condition "title=int3-secrets-only,expression=resource.name.startsWith(\"projects/$PROJECT_NUMBER/secrets/int3-\")" >/dev/null

# Backup-age gauges for the backup-failure alert (deploy/gcp/vm/publish-backup-metrics.sh).
"${G[@]}" projects add-iam-policy-binding "$GCP_PROJECT" --member "serviceAccount:$SA_EMAIL" \
  --role roles/monitoring.metricWriter --condition=None >/dev/null

echo "== secret: int3-stack-env (generated on the VM at first boot, never leaves it but to here) =="
exists "${G[@]}" secrets describe int3-stack-env \
  || "${G[@]}" secrets create int3-stack-env --replication-policy=user-managed --locations="$GCP_REGION" >/dev/null
"${G[@]}" secrets add-iam-policy-binding int3-stack-env --member "serviceAccount:$SA_EMAIL" \
  --role roles/secretmanager.secretVersionAdder >/dev/null

echo "== backups: gs://$INT3_BUCKET (private, ${INT3_BACKUP_DAYS}-day retention) =="
exists "${G[@]}" storage buckets describe "gs://$INT3_BUCKET" \
  || "${G[@]}" storage buckets create "gs://$INT3_BUCKET" --location "$GCP_REGION" \
       --uniform-bucket-level-access --public-access-prevention
lifecycle="$(mktemp)"
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":%s}}]}' "$INT3_BACKUP_DAYS" > "$lifecycle"
"${G[@]}" storage buckets update "gs://$INT3_BUCKET" --lifecycle-file="$(cygpath -w "$lifecycle" 2>/dev/null || echo "$lifecycle")" >/dev/null
rm -f "$lifecycle"
for role in roles/storage.objectCreator roles/storage.objectViewer; do
  "${G[@]}" storage buckets add-iam-policy-binding "gs://$INT3_BUCKET" \
    --member "serviceAccount:$SA_EMAIL" --role "$role" >/dev/null
done

echo "== VM: $INT3_VM ($INT3_MACHINE, ${INT3_DISK_GB} GB) =="
if ! exists "${G[@]}" compute instances describe "$INT3_VM" --zone "$GCP_ZONE"; then
  startup="$HERE/vm/startup.sh"
  "${G[@]}" compute instances create "$INT3_VM" --zone "$GCP_ZONE" \
    --machine-type "$INT3_MACHINE" \
    --network-interface "subnet=$INT3_NETWORK-$GCP_REGION,address=$IP" \
    --service-account "$SA_EMAIL" --scopes cloud-platform \
    --image-family debian-12 --image-project debian-cloud \
    --boot-disk-size "${INT3_DISK_GB}GB" --boot-disk-type pd-balanced \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --deletion-protection \
    --metadata "enable-oslogin=TRUE,block-project-ssh-keys=TRUE,int3-repo=$INT3_REPO,int3-ref=$INT3_REF" \
    --metadata-from-file "startup-script=$(cygpath -w "$startup" 2>/dev/null || echo "$startup")" >/dev/null
fi

key="missing"
"${G[@]}" secrets versions list int3-anthropic-api-key --limit 1 --format='value(name)' 2>/dev/null | grep -q . && key="stored"
cat <<EOF

Static IP: $IP
Add at Porkbun, for int3labs.app:   A   *.app   ->   $IP
Check with:                         nslookup anything.$INT3_BASE_DOMAIN
Then tell the operator it resolves. No certificate is requested until then.

Anthropic key in Secret Manager: $key
EOF
