#!/usr/bin/env bash
#
# Forced-command SSH hook for pull-only prod deploys. Installed on usfr2 at
# /home/sam/funnelmanager-prod-deploy-hook.sh and wired to the CI deploy key in
# ~/.ssh/authorized_keys (command="...",no-pty,...). The server never builds:
# CI (.github/workflows/deploy-prod.yml) pushes images to GHCR, then invokes
# this hook, which refreshes the compose file, pulls, and restarts the stack.
#
# Protocol:
#   SSH command: deploy [IMAGE_TAG]   IMAGE_TAG defaults to "prod" (the moving
#                                     tag CI pushes); pass sha-<sha> or v<x.y.z>
#                                     to pin/roll back.
#   stdin line 1: GHCR username
#   stdin line 2: GHCR token (ephemeral GITHUB_TOKEN from the workflow run)
#   stdin rest:   docker-compose.prod.yml content at the deployed ref
set -euo pipefail

DEPLOY_DIR=/home/sam/funnelmanager-prod
COMPOSE_FILE=docker-compose.prod.yml
ENV_FILE=.env.prod

# shellcheck disable=SC2086  # word-splitting the SSH command is intentional
set -- ${SSH_ORIGINAL_COMMAND:-deploy}
[ "${1:-}" = "deploy" ] || { echo "unsupported command: ${1:-}" >&2; exit 64; }
IMAGE_TAG="$(printf '%s' "${2:-prod}" | tr -cd 'A-Za-z0-9._-')"
[ -n "$IMAGE_TAG" ] || IMAGE_TAG=prod

cd "$DEPLOY_DIR"
[ -f "$ENV_FILE" ] || { echo "FATAL: $ENV_FILE not found in $DEPLOY_DIR" >&2; exit 1; }

IFS= read -r GHCR_USER
IFS= read -r GHCR_TOKEN
cat > "$COMPOSE_FILE.incoming"
[ -s "$COMPOSE_FILE.incoming" ] || { echo "FATAL: no compose file on stdin" >&2; exit 1; }
mv "$COMPOSE_FILE.incoming" "$COMPOSE_FILE"

printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
trap 'docker logout ghcr.io >/dev/null 2>&1 || true' EXIT

echo "==> Pulling images (IMAGE_TAG=$IMAGE_TAG)"
IMAGE_TAG="$IMAGE_TAG" docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull -q

echo "==> Starting prod stack"
IMAGE_TAG="$IMAGE_TAG" docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --remove-orphans

echo "==> Pruning unused images"
docker image prune -f >/dev/null 2>&1 || true

echo "==> Current state"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps
echo "==> Deploy complete"
