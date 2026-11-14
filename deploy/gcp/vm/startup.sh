#!/usr/bin/env bash
# VM startup script (GCE runs it as root on EVERY boot; it is idempotent). Installs Docker once,
# clones the public repo once at the configured ref (never auto-updates: deploy/gcp/vm/update.sh
# does that on request), installs the systemd units, and starts the stack. Holds no secrets.
set -euo pipefail
exec > >(logger -t int3-startup) 2>&1
md() { curl -fsS -H 'Metadata-Flavor: Google' "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }

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

if [ ! -d /opt/int3/.git ]; then
  git clone --branch "$(md int3-ref)" "$(md int3-repo)" /opt/int3
fi
install -m 0700 -d /var/lib/int3
install -m 0644 /opt/int3/deploy/gcp/systemd/*.service /opt/int3/deploy/gcp/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now int3-backup.timer int3-restore-test.timer int3-backup-metrics.timer int3-purge-memories.timer
systemctl enable int3-stack.service
systemctl start --no-block int3-stack.service
