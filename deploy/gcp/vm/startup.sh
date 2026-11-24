#!/usr/bin/env bash
# VM startup script (GCE runs it as root on EVERY boot; it is idempotent). Installs Docker once,
# clones the public repo once and checks out exactly the int3-sha metadata commit (never
# auto-updates: deploy/gcp/vm/update.sh does that on request), installs the systemd units, and
# starts the stack. Holds no secrets.
set -euo pipefail
exec > >(logger -t int3-startup) 2>&1
md() { curl -fsS -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
INT3_DIR="${INT3_DIR:-/opt/int3}"   # overridable only so test_startup.sh can run this without root

if ! command -v docker >/dev/null; then
  apt-get update -q
  apt-get install -yq ca-certificates curl gnupg git dnsutils openssl gzip unattended-upgrades
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin
  # Container logs are bounded: the disk must not fill from logs.
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'JSON'
{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}
JSON
  systemctl restart docker
fi

# First boot: the new VM runs exactly the reviewed commit in int3-sha, never "whatever the branch
# head is" (the same rule as update.sh). The clone is built in a temp dir and moved into place only
# once it is at that commit, so a refused boot leaves no checkout that a later boot would trust.
if [ ! -d "$INT3_DIR/.git" ]; then
  refuse() { echo "[startup] refused: $*; nothing deployed" >&2; exit 1; }
  ref="$(md int3-ref)"; repo="$(md int3-repo)"
  want="$(md int3-sha 2>/dev/null)" || want=""
  [ -n "$want" ] || refuse "instance metadata int3-sha is missing (set INT3_SHA for infra.sh)"
  [[ "$want" =~ ^[0-9a-f]{7,40}$ ]] || refuse "int3-sha '$want' is not a commit SHA (7-40 lowercase hex)"
  tmp="$(mktemp -d "$(dirname "$INT3_DIR")/.int3-clone.XXXXXX")"   # same filesystem: the mv is atomic
  trap 'rm -rf "$tmp"' EXIT
  git clone -q --branch "$ref" "$repo" "$tmp"
  sha="$(git -C "$tmp" rev-parse -q --verify "$want^{commit}" 2>/dev/null)" \
    || refuse "commit $want not found in $repo"
  git -C "$tmp" merge-base --is-ancestor "$sha" "origin/$ref" || refuse "$want is not on $ref"
  git -C "$tmp" checkout -q --detach "$sha"
  mv "$tmp" "$INT3_DIR"; trap - EXIT
  echo "[startup] cloned $repo at $(git -C "$INT3_DIR" rev-parse --short HEAD) (on $ref)"
fi
install -m 0700 -d /var/lib/int3
install -m 0644 "$INT3_DIR"/deploy/gcp/systemd/*.service "$INT3_DIR"/deploy/gcp/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now int3-backup.timer int3-restore-test.timer int3-backup-metrics.timer int3-purge-memories.timer
systemctl enable int3-stack.service
systemctl start --no-block int3-stack.service
