#!/usr/bin/env bash
# images.sh — sourced by demo/make-demo.sh and e2e/make-e2e.sh (run from the repo root).
#
# Version-safe shared images. "Build only if missing" let a stale image silently stand in for the
# pinned one: a gated image predating a base re-pin would run the OLD Hermes, and a brain-mcp image
# predating a sidecar change would run old sidecar code. So:
#   1. hermes-base-pinned is rebuilt unless its hermes.commit label == the pin in build-base.sh
#      (build-base.sh sets the label and verifies web_server.py against the pinned tag's hash);
#   2. hermes-gated and hermes-deploy-brain-mcp are ALWAYS built (layer cache keeps it fast);
#   3. hermes-gated must sit on exactly that base: its first N layers == the base's N layers.
# The pin values are parsed from build-base.sh so there is one source of truth. Paths to our own
# scripts are relative to this file; docker build contexts stay repo-relative (the drivers cd to the
# repo root), because a native docker.exe cannot open MSYS paths under MSYS_NO_PATHCONV=1.
_IMAGES_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_PIN_COMMIT="$(sed -n 's/^HERMES_COMMIT="${HERMES_COMMIT:-\([0-9a-f]\{40\}\)}"$/\1/p' "$_IMAGES_HERE/build-base.sh")"
HERMES_PIN_WEB_SERVER_SHA256="$(sed -n 's/^WEB_SERVER_SHA256="${WEB_SERVER_SHA256:-\([0-9a-f]\{64\}\)}"$/\1/p' "$_IMAGES_HERE/build-base.sh")"
export HERMES_PIN_COMMIT HERMES_PIN_WEB_SERVER_SHA256

_layers() { docker image inspect -f '{{range .RootFS.Layers}}{{println .}}{{end}}' "$1" | grep sha256; }

ensure_images() {
  [ -n "$HERMES_PIN_COMMIT" ] && [ -n "$HERMES_PIN_WEB_SERVER_SHA256" ] \
    || { echo "images.sh: could not parse the Hermes pin from deploy/hermes/build-base.sh"; return 1; }
  local have
  have="$(docker image inspect -f '{{index .Config.Labels "hermes.commit"}}' hermes-base-pinned 2>/dev/null || true)"
  if [ "$have" = "$HERMES_PIN_COMMIT" ]; then
    echo "  base: hermes-base-pinned labelled hermes.commit=$HERMES_PIN_COMMIT (matches the pin)"
  else
    echo "  base: hermes-base-pinned label '${have:-<none>}' != pin $HERMES_PIN_COMMIT -> rebuilding"
    bash "$_IMAGES_HERE/build-base.sh" || return 1
  fi
  docker build -q -t hermes-gated --build-arg BASE=hermes-base-pinned \
    -f deploy/hermes/gated/Dockerfile.gated deploy/hermes/gated >/dev/null || return 1
  docker build -q -t hermes-deploy-brain-mcp deploy/hermes/brain-mcp >/dev/null || return 1
  docker build -q -t hermes-ingress deploy/hermes/ingress >/dev/null || return 1
  local base n
  base="$(_layers hermes-base-pinned)"; n="$(printf '%s\n' "$base" | wc -l)"
  [ "$(_layers hermes-gated | head -n "$n")" = "$base" ] \
    || { echo "  FATAL: hermes-gated is not built on the current hermes-base-pinned"; return 1; }
  echo "  gated: rebuilt on hermes-base-pinned (first $n layers identical); brain-mcp, ingress: rebuilt"
}
