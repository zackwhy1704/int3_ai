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
#   Release: Hermes Agent v0.21.7 (2026-10-12)
#   Page:    https://github.com/NousResearch/hermes-agent/releases/tag/v0.21.7
#   Tag:     v0.21.7  ->  commit 71d34bad1045610252eda02ee79549436896e784
HERMES_COMMIT="${HERMES_COMMIT:-71d34bad1045610252eda02ee79549436896e784}"
TAG="${BASE_TAG:-hermes-base-pinned}"

echo "[build-base] $HERMES_REPO @ $HERMES_COMMIT -> $TAG"
docker build -t "$TAG" "$HERMES_REPO#$HERMES_COMMIT"
echo "[build-base] done: $TAG (now run: docker build -t hermes-gated --build-arg BASE=$TAG -f gated/Dockerfile.gated gated)"
