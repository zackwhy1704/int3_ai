#!/usr/bin/env bash
# Build the stock Hermes backend base image from upstream at a PINNED commit,
# via a git build context, so the server-side Hermes version is reproducible and
# cannot drift under us. gated/Dockerfile.gated layers the proxy + startup gate
# on top of the tag this produces (hermes-base-pinned).
#
# Reproducible: anyone with Docker can run this and get the same base — no
# locally hand-built image required.
set -euo pipefail
HERMES_REPO="${HERMES_REPO:-https://github.com/NousResearch/hermes-agent.git}"
# Pinned to a PUBLISHED release so partners run the same version we build against,
# and an official Windows Desktop installer exists for it (Microsoft Store / MSIX /
# installer; the GitHub release carries no binary asset). Bump only with
# justification; keep README in sync.
#   Release: Hermes Agent v0.22.6 (2026-11-02)
#   Page:    https://github.com/NousResearch/hermes-agent/releases/tag/v0.22.6
#   Tag:     v0.22.6  ->  commit f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab
#   (was v0.21.7 -> 71d34bad; bumped in launcher M0 because Windows MSIX desktops
#   auto-update via App Installer OnLaunch, so desktops cannot be held at v0.21.7.
#   Record any bump in control_schema.sql hermes_compat.)
HERMES_COMMIT="${HERMES_COMMIT:-f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab}"
# sha256 of hermes_cli/web_server.py AT THAT COMMIT (`git show v0.22.6:hermes_cli/web_server.py |
# sha256sum`; v0.21.7 was 14428aa3...). Bump it WITH the commit. Checked against the built image
# here, and against every running backend by the demo N0 row (demo/run_demo.sh).
WEB_SERVER_SHA256="${WEB_SERVER_SHA256:-ac7dbb8d06cd9579f699bf712586d1f414b22818f801d0b461e939eb2c78c5dc}"
TAG="${BASE_TAG:-hermes-base-pinned}"
export MSYS_NO_PATHCONV=1

echo "[build-base] $HERMES_REPO @ $HERMES_COMMIT -> $TAG"
# The label is what the demo/e2e drivers compare to the pin (images.sh) to decide whether to rebuild.
docker build --label "hermes.commit=$HERMES_COMMIT" -t "$TAG" "$HERMES_REPO#$HERMES_COMMIT"
got="$(docker run --rm --entrypoint sha256sum "$TAG" /opt/hermes/hermes_cli/web_server.py | cut -d' ' -f1)"
[ "$got" = "$WEB_SERVER_SHA256" ] || { echo "[build-base] FATAL: web_server.py in $TAG is $got, expected $WEB_SERVER_SHA256 for $HERMES_COMMIT"; exit 1; }
echo "[build-base] verified: web_server.py sha256 matches the pinned commit"
echo "[build-base] done: $TAG (now run: docker build -t hermes-gated --build-arg BASE=$TAG -f gated/Dockerfile.gated gated)"
