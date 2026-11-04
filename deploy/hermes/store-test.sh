#!/usr/bin/env bash
# Store-test helper (operator, LOCAL only): mint a single-use launcher invite for one user of a
# running launcher-mode stack, so an official Microsoft Store Hermes Desktop can be tested on this
# PC through the int3 launcher. The invite is a credential: it is NEVER printed. It goes to the
# Windows clipboard (--clip, default) or to a file only your account can read (--out <file>).
#
#   bash deploy/hermes/store-test.sh [--project hermes-launch] [--user priya] [--out <file>]
#
# Prereqs: a stack started with launcher/e2e/stack-up.sh (INGRESS_MODE=launcher, launcher edge on
# 127.0.0.1:8100), the int3 launcher installed, and Store Hermes installed. The Store package needs
# Windows 11 22H2+ (Hermes v0.22.6 apps/desktop/BUILDING.md:14).
#
# After the test, report the version the launcher shows ("Hermes <version> · untested") or
#   powershell -c "(Get-AppxPackage NousResearchInc.HermesAgent).Version"
# and the operator records it (then the launcher shows the release's state, e.g. supported):
#   bash deploy/hermes/store-test.sh --record <package-version> [--release v0.22.6]
set -euo pipefail
export MSYS_NO_PATHCONV=1
PROJECT=hermes-launch USER_ID=priya TENANT=brindlewood OUT="" RECORD="" RELEASE=v0.22.6 TTL=4
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --user) USER_ID="$2"; shift 2 ;;
    --tenant) TENANT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --record) RECORD="$2"; shift 2 ;;
    --release) RELEASE="$2"; shift 2 ;;
    --ttl-hours) TTL="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ "$PROJECT" != "hermes-deploy" ] || { echo "refusing the default project" >&2; exit 2; }
BRAIN="${PROJECT}-brain-1"
docker inspect "$BRAIN" >/dev/null 2>&1 || { echo "$BRAIN is not running: start a stack with launcher/e2e/stack-up.sh" >&2; exit 2; }
ENVF="$(cd "$(dirname "$0")" && pwd)/.env"
ADMIN_DB_PASSWORD="$(grep -E '^ADMIN_DB_PASSWORD=' "$ENVF" | cut -d= -f2-)"
admin() {
  docker exec -e ADMIN_DB_USER="${ADMIN_DB_USER:-brain}" -e ADMIN_DB_PASSWORD="$ADMIN_DB_PASSWORD" \
    "$BRAIN" python -m app.launcher.admin "$@"
}

if [ -n "$RECORD" ]; then
  admin record-build --platform windows --package-version "$RECORD" --release "$RELEASE"
  exit 0
fi

link="$(admin invite --tenant "$TENANT" --user "$USER_ID" --ttl-hours "$TTL" | grep -o 'http[^ ]*/invite#cb_inv_[A-Za-z0-9_-]*' || true)"
[ -n "$link" ] || { echo "no invite minted (is $USER_ID@$TENANT provisioned in launcher mode?)" >&2; exit 1; }
if [ -n "$OUT" ]; then
  ( umask 077; printf '%s\n' "$link" > "$OUT" )
  command -v icacls >/dev/null && icacls "$(cygpath -w "$OUT" 2>/dev/null || echo "$OUT")" /inheritance:r /grant:r "${USERNAME:-$USER}:F" >/dev/null
  echo "invite for $USER_ID@$TENANT written to $OUT (single use, ${TTL}h). Delete it after sign-in."
else
  printf '%s' "$link" | clip.exe
  echo "invite for $USER_ID@$TENANT is on the clipboard (single use, ${TTL}h). Paste it into int3 › Sign in."
fi
unset link
v="$(powershell -NoProfile -c "(Get-AppxPackage NousResearchInc.HermesAgent).Version" 2>/dev/null | tr -d '\r' || true)"
echo "Store Hermes Desktop on this PC: ${v:-none found (the Store package needs Windows 11 22H2 or later)}"
