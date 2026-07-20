#!/bin/sh
set -eu

DOMAIN="${DOMAIN:-_}"
export DOMAIN

envsubst '${DOMAIN}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf

# Browser-side runtime config (OIDC issuer/client + optional hub app list).
FRONTEND_OIDC_ISSUER="${FRONTEND_OIDC_ISSUER:-http://localhost:8080/realms/funnelmanager}"
FRONTEND_OIDC_CLIENT_ID="${FRONTEND_OIDC_CLIENT_ID:-frontend}"
{
  echo "window.__FM_CONFIG__ = {"
  echo "  oidcIssuer: '${FRONTEND_OIDC_ISSUER}',"
  echo "  oidcClientId: '${FRONTEND_OIDC_CLIENT_ID}',"
  if [ -n "${WEB_APPS:-}" ]; then
    echo "  apps: ${WEB_APPS},"
  fi
  echo "}"
} > /usr/share/nginx/html/config.js

exec nginx -g 'daemon off;'
