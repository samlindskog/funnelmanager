#!/bin/sh
set -eu

# Browser-side runtime config — same OIDC session as the hub (shared
# localStorage keys, same public client).
FRONTEND_OIDC_ISSUER="${FRONTEND_OIDC_ISSUER:-http://localhost:8080/realms/funnelmanager}"
FRONTEND_OIDC_CLIENT_ID="${FRONTEND_OIDC_CLIENT_ID:-frontend}"
cat > /usr/share/nginx/html/search/config.js <<EOF
window.__FM_CONFIG__ = {
  oidcIssuer: '${FRONTEND_OIDC_ISSUER}',
  oidcClientId: '${FRONTEND_OIDC_CLIENT_ID}',
}
EOF

exec nginx -g 'daemon off;'
