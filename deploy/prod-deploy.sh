#!/usr/bin/env bash
#
# Prod deploy for the coexisting funnelmanager-prod stack on usfr2.
# Run on the box, from the prod checkout root, or via deploy/prod-deploy.sh.
#
#   deploy/prod-deploy.sh                # deploy latest origin/main
#   deploy/prod-deploy.sh v1.2.0         # deploy a tag
#   deploy/prod-deploy.sh origin/main    # deploy a branch tip
#   deploy/prod-deploy.sh <sha>          # deploy a specific commit
#
# CI (.github/workflows/deploy-prod.yml) invokes this over SSH through a
# forced-command hook; the requested ref arrives as the first argument.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

REF="${1:-origin/main}"
COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"

if [ ! -f "$ENV_FILE" ]; then
  echo "FATAL: $ENV_FILE not found in $REPO_DIR" >&2
  exit 1
fi

echo "==> Fetching git refs"
git fetch --tags --prune --force origin

echo "==> Checking out $REF (detached)"
git -c advice.detachedHead=false checkout -f --detach "$REF"
echo "    now at $(git rev-parse --short HEAD) ($(git log -1 --format=%s))"

echo "==> Building and starting prod stack"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build --remove-orphans

echo "==> Pruning dangling images"
docker image prune -f >/dev/null 2>&1 || true

echo "==> Current state"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps
echo "==> Deploy complete"
