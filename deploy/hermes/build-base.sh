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
# Pinned upstream commit. Bump only with justification; keep README in sync.
HERMES_COMMIT="${HERMES_COMMIT:-73162b00eefde3794bed0afb53d84a19c0eed230}"
TAG="${BASE_TAG:-hermes-base-pinned}"

echo "[build-base] $HERMES_REPO @ $HERMES_COMMIT -> $TAG"
docker build -t "$TAG" "$HERMES_REPO#$HERMES_COMMIT"
echo "[build-base] done: $TAG (now run: docker build -t hermes-gated --build-arg BASE=$TAG -f gated/Dockerfile.gated gated)"
