#!/usr/bin/env bash
# Remove an isolated e2e stack and its per-user resources. REFUSES to touch the
# default hermes-deploy project, so it can never tear down the real deploy stack.
set -u
PROJECT="${DEPLOY_PROJECT:-hermes-e2e}"
[ "$PROJECT" != "hermes-deploy" ] || { echo "teardown.sh: refusing to tear down the default project"; exit 2; }
NS="$PROJECT-"
docker rm -f $(docker ps -aq --filter "name=${NS}brindlewood") 2>/dev/null >/dev/null || true
docker volume rm $(docker volume ls -q --filter "name=hermes-home-${NS}brindlewood") 2>/dev/null >/dev/null || true
docker network rm $(docker network ls -q --filter "name=u-${NS}brindlewood") 2>/dev/null >/dev/null || true
DEPLOY_PROJECT="$PROJECT" docker compose -f deploy/hermes/docker-compose.yml down -v --remove-orphans 2>/dev/null >/dev/null || true
echo "teardown.sh: removed isolated project $PROJECT"
