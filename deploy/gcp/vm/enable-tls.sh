#!/usr/bin/env bash
# Request certificates. Run ONLY after the owner has confirmed nslookup: the flag says so, and this
# independently checks public DNS and refuses if the wildcard does not point at this VM.
#
#   sudo bash deploy/gcp/vm/enable-tls.sh --owner-confirmed-dns --staging      # first, no rate limits
#   sudo bash deploy/gcp/vm/enable-tls.sh --owner-confirmed-dns --production
set -euo pipefail
. "$(dirname "$0")/lib.sh"
confirmed="" issuer=""
for a in "$@"; do
  case "$a" in
    --owner-confirmed-dns) confirmed=1 ;;
    --staging) issuer=staging ;;
    --production) issuer=production ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
[ -n "$confirmed" ] || { echo "refusing: the owner has not confirmed DNS (--owner-confirmed-dns)" >&2; exit 2; }
[ -n "$issuer" ] || { echo "say --staging or --production" >&2; exit 2; }

ip="$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)"
probe="$(new_id).$INT3_BASE_DOMAIN"
for ns in 1.1.1.1 8.8.8.8; do
  got="$(dig +short +time=5 +tries=2 A "$probe" "@$ns" | tail -1)"
  [ "$got" = "$ip" ] || { echo "refusing: $ns answers '${got:-nothing}' for the wildcard; this VM is $ip" >&2; exit 1; }
done
echo "[tls] wildcard resolves to $ip on 1.1.1.1 and 8.8.8.8"

render_caddy "$issuer"
# Restart, not reload: a reload keeps certificates cached in memory, so switching from staging to
# production would go on serving the staging certificate.
docker compose -f "$GCP_ROOT/caddy/compose.yml" up -d
docker restart int3-caddy >/dev/null

launcher_id="$(awk -F'\t' '$3=="launcher"{print $1; exit}' "$ROUTES")"
host="$launcher_id.$INT3_BASE_DOMAIN"
insecure=(); [ "$issuer" = staging ] && insecure=(--insecure)   # staging certificates are untrusted
for _ in $(seq 1 30); do
  code="$(curl -s "${insecure[@]}" -o /dev/null -w '%{http_code}' --max-time 10 "https://$host/launcher/healthz" || true)"
  [ "$code" = 200 ] && break
  sleep 5
done
[ "$code" = 200 ] || { echo "[tls] FAILED: https://$host/launcher/healthz answered ${code:-nothing}; see: docker logs int3-caddy" >&2; exit 1; }
echo "[tls] https://$host/launcher/healthz = 200 ($issuer certificate)"
echo "[tls] listening: $(ss -ltnH | awk '{print $4}' | grep -E ':(80|443)$' | sort -u | tr '\n' ' ')"
