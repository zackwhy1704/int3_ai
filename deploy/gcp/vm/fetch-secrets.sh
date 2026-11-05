#!/usr/bin/env bash
# Pull secrets from Secret Manager into /run/int3/stack.env (tmpfs, root only; gone at shutdown).
# First boot only: generate the non-model secrets and store them as int3-stack-env, so a rebuilt VM
# comes back with the same database passwords and the keyvault key. Prints names and states, never
# a value. The Anthropic key is the owner's: stored by hand, read here, never generated.
#
# What this does NOT claim: Docker keeps container env in its own config under /var/lib/docker
# (root only, on the encrypted boot disk). "tmpfs only" is true of this file, not of that.
set -euo pipefail
umask 077
. "$(dirname "$0")/lib.sh"
mkdir -p /run/int3

sm() { gcloud --project "$GCP_PROJECT" --quiet secrets "$@"; }

# Fail closed: only a SUCCESSFUL listing with no versions means first boot. Generating after a
# failed listing (a network blip) would replace every password the database already holds.
versions="$(sm versions list int3-stack-env --limit 1 --format='value(name)')" \
  || { echo "[secrets] cannot read Secret Manager; refusing to continue" >&2; exit 1; }
if [ -z "$versions" ]; then
  echo "[secrets] first boot: generating int3-stack-env"
  gen="$(mktemp -p /run/int3)"
  priv_der="$(mktemp -p /run/int3)"
  openssl genpkey -algorithm X25519 -outform DER -out "$priv_der" 2>/dev/null
  {
    echo "ADMIN_DB_USER=brain"
    echo "ADMIN_DB_PASSWORD=$(openssl rand -hex 32)"
    echo "APP_CONTROL_PASSWORD=$(openssl rand -hex 32)"
    echo "TENANT_DB_KEY=$(openssl rand -hex 32)"
    echo "GATEWAY_DB_PASSWORD=$(openssl rand -hex 32)"
    echo "KEY_INTAKE_DB_PASSWORD=$(openssl rand -hex 32)"
    echo "TENANT_KEY_KEK=$(openssl rand -base64 32)"
    # keyvault v2 (X25519): raw 32-byte keys, base64, the last 32 bytes of the DER forms.
    echo "KEYVAULT_PRIVATE_KEY=$(tail -c 32 "$priv_der" | base64 -w0)"
    echo "KEYVAULT_PUBLIC_KEY=$(openssl pkey -inform DER -in "$priv_der" -pubout -outform DER | tail -c 32 | base64 -w0)"
  } > "$gen"
  sm versions add int3-stack-env --data-file="$gen" >/dev/null
  rm -f "$gen" "$priv_der"
fi

out="$(mktemp -p /run/int3)"
sm versions access latest --secret int3-stack-env > "$out"
{
  echo
  echo "ANTHROPIC_MODEL=claude-sonnet-5-5"
  echo "ENV=production"
  echo "EGRESS_ALLOW="
} >> "$out"
key_state=missing
if key="$(sm versions access latest --secret int3-anthropic-api-key 2>/dev/null)" && [ -n "$key" ]; then
  printf 'ANTHROPIC_API_KEY=%s\n' "$key" >> "$out"
  key_state=stored
fi
unset key
mv "$out" "$SECRETS"
echo "[secrets] $SECRETS written; Anthropic key: $key_state"
